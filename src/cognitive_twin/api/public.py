"""Unauthenticated routes: credential verification, published keys, health, metrics, demo."""

from __future__ import annotations

import logging
import secrets

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..consent import ConsentProfile
from ..deps import DB, CommitRoute, state
from ..edge.sensor import EdgeProcessor, NeuralBand
from ..env.simulator import SimulatedLearner
from ..explain import explain, skill_statuses
from ..keys import KeyRegistry
from ..ledger.service import LedgerService
from ..observability import log, log_event
from ..rl.shield import Guardian
from ..rl.teacher import MultiAgentTeacher
from ..twin import CognitiveTwin

router = APIRouter(tags=["public"], route_class=CommitRoute)


@router.get("/api/public/credentials/{credential_id}/verify")
def verify(credential_id: str, request: Request, db: Session = DB) -> dict:
    if len(credential_id) > 64:
        raise HTTPException(404, "credential not found")
    out = LedgerService(db, KeyRegistry(db, state(request).key_provider)).verify(credential_id)
    if out is None:
        raise HTTPException(404, "credential not found")
    return out


@router.get("/api/public/keys")
def keys(request: Request, db: Session = DB) -> list[dict]:
    return KeyRegistry(db, state(request).key_provider).published()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request, db: Session = DB):
    st = state(request)
    checks = {"database": False, "policy": False, "ledger_chain": st.chain_ok}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
        st.policies.current(db)
        checks["policy"] = True
    except Exception as e:  # noqa: BLE001 - readiness reports, it never raises
        log_event(log, "readiness check failed", level=logging.WARNING, error=str(e)[:200])
    ok = all(checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@router.get("/metrics")
def metrics(request: Request):
    token = request.app.state.metrics_token
    if token and not secrets.compare_digest(request.headers.get("authorization", ""),
                                            f"Bearer {token}"):
        raise HTTPException(401, "metrics token required")
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class DemoBody(BaseModel):
    steps: int = Field(default=120, ge=1, le=300)
    seed: int | None = Field(default=None, ge=0, le=2**31)
    headband: bool = True
    name: str = Field(default="Demo student", max_length=40)


@router.post("/api/demo/run")
def demo(body: DemoBody, request: Request, db: Session = DB) -> dict:
    """Teach a simulated student and return the whole trajectory for playback.

    Stateless and personal-data-free: nothing is stored. Disabled in production
    unless CT_DEMO_ENABLED=true.
    """
    st = state(request)
    if not st.settings.demo_enabled:
        raise HTTPException(404, "the demo is not enabled here")
    policy = st.policies.current(db)
    seed = body.seed if body.seed is not None else secrets.randbelow(2**31)
    rng = np.random.default_rng(seed)
    track = st.track
    learner = SimulatedLearner.sample(track, rng)
    twin = CognitiveTwin("demo", track)
    cur, tutor, pacing = policy.agents(track, {})
    guardian = Guardian()
    teacher = MultiAgentTeacher(track, np.random.default_rng(seed + 1), cur, tutor, pacing,
                                guardian)
    edge = EdgeProcessor(ConsentProfile(age=30, region="IN", neural_opt_in=body.headband),
                         NeuralBand(rng))
    frames, probe = [], track.topological_order()[0]
    for _ in range(body.steps):
        twin.observe_state(edge.sense(learner, probe, 2))
        d = teacher.act(twin, edge.active, explore=False)
        ex = explain(d, twin)
        if d.action == "break":
            learner.rest()
            twin.observe_break()
            frame = {"action": "break", "explanation": ex, "done": d.proposed_action == "done"}
        else:
            probe = d.skill
            out = learner.attempt(d.skill, d.difficulty, d.modality)
            twin.observe_answer(d.item.id, d.skill, d.difficulty, d.modality, out.correct)
            frame = {"action": "teach", "skill": d.skill, "difficulty": d.difficulty,
                     "modality": d.modality, "correct": out.correct,
                     "belief": round(twin.p_known[d.skill], 3), "explanation": ex}
        frame.update(step=twin.step, skills=skill_statuses(twin, set()),
                     state={"attention": round(twin.attention, 3), "load": round(twin.load, 3),
                            "fatigue_estimate": round(twin.fatigue_estimate(), 3)})
        frames.append(frame)
        if frame.get("done"):
            break
    return {"name": body.name, "seed": seed, "policy_version": policy.version,
            "headband": edge.active, "track": track.name, "frames": frames,
            "interventions": [i.as_dict() for i in guardian.log]}
