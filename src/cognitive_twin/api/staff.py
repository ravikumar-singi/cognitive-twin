"""Educator and administrator routes. Authorisation is enforced on every call (R3.2, R3.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import (
    AuditEvent,
    Cohort,
    CohortMember,
    Credential,
    Decision,
    Twin,
    User,
    Validator,
)
from ..deps import DB, CommitRoute, ctx, educator_can_see, require_role, service, state
from ..keys import KeyRegistry, KeyServiceError
from ..learning import LearningService, audit
from ..ledger.service import LedgerService
from ..policy_store import PolicyError, activate_policy, import_policy, list_policies

edu = APIRouter(prefix="/api/educator", tags=["educator"], route_class=CommitRoute)
adm = APIRouter(prefix="/api/admin", tags=["admin"], route_class=CommitRoute)
staff = require_role("educator", "admin")
admin_only = require_role("admin")


class CredentialBody(BaseModel):
    skill: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")


class RevokeBody(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def _learner_or_404(db: Session, educator: User, learner_id: str) -> User:
    learner = db.get(User, learner_id)
    # same response whether the learner is missing or outside the educator's cohorts
    if learner is None or learner.role != "learner" or not educator_can_see(db, educator,
                                                                            learner_id):
        raise HTTPException(404, "learner not found")
    return learner


@edu.get("/cohorts")
def cohorts(user: User = Depends(staff), db: Session = DB) -> list[dict]:
    q = select(Cohort).order_by(Cohort.name)
    if user.role == "educator":
        q = q.join(CohortMember, CohortMember.cohort_id == Cohort.id).where(
            CohortMember.user_id == user.id, CohortMember.member_role == "educator")
    out = []
    for c in db.scalars(q).all():
        n = db.scalar(select(func.count()).select_from(CohortMember).where(
            CohortMember.cohort_id == c.id, CohortMember.member_role == "learner"))
        out.append({"id": c.id, "name": c.name, "learners": n})
    return out


@edu.get("/cohorts/{cohort_id}/learners")
def cohort_learners(cohort_id: str, user: User = Depends(staff),
                    db: Session = DB) -> list[dict]:
    if user.role == "educator" and db.get(CohortMember, (cohort_id, user.id)) is None:
        raise HTTPException(404, "cohort not found")
    rows = db.execute(select(User, Twin).join(CohortMember, CohortMember.user_id == User.id)
                      .outerjoin(Twin, Twin.user_id == User.id)
                      .where(CohortMember.cohort_id == cohort_id,
                             CohortMember.member_role == "learner")
                      .order_by(User.display_name)).all()
    out = []
    for u, t in rows:
        p = (t.p_known if t else {}) or {}
        last = db.scalar(select(func.max(Decision.created_at)).where(Decision.user_id == u.id))
        creds = db.scalar(select(func.count()).select_from(Credential)
                          .where(Credential.subject_did == u.subject_did))
        out.append({"id": u.id, "name": u.display_name, "steps": t.step if t else 0,
                    "mastered": sum(1 for v in p.values() if v >= 0.95),
                    "credentials": creds, "last_active": last.isoformat() if last else None})
    return out


@edu.get("/learners/{learner_id}")
def learner_view(learner_id: str, user: User = Depends(staff), db: Session = DB,
                 svc: LearningService = Depends(service)) -> dict:
    return svc.view(_learner_or_404(db, user, learner_id))


@edu.post("/learners/{learner_id}/credentials")
def issue_for(learner_id: str, body: CredentialBody, request: Request,
              user: User = Depends(staff), db: Session = DB,
              svc: LearningService = Depends(service)) -> dict:
    learner = _learner_or_404(db, user, learner_id)
    out = svc.request_credential(ctx(request), learner, body.skill)
    return {"issued": out.issued, "reason": out.reason, "credential": out.credential}


@edu.post("/credentials/{credential_id}/revoke")
def revoke(credential_id: str, body: RevokeBody, request: Request, user: User = Depends(staff),
           db: Session = DB) -> dict:
    cred = db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    owner = db.scalar(select(User).where(User.subject_did == cred.subject_did))
    if user.role != "admin" and (owner is None or not educator_can_see(db, user, owner.id)):
        raise HTTPException(404, "credential not found")
    st = state(request)
    LedgerService(db, KeyRegistry(db, st.key_provider)).revoke(credential_id, body.reason,
                                                               user.id)
    audit(db, ctx(request), "credential.revoke", credential_id, reason=body.reason)
    return {"revoked": True}


# --- administrators -------------------------------------------------------------------------

class ActivateBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RotateBody(BaseModel):
    validator_id: str = Field(min_length=1, max_length=64)
    kms_key_arn: str | None = Field(default=None, max_length=255)


class CohortBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@adm.get("/policies")
def policies(user: User = Depends(admin_only), db: Session = DB) -> list[dict]:
    return list_policies(db)


@adm.post("/policies")
async def upload_policy(request: Request, user: User = Depends(admin_only),
                        db: Session = DB) -> dict:
    try:
        blob = await request.json()
        version = import_policy(db, blob, user.id, notes="uploaded by admin", activate=False)
    except (PolicyError, ValueError) as e:
        raise HTTPException(400, f"invalid policy: {e}") from None
    audit(db, ctx(request), "policy.import", version)
    return {"version": version, "active": False}


@adm.post("/policies/{version}/activate")
def activate(version: str, body: ActivateBody, request: Request,
             user: User = Depends(admin_only), db: Session = DB) -> dict:
    try:
        activate_policy(db, version, user.id, body.reason)
    except PolicyError:
        raise HTTPException(404, "unknown policy version") from None
    state(request).policies.invalidate()
    audit(db, ctx(request), "policy.activate", version, reason=body.reason)
    return {"active": version}


@adm.get("/validators")
def validators(user: User = Depends(admin_only), db: Session = DB) -> list[dict]:
    return [{"id": v.id, "name": v.name, "stake": v.stake}
            for v in db.scalars(select(Validator).order_by(Validator.id)).all()]


@adm.get("/keys")
def keys(request: Request, user: User = Depends(admin_only),
         db: Session = DB) -> list[dict]:
    return KeyRegistry(db, state(request).key_provider).published()


@adm.post("/keys/rotate")
def rotate(body: RotateBody, request: Request, user: User = Depends(admin_only),
           db: Session = DB) -> dict:
    try:
        key = KeyRegistry(db, state(request).key_provider).rotate(body.validator_id,
                                                                  body.kms_key_arn)
    except KeyServiceError as e:
        raise HTTPException(400, str(e)) from None
    audit(db, ctx(request), "key.rotate", key.kid, validator=body.validator_id,
          version=key.version)
    return {"kid": key.kid, "version": key.version}


@adm.get("/ledger/verify")
def verify_ledger(request: Request, user: User = Depends(admin_only),
                  db: Session = DB) -> dict:
    lg = LedgerService(db, KeyRegistry(db, state(request).key_provider))
    ok, bad = lg.verify_chain()
    state(request).chain_ok = ok
    return {"chain_intact": ok, "first_bad_block": bad, "blocks": lg.block_count()}


@adm.get("/audit")
def audit_log(limit: int = 100, user: User = Depends(admin_only),
              db: Session = DB) -> list[dict]:
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())
                      .limit(max(1, min(limit, 500)))).all()
    return [{"at": r.created_at.isoformat(), "actor": r.actor_id, "role": r.actor_role,
             "action": r.action, "target": r.target, "outcome": r.outcome,
             "request_id": r.request_id, "details": r.details} for r in rows]


@adm.post("/cohorts")
def create_cohort(body: CohortBody, request: Request, user: User = Depends(admin_only),
                  db: Session = DB) -> dict:
    if db.scalar(select(Cohort).where(Cohort.name == body.name)):
        raise HTTPException(409, "a cohort with that name exists")
    c = Cohort(name=body.name)
    db.add(c)
    db.flush()
    audit(db, ctx(request), "cohort.create", c.id, name=body.name)
    return {"id": c.id, "name": c.name}
