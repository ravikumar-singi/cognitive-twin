"""Learner routes: everything here acts on the signed-in user's own records only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db.models import User
from ..deps import SESSION_COOKIE, CommitRoute, ctx, current_user, service, state
from ..learning import LearningService

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


def _learner(user: User) -> User:
    if user.role != "learner":
        raise HTTPException(403, "this area is for learners")
    return user


@router.get("")
def me(user: User = Depends(current_user), svc: LearningService = Depends(service)) -> dict:
    if user.role != "learner":
        return {"user": {"id": user.id, "name": user.display_name, "role": user.role,
                         "region": user.region, "institution": user.institution}}
    return svc.view(user)


@router.post("/consent")
def consent(body: ConsentBody, request: Request, user: User = Depends(current_user),
            svc: LearningService = Depends(service)) -> dict:
    _learner(user)
    return svc.record_consent(ctx(request), user, body.learning, body.neural_sensing)


@router.get("/consent/history")
def consent_history(user: User = Depends(current_user),
                    svc: LearningService = Depends(service)) -> list[dict]:
    return svc.consent_history(user.id)


@router.post("/next")
def next_step(request: Request, body: NextBody | None = None,
              user: User = Depends(current_user), svc: LearningService = Depends(service)):
    _learner(user)
    return svc.next(ctx(request), user, skip_break=bool(body and body.skip_break))


@router.post("/answer")
def answer(body: AnswerBody, request: Request, user: User = Depends(current_user),
           svc: LearningService = Depends(service)) -> dict:
    _learner(user)
    return svc.answer(ctx(request), user, body.item_id, body.choice)


@router.post("/break")
def take_break(request: Request, user: User = Depends(current_user),
               svc: LearningService = Depends(service)) -> dict:
    _learner(user)
    return svc.take_break(ctx(request), user)


@router.post("/credentials")
def claim(body: CredentialBody, request: Request, user: User = Depends(current_user),
          svc: LearningService = Depends(service)) -> dict:
    _learner(user)
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
    svc.delete(ctx(request), user)
    resp = JSONResponse({"deleted": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
