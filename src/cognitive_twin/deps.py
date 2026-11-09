"""Shared application state and request dependencies (authentication, authorisation)."""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
import anyio.to_thread
from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from .auth.oidc import OIDCClient, OIDCError
from .auth.tokens import TokenError, unsign
from .config import Settings
from .db.models import CohortMember, User
from .keys import KeyProvider, KeyRegistry
from .learning import Ctx, LearningService
from .observability import user_id_var
from .policy_store import PolicyCache
from .security import RateLimiter
from .skills import Track

SESSION_COOKIE = "ct_session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    track: Track
    policies: PolicyCache
    key_provider: KeyProvider
    limiter: RateLimiter
    oidc: OIDCClient | None
    chain_ok: bool = True


def state(request: Request) -> AppState:
    return request.app.state.ct


# Session teardown (commit/rollback/close) runs on its own small thread pool. If it
# shared the request pool, a burst could fill every request thread with handlers
# waiting for a DB connection, while the connections are held by finished requests
# whose teardown is queued behind them: a deadlock until the pool timeout fires.
TEARDOWN_LIMITER = anyio.CapacityLimiter(16)


current_session: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "ct_session", default=None)


def _finish_in_thread(fn):
    """Commit (or roll back) in the handler's own worker thread, before it returns.

    Otherwise the connection stays checked out while FastAPI waits for another thread
    to validate the response and run teardown. Under a burst, finished requests then
    hold every connection waiting for threads, while every thread is held by new
    requests waiting for connections: a circular wait.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            db = current_session.get()
            if db is not None:
                db.rollback()
            raise
        db = current_session.get()
        if db is not None:
            db.commit()
        return result
    return wrapper


class CommitRoute(APIRoute):
    """Route class for every router: sync handlers commit in-thread (see above)."""

    def __init__(self, path: str, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            endpoint = _finish_in_thread(endpoint)
        super().__init__(path, endpoint, **kwargs)


async def get_db(request: Request) -> AsyncIterator[Session]:
    db = state(request).sessions()  # cheap: no connection until the first query
    current_session.set(db)  # copied into the handler's worker-thread context
    ok = False
    try:
        yield db
        ok = True
    finally:
        # Shielded: if the client disconnects (a closed tab) the request is cancelled,
        # and an unshielded cleanup would be cancelled too, leaking the connection
        # "idle in transaction" until the pool runs dry.
        with anyio.CancelScope(shield=True):
            try:
                if ok:
                    await anyio.to_thread.run_sync(db.commit, limiter=TEARDOWN_LIMITER)
                else:
                    await anyio.to_thread.run_sync(db.rollback, limiter=TEARDOWN_LIMITER)
            finally:
                await anyio.to_thread.run_sync(db.close, limiter=TEARDOWN_LIMITER)


# Commit before the response is sent (scope="function"), so a client that acts on a
# response always sees what it reported (read-your-writes).
DB = Depends(get_db, scope="function")


def service(request: Request, db: Session = DB) -> LearningService:
    st = state(request)
    return LearningService(db, st.settings, st.policies, st.track,
                           KeyRegistry(db, st.key_provider))


def _bearer_user(request: Request, db: Session, st: AppState) -> User | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    if st.oidc is None or not st.settings.api_audience:
        raise HTTPException(401, "bearer tokens are not enabled")
    try:
        claims = st.oidc.validate(auth[7:].strip(), st.settings.api_audience)
    except OIDCError:
        raise HTTPException(401, "invalid access token") from None
    user = db.scalar(select(User).where(User.idp_issuer == claims["iss"],
                                        User.idp_subject == str(claims["sub"])))
    if user is None:
        raise HTTPException(403, "sign in through the web app once before using the API")
    request.state.auth = "bearer"
    return user


def current_user(request: Request, db: Session = DB) -> User:
    st = state(request)
    user = _bearer_user(request, db, st)
    if user is None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(401, "please sign in")
        try:
            payload = unsign(token, st.settings.session_secret)
        except TokenError:
            raise HTTPException(401, "your session has expired; please sign in again") from None
        user = db.get(User, payload.get("uid"))
        if user is None:
            raise HTTPException(401, "please sign in")
        if request.method in UNSAFE and request.headers.get("x-csrf-token") != payload["csrf"]:
            raise HTTPException(403, "missing or invalid CSRF token")
        request.state.auth = "cookie"
    # Release the connection before the handler runs. Dependencies and the handler run
    # in separate thread-pool hops; holding a connection across that gap lets a burst
    # starve itself (connections held by requests waiting for threads held by
    # requests waiting for connections). The user object stays usable.
    db.commit()
    ok, reset = st.limiter.hit(f"user:{user.id}", st.settings.rate_user_per_min)
    if not ok:
        raise HTTPException(429, "Too many requests. Please slow down.",
                            headers={"Retry-After": str(reset)})
    user_id_var.set(user.id)
    request.state.user = user
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "you don't have permission to do that")
        return user
    return dep


def ctx(request: Request) -> Ctx:
    from .observability import request_id_var
    return Ctx(actor=getattr(request.state, "user", None),
               ip=getattr(request.state, "client_ip", None),
               request_id=getattr(request.state, "request_id", None) or request_id_var.get())


def educator_can_see(db: Session, educator: User, learner_id: str) -> bool:
    if educator.role == "admin":
        return True
    if educator.role != "educator":
        return False
    mine = select(CohortMember.cohort_id).where(CohortMember.user_id == educator.id,
                                                CohortMember.member_role == "educator")
    hit = db.scalar(select(CohortMember).where(CohortMember.user_id == learner_id,
                                               CohortMember.member_role == "learner",
                                               CohortMember.cohort_id.in_(mine)))
    return hit is not None
