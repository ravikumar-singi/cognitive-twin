"""Learner routes: everything here acts on the signed-in user's own records only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Cohort, CohortMember, School, Twin, User
from ..deps import (
    DB,
    SESSION_COOKIE,
    CommitRoute,
    ctx,
    current_user,
    default_track,
    service,
    state,
)
from ..learning import LearningService
from ..schools import allowed_tracks

router = APIRouter(prefix="/api/me", tags=["learner"], route_class=CommitRoute)


class ConsentBody(BaseModel):
    learning: bool
    neural_sensing: bool = False


class NextBody(BaseModel):
    skip_break: bool = False


class AnswerBody(BaseModel):
    item_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    choice: int = Field(ge=0, le=3)


class CredentialBody(BaseModel):
    skill: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")


class DeleteBody(BaseModel):
    confirm: str


def _learner(user: User, svc: LearningService | None = None) -> User:
    if user.role != "learner":
        raise HTTPException(403, "this area is for learners")
    if svc is not None:
        school = svc.db.get(School, user.school_id) if user.school_id else None
        if svc.track.id not in allowed_tracks(school, {svc.track.id: svc.track}):
            raise HTTPException(404, "your school doesn't offer that subject")
    return user


@router.get("/subjects")
def subjects(request: Request, user: User = Depends(current_user), db: Session = DB) -> dict:
    """The subjects this learner can study, with their progress in each."""
    st = state(request)
    school = db.get(School, user.school_id) if user.school_id else None
    twins = {t.track_id: t for t in db.scalars(select(Twin).where(Twin.user_id == user.id))}
    classes = db.execute(select(Cohort.name, Cohort.track_id)
                         .join(CohortMember, CohortMember.cohort_id == Cohort.id)
                         .where(CohortMember.user_id == user.id)).all()
    out = []
    for tid in allowed_tracks(school, st.tracks):
        t = st.tracks[tid]
        tw = twins.get(tid)
        p = (tw.p_known if tw else {}) or {}
        out.append({"id": tid, "name": t.name, "audience": t.audience,
                    "description": t.description, "skills": len(t.skills),
                    "mastered": sum(1 for v in p.values() if v >= 0.95),
                    "questions_answered": tw.step if tw else 0,
                    "classes": [n for n, ct in classes if ct == tid]})
    out.sort(key=lambda x: not x["classes"])  # the learner's class subjects first
    return {"subjects": out, "default": default_track(db, user, st.tracks),
            "school": school.name if school else None,
            "classes": [n for n, _ in classes]}


@router.get("")
def me(user: User = Depends(current_user), svc: LearningService = Depends(service)) -> dict:
    if user.role != "learner":
        return {"user": {"id": user.id, "name": user.display_name, "role": user.role,
                         "region": user.region, "institution": user.institution}}
    _learner(user, svc)
    return svc.view(user)


@router.post("/consent")
def consent(body: ConsentBody, request: Request, user: User = Depends(current_user),
            svc: LearningService = Depends(service)) -> dict:
    _learner(user, svc)
    return svc.record_consent(ctx(request), user, body.learning, body.neural_sensing)


@router.get("/consent/history")
def consent_history(user: User = Depends(current_user),
                    svc: LearningService = Depends(service)) -> list[dict]:
    return svc.consent_history(user.id)


@router.post("/next")
def next_step(request: Request, body: NextBody | None = None,
              user: User = Depends(current_user), svc: LearningService = Depends(service)):
    _learner(user, svc)
    return svc.next(ctx(request), user, skip_break=bool(body and body.skip_break))


@router.post("/answer")
def answer(body: AnswerBody, request: Request, user: User = Depends(current_user),
           svc: LearningService = Depends(service)) -> dict:
    _learner(user, svc)
    return svc.answer(ctx(request), user, body.item_id, body.choice)


@router.post("/break")
def take_break(request: Request, user: User = Depends(current_user),
               svc: LearningService = Depends(service)) -> dict:
    _learner(user, svc)
    return svc.take_break(ctx(request), user)


@router.post("/credentials")
def claim(body: CredentialBody, request: Request, user: User = Depends(current_user),
          svc: LearningService = Depends(service)) -> dict:
    _learner(user, svc)
    st = state(request)
    ok, reset = st.limiter.hit(f"cred:{user.id}", st.settings.rate_credential_per_min)
    if not ok:
        raise HTTPException(429, "Too many credential requests. Try again in a minute.",
                            headers={"Retry-After": str(reset)})
    out = svc.request_credential(ctx(request), user, body.skill)
    return {"issued": out.issued, "reason": out.reason, "credential": out.credential,
            "votes": [{"validator": v["validator"], "approve": v["approve"]} for v in out.votes]}


@router.get("/export")
def export(request: Request, user: User = Depends(current_user),
           svc: LearningService = Depends(service)):
    data = svc.export(ctx(request), user)
    return JSONResponse(data, headers={
        "Content-Disposition": 'attachment; filename="cognitive-twin-my-data.json"'})


@router.post("/delete")
def delete_me(body: DeleteBody, request: Request, user: User = Depends(current_user),
              svc: LearningService = Depends(service)):
    if body.confirm != "delete my data":
        raise HTTPException(400, 'type "delete my data" to confirm')
    if user.role == "admin":
        raise HTTPException(400, "administrator accounts are removed through the institution")
    if user.role == "learner" and user.age_band != "adult":
        # a child's account belongs to the school's relationship with the family
        raise HTTPException(403, "Ask your teacher or parent to delete your account. "
                                 "They can do it for you straight away.")
    svc.delete(ctx(request), user)
    resp = JSONResponse({"deleted": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
