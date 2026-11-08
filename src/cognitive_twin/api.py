"""HTTP API and dashboard."""

from __future__ import annotations

from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .consent import ConsentProfile
from .explain import skill_statuses
from .rl.agents import TUTOR_ARMS
from .service import LearnerSession, Platform

app = FastAPI(title="Cognitive Twin", version=__version__,
              description="Multi-agent RL for personalised education")
platform = Platform()


class EnrolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    age: int = Field(ge=5, le=120)
    region: str = "IN"
    neural_opt_in: bool = False
    guardian_consent: bool = False
    simulated: bool = True


class NextRequest(BaseModel):
    skip_break: bool = False


class AnswerRequest(BaseModel):
    item_id: str
    choice: int = Field(ge=0, le=3)


class AutoplayRequest(BaseModel):
    steps: int = Field(default=20, ge=1, le=200)


class CredentialRequest(BaseModel):
    skill: str


def _session(sid: str) -> LearnerSession:
    try:
        return platform.get(sid)
    except KeyError:
        raise HTTPException(404, "learner not found") from None


def _view(s: LearnerSession) -> dict:
    creds = [platform.ledger.credentials[c] for c in s.credentials]
    credentialed = {c["credentialSubject"]["skill"] for c in creds}
    skills = skill_statuses(s.twin, credentialed)
    return {
        "id": s.id, "name": s.name, "simulated": s.simulated is not None,
        "consent": s.consent.explain(), "sensing_active": s.edge.active,
        "twin": s.twin.snapshot(),
        "skills": skills,
        "track": platform.track.name,
        "progress": {"mastered": sum(x["status"] in ("mastered", "credentialed") for x in skills),
                     "total": len(skills), "credentials": len(creds)},
        "interventions": [i.as_dict() for i in s.teacher.guardian.log[-20:]],
        "log": s.log[-30:],
        "credentials": creds,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return resources.files("cognitive_twin").joinpath("web/index.html").read_text()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/api/learners", status_code=201)
def enrol(req: EnrolRequest) -> dict:
    consent = ConsentProfile(req.age, req.region, req.neural_opt_in, req.guardian_consent)
    try:
        s = platform.enrol(req.name, consent, req.simulated)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    return _view(s)


@app.get("/api/learners/{sid}")
def learner(sid: str) -> dict:
    return _view(_session(sid))


@app.post("/api/learners/{sid}/next")
def next_step(sid: str, req: NextRequest | None = None) -> dict:
    s = _session(sid)
    d = platform.next_decision(s, skip_break=bool(req and req.skip_break))
    out = d.as_dict()
    out["explanation"] = d.explanation
    if d.action == "teach":
        out["item"] = {"id": d.item.id, "prompt": d.item.prompt, "options": list(d.item.options),
                       "skill_name": platform.track.skills[d.skill].name,
                       # worked examples show the tip up front; other styles keep it back
                       "tip": d.item.hint if d.modality == "worked_example" else None}
    return out


@app.post("/api/learners/{sid}/answer")
def answer(sid: str, req: AnswerRequest) -> dict:
    try:
        return platform.answer(_session(sid), req.item_id, req.choice)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/learners/{sid}/break")
def take_break(sid: str) -> dict:
    s = _session(sid)
    platform.take_break(s)
    return {"ok": True, "twin": s.twin.snapshot()}


@app.post("/api/learners/{sid}/autoplay")
def autoplay(sid: str, req: AutoplayRequest) -> dict:
    s = _session(sid)
    try:
        steps = platform.autoplay(s, req.steps)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"steps": steps, **_view(s)}


@app.post("/api/learners/{sid}/credentials")
def request_credential(sid: str, req: CredentialRequest) -> dict:
    s = _session(sid)
    try:
        res = platform.request_credential(s, req.skill)
    except KeyError:
        raise HTTPException(404, "unknown skill") from None
    return {"issued": res.issued, "reason": res.reason, "votes": res.votes,
            "slashed": res.slashed, "credential": res.credential}


@app.get("/api/credentials/{cid}/verify")
def verify(cid: str) -> dict:
    cred = platform.ledger.credentials.get(cid)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return {"credential": cred, "verification": platform.ledger.verify_credential(cred)}


@app.get("/api/ledger")
def ledger() -> dict:
    lg = platform.ledger
    return {"chain_intact": lg.verify_chain(), "blocks": lg.blocks[-20:],
            "validators": [{"name": v.name, "did": v.did, "stake": v.stake}
                           for v in lg.validators]}


@app.get("/api/policy")
def policy() -> dict:
    p = platform.policy
    return {
        "tutor_arm_pulls": {f"d{d}/{m}": int(n) for (d, m), n in
                            zip(TUTOR_ARMS, p.tutor.model.pulls, strict=True)},
        "curriculum_pulls": dict(zip(p.curriculum.skills, map(int, p.curriculum.model.pulls),
                                     strict=True)),
        "pacing_q": {s: {"continue": round(q[0], 3), "break": round(q[1], 3)}
                     for s, q in sorted(p.pacing.q.items())},
    }
