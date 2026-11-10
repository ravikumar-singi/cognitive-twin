"""Application factory.

``create_app()`` builds a stateless service: everything a request needs is in
PostgreSQL (and Redis for rate-limit counters), so any number of instances can
run behind a load balancer (R1.4).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import auth, learner, public, staff
from .auth.oidc import OIDCClient
from .config import Settings, get_settings
from .db.models import AuditEvent
from .db.session import create_all_for_tests, engine_for, session_factory, transaction
from .deps import AppState
from .keys import KeyRegistry, KeyServiceError, provider_from_settings
from .learning import Conflict, ConsentRequired, NotEligible, NotFound
from .ledger.service import LedgerService
from .observability import (
    RequestContextMiddleware,
    log,
    log_event,
    register_active_learners,
    setup_logging,
    setup_tracing,
)
from .policy_store import PolicyCache, PolicyError, import_bundled_policies
from .security import IPRateLimitMiddleware, RateLimiter, SecurityHeadersMiddleware
from .skills import load_tracks

STATIC = ("app.js", "app.css")


BOOTSTRAP_LOCK_ID = 724_312


@contextmanager
def startup_lock(engine):
    """Serialise startup work across instances that boot at the same moment (R1.4)."""
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": BOOTSTRAP_LOCK_ID})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BOOTSTRAP_LOCK_ID})
            conn.commit()


def bootstrap(st: AppState) -> None:
    with startup_lock(st.engine):
        _bootstrap(st)


def _bootstrap(st: AppState) -> None:
    """Startup checks. Development also creates the schema and seed data."""
    if st.settings.is_dev:
        create_all_for_tests(st.engine)
    with transaction(st.sessions) as db:
        keys = KeyRegistry(db, st.key_provider)
        lg = LedgerService(db, keys)
        lg.ensure_genesis()
        if st.settings.is_dev:
            lg.bootstrap_dev_validators()
            seed_demo_school(db)
        # Every subject starts with the benchmarked policy shipped in this release; admins
        # can import and activate newer versions later (R5). Existing activations are kept.
        added = import_bundled_policies(db, actor_id="system")
        if added:
            db.add(AuditEvent(action="policy.bundled_import", target="startup", outcome="ok",
                              details={"subjects": added}))
        ok, bad = lg.verify_chain()  # R1.2: verified on startup
        st.chain_ok = ok
        db.add(AuditEvent(action="ledger.verify", target="startup",
                          outcome="ok" if ok else "broken", details={"first_bad_block": bad}))
    level = logging.INFO if ok else logging.CRITICAL
    log_event(log, "ledger chain verified at startup" if ok else "LEDGER_CHAIN_BROKEN",
              level=level, first_bad_block=bad)


DEMO_CLASSES = (("ML Pilot A", "ml-foundations", "MLPILOTA"),
                ("ML Pilot B", "ml-foundations", "MLPILOTB"),
                ("Maths 5A", "middle-school-maths", "MATHS5A"),
                ("Algebra 9B", "high-school-algebra", "ALGEBRA9B"))


def seed_demo_school(db) -> None:
    """Development only: a school with classes and fixed class codes to try things with."""
    from sqlalchemy import select

    from .auth.provision import DEV_SCHOOL
    from .db.models import Cohort, School
    school = db.scalar(select(School).where(School.name == DEV_SCHOOL))
    if school is None:
        school = School(name=DEV_SCHOOL, region="IN", youngest_band="under13", sign_in="local")
        db.add(school)
        db.flush()
    for name, track, code in DEMO_CLASSES:
        c = db.scalar(select(Cohort).where(Cohort.school_id == school.id, Cohort.name == name))
        if c is None:
            db.add(Cohort(school_id=school.id, name=name, track_id=track, join_code=code))
    db.flush()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = (settings or get_settings()).validate()
    setup_logging(settings.log_level)
    engine = engine_for(settings.database_url)
    sessions = session_factory(engine)
    st = AppState(settings=settings, engine=engine, sessions=sessions, tracks=load_tracks(),
                  policies=PolicyCache(settings.policy_cache_seconds),
                  key_provider=provider_from_settings(settings),
                  limiter=RateLimiter(settings.redis_url),
                  oidc=OIDCClient(settings) if settings.oidc_enabled else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap(st)
        register_active_learners(sessions)
        log_event(log, "service started", env=settings.env, version=__version__)
        yield

    app = FastAPI(title="Cognitive Twin", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.is_dev else None,
                  redoc_url=None, openapi_url="/openapi.json" if settings.is_dev else None)
    app.state.ct = st
    app.state.metrics_token = os.environ.get("CT_METRICS_TOKEN", "")

    # outermost first: request context -> trusted host -> security headers -> IP limits
    app.add_middleware(IPRateLimitMiddleware, settings=settings, limiter=st.limiter)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    if "*" not in settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestContextMiddleware)

    for r in (auth.router, learner.router, staff.edu, staff.sch, staff.adm, public.router):
        app.include_router(r)

    @app.exception_handler(ConsentRequired)
    async def _consent(_: Request, e: ConsentRequired):
        return JSONResponse({"detail": str(e), "code": "consent_required"}, status_code=403)

    @app.exception_handler(NotEligible)
    async def _eligible(_: Request, e: NotEligible):
        return JSONResponse({"detail": str(e), "code": "not_eligible"}, status_code=403)

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, e: Conflict):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(NotFound)
    async def _missing(_: Request, e: NotFound):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(PolicyError)
    @app.exception_handler(KeyServiceError)
    async def _unavailable(_: Request, e: Exception):
        log_event(log, "dependency unavailable", level=logging.ERROR, error=str(e))
        return JSONResponse({"detail": "A service the tutors depend on is unavailable. "
                                       "Please try again shortly."}, status_code=503)

    @app.exception_handler(SATimeoutError)
    @app.exception_handler(OperationalError)
    async def _busy(_: Request, e: Exception):
        log_event(log, "database busy or unavailable", level=logging.WARNING,
                  error=type(e).__name__)
        return JSONResponse({"detail": "The tutors are very busy right now. Please try again "
                                       "in a few seconds."}, status_code=503,
                            headers={"Retry-After": "5"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return resources.files("cognitive_twin").joinpath("web/index.html").read_text()

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        if name not in STATIC:
            return Response(status_code=404)
        body = resources.files("cognitive_twin").joinpath(f"web/{name}").read_text()
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return Response(body, media_type=media, headers={"Cache-Control": "no-cache"})

    setup_tracing(app, engine)
    return app


def app_from_env() -> FastAPI:  # uvicorn --factory entry point
    return create_app()
