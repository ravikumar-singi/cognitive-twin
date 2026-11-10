"""Teacher, school-administrator and platform-administrator routes.

Authorisation is enforced on every call (R3.2, R3.3): teachers see only their own
classes, school administrators only their own school, and platform administrators
operate the service. Anything outside a person's reach answers 404, so the API does
not reveal whether a learner or class exists elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..db.models import (
    AuditEvent,
    Cohort,
    CohortMember,
    Credential,
    Decision,
    LocalCredential,
    School,
    Twin,
    User,
    Validator,
)
from ..deps import DB, CommitRoute, ctx, educator_can_see, require_role, service, state
from ..keys import KeyRegistry, KeyServiceError
from ..learning import LearningService, audit
from ..ledger.service import LedgerService
from ..policy_store import PolicyError, activate_policy, import_policy, list_policies
from ..schools import (
    BANDS,
    AccountError,
    add_student,
    allowed_tracks,
    create_class,
    create_invite,
    create_school,
    is_local,
    new_join_code,
    set_age_band,
    set_password,
    temp_password,
)
from ..twin import MASTERY

edu = APIRouter(prefix="/api/educator", tags=["educator"], route_class=CommitRoute)
sch = APIRouter(prefix="/api/school", tags=["school"], route_class=CommitRoute)
adm = APIRouter(prefix="/api/admin", tags=["admin"], route_class=CommitRoute)
staff = require_role("educator", "school_admin", "admin")
school_staff = require_role("educator", "school_admin")
school_admin = require_role("school_admin")
admin_only = require_role("admin")

Band = Field(pattern=r"^(under13|13to17|adult)$")


class CredentialBody(BaseModel):
    skill: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")


class RevokeBody(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def _bad(e: AccountError):
    return HTTPException(400, str(e))


def _learner_or_404(db: Session, staffer: User, learner_id: str) -> User:
    learner = db.get(User, learner_id)
    # same response whether the learner is missing or outside this person's reach
    if learner is None or learner.role != "learner" or not educator_can_see(db, staffer,
                                                                            learner_id):
        raise HTTPException(404, "learner not found")
    return learner


def _cohort_or_404(db: Session, user: User, cohort_id: str) -> Cohort:
    c = db.get(Cohort, cohort_id)
    ok = c is not None and (
        user.role == "admin"
        or (user.role == "school_admin" and c.school_id == user.school_id)
        or (user.role == "educator" and db.get(CohortMember, (cohort_id, user.id)) is not None))
    if not ok:
        raise HTTPException(404, "class not found")
    return c


def _school(db: Session, user: User) -> School:
    school = db.get(School, user.school_id) if user.school_id else None
    if school is None:
        raise HTTPException(400, "your account isn't linked to a school")
    return school


def _cohort_json(db: Session, c: Cohort, tracks: dict) -> dict:
    by_band = dict(db.execute(
        select(User.age_band, func.count())
        .join(CohortMember, CohortMember.user_id == User.id)
        .where(CohortMember.cohort_id == c.id, CohortMember.member_role == "learner")
        .group_by(User.age_band)).all())
    waiting = by_band.pop("unknown", 0)
    counts = {True: waiting, False: sum(by_band.values())}
    teachers = db.scalars(select(User.display_name)
                          .join(CohortMember, CohortMember.user_id == User.id)
                          .where(CohortMember.cohort_id == c.id,
                                 CohortMember.member_role == "educator")).all()
    t = tracks.get(c.track_id) if c.track_id else None
    return {"id": c.id, "name": c.name, "track": c.track_id,
            "track_name": t.name if t else "Any subject", "join_code": c.join_code,
            "learners": counts.get(False, 0), "waiting": counts.get(True, 0),
            "teachers": list(teachers)}


# --- classes ------------------------------------------------------------------------------

@edu.get("/cohorts")
def cohorts(request: Request, user: User = Depends(staff), db: Session = DB) -> list[dict]:
    q = select(Cohort).order_by(Cohort.name)
    if user.role == "educator":
        q = q.join(CohortMember, CohortMember.cohort_id == Cohort.id).where(
            CohortMember.user_id == user.id, CohortMember.member_role == "educator")
    elif user.role == "school_admin":
        q = q.where(Cohort.school_id == user.school_id)
    tracks = state(request).tracks
    return [_cohort_json(db, c, tracks) for c in db.scalars(q).all()]


class ClassBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    track: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")


@edu.post("/cohorts")
def new_class(body: ClassBody, request: Request, user: User = Depends(school_staff),
              db: Session = DB) -> dict:
    st = state(request)
    school = _school(db, user)
    if body.track not in allowed_tracks(school, st.tracks):
        raise HTTPException(400, "your school doesn't offer that subject")
    try:
        c = create_class(db, school, body.name, body.track, user)
    except AccountError as e:
        raise _bad(e) from None
    audit(db, ctx(request), "class.create", c.id, name=c.name, track=c.track_id)
    return _cohort_json(db, c, st.tracks)


class CodeBody(BaseModel):
    enabled: bool = True


@edu.post("/cohorts/{cohort_id}/code")
def class_code(cohort_id: str, body: CodeBody, request: Request,
               user: User = Depends(staff), db: Session = DB) -> dict:
    c = _cohort_or_404(db, user, cohort_id)
    try:
        code = new_join_code(db, c, body.enabled)
    except AccountError as e:
        raise _bad(e) from None
    audit(db, ctx(request), "class.code", c.id, enabled=body.enabled)
    return {"join_code": code}


@edu.get("/cohorts/{cohort_id}/learners")
def cohort_learners(cohort_id: str, request: Request, user: User = Depends(staff),
                    db: Session = DB) -> list[dict]:
    c = _cohort_or_404(db, user, cohort_id)
    tid = c.track_id or "ml-foundations"
    rows = db.execute(select(User, Twin, LocalCredential.username)
                      .join(CohortMember, CohortMember.user_id == User.id)
                      .outerjoin(Twin, and_(Twin.user_id == User.id, Twin.track_id == tid))
                      .outerjoin(LocalCredential, LocalCredential.user_id == User.id)
                      .where(CohortMember.cohort_id == cohort_id,
                             CohortMember.member_role == "learner")
                      .order_by(User.display_name)).all()
    out = []
    for u, t, username in rows:
        p = (t.p_known if t else {}) or {}
        last = db.scalar(select(func.max(Decision.created_at)).where(Decision.user_id == u.id))
        creds = db.scalar(select(func.count()).select_from(Credential)
                          .where(Credential.subject_did == u.subject_did))
        out.append({"id": u.id, "name": u.display_name, "username": username,
                    "age_band": u.age_band, "guardian_ok": u.guardian_ok,
                    "waiting": u.age_band == "unknown", "local": is_local(u),
                    "steps": t.step if t else 0, "track": tid,
                    "mastered": sum(1 for v in p.values() if v >= MASTERY),
                    "credentials": creds, "last_active": last.isoformat() if last else None})
    return out


class StudentBody(BaseModel):
    first_name: str = Field(min_length=1, max_length=40)
    age_band: str = Band
    guardian_ok: bool = False


@edu.post("/cohorts/{cohort_id}/students")
def new_student(cohort_id: str, body: StudentBody, request: Request,
                user: User = Depends(school_staff), db: Session = DB) -> dict:
    """Add a student directly. Returns a one-time temporary password to hand to them."""
    c = _cohort_or_404(db, user, cohort_id)
    try:
        learner, username, pw = add_student(db, c, body.first_name, body.age_band,
                                            body.guardian_ok)
    except AccountError as e:
        raise _bad(e) from None
    audit(db, ctx(request), "learner.create", learner.id, cohort=c.id, age_band=body.age_band,
          guardian=body.guardian_ok)
    return {"id": learner.id, "name": learner.display_name, "username": username,
            "temporary_password": pw}


class ApproveBody(BaseModel):
    age_band: str = Band
    guardian_ok: bool = False


@edu.post("/learners/{learner_id}/approve")
def approve(learner_id: str, body: ApproveBody, request: Request,
            user: User = Depends(school_staff), db: Session = DB) -> dict:
    """Confirm a learner's age group (and, for under-18s, guardian consent) from school records."""
    learner = _learner_or_404(db, user, learner_id)
    if not is_local(learner):
        raise HTTPException(400, "This learner's age and consent come from your school's "
                                 "sign-in system; update them there.")
    # Recording an under-18's age without guardian consent is allowed; the learner then
    # waits (and is told why) until the school confirms consent.
    set_age_band(db, learner, body.age_band, body.guardian_ok)
    audit(db, ctx(request), "learner.approve", learner.id, age_band=body.age_band,
          guardian=body.guardian_ok)
    return {"id": learner.id, "age_band": learner.age_band, "guardian_ok": learner.guardian_ok}


@edu.post("/learners/{learner_id}/password")
def reset_learner_password(learner_id: str, request: Request,
                           user: User = Depends(school_staff), db: Session = DB) -> dict:
    learner = _learner_or_404(db, user, learner_id)
    if not is_local(learner):
        raise HTTPException(400, "This learner signs in through your school's sign-in page.")
    pw = temp_password()
    set_password(db, learner, pw, must_change=True)
    audit(db, ctx(request), "learner.password_reset", learner.id)
    cred = db.get(LocalCredential, learner.id)
    return {"username": cred.username, "temporary_password": pw}


@edu.get("/learners/{learner_id}")
def learner_view(learner_id: str, user: User = Depends(staff), db: Session = DB,
                 svc: LearningService = Depends(service)) -> dict:
    return svc.view(_learner_or_404(db, user, learner_id))


@edu.get("/learners/{learner_id}/export")
def learner_export(learner_id: str, request: Request, user: User = Depends(school_staff),
                   db: Session = DB, svc: LearningService = Depends(service)):
    """For a parent's or learner's request to see their data, handled by the school."""
    learner = _learner_or_404(db, user, learner_id)
    return JSONResponse(svc.export(ctx(request), learner), headers={
        "Content-Disposition": 'attachment; filename="cognitive-twin-learner-data.json"'})


class DeleteBody(BaseModel):
    confirm: str


@edu.post("/learners/{learner_id}/delete")
def learner_delete(learner_id: str, body: DeleteBody, request: Request,
                   user: User = Depends(school_staff), db: Session = DB,
                   svc: LearningService = Depends(service)) -> dict:
    learner = _learner_or_404(db, user, learner_id)
    if body.confirm != "delete":
        raise HTTPException(400, 'type "delete" to confirm')
    svc.delete(ctx(request), learner)
    return {"deleted": True}


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


# --- school administrators -----------------------------------------------------------------

def _school_json(db: Session, school: School, tracks: dict) -> dict:
    n = dict(db.execute(select(User.role, func.count()).where(User.school_id == school.id)
                        .group_by(User.role)).all())
    return {"id": school.id, "name": school.name, "sign_in": school.sign_in,
            "region": school.region, "youngest_band": school.youngest_band,
            "allowed_tracks": allowed_tracks(school, tracks),
            "all_tracks": [{"id": t.id, "name": t.name, "audience": t.audience}
                           for t in tracks.values()],
            "learners": n.get("learner", 0), "teachers": n.get("educator", 0),
            "admins": n.get("school_admin", 0),
            "classes": db.scalar(select(func.count()).select_from(Cohort)
                                 .where(Cohort.school_id == school.id))}


@sch.get("")
def my_school(request: Request, user: User = Depends(school_admin), db: Session = DB) -> dict:
    school = _school(db, user)
    staff_rows = db.execute(select(User, LocalCredential.username)
                            .outerjoin(LocalCredential, LocalCredential.user_id == User.id)
                            .where(User.school_id == school.id,
                                   User.role.in_(("educator", "school_admin")))
                            .order_by(User.display_name)).all()
    out = _school_json(db, school, state(request).tracks)
    out["staff"] = [{"id": u.id, "name": u.display_name, "role": u.role, "username": un,
                     "local": is_local(u)} for u, un in staff_rows]
    return out


class SchoolSettings(BaseModel):
    youngest_band: str | None = Field(default=None, pattern=r"^(under13|13to17|adult)$")
    allowed_tracks: list[str] | None = Field(default=None, max_length=50)


@sch.post("/settings")
def school_settings(body: SchoolSettings, request: Request, user: User = Depends(school_admin),
                    db: Session = DB) -> dict:
    st = state(request)
    school = _school(db, user)
    if body.youngest_band is not None:
        school.youngest_band = body.youngest_band
    if body.allowed_tracks is not None:
        bad = [t for t in body.allowed_tracks if t not in st.tracks]
        if bad or not body.allowed_tracks:
            raise HTTPException(400, "choose at least one subject from the list")
        school.allowed_tracks = sorted(set(body.allowed_tracks))
    db.flush()
    audit(db, ctx(request), "school.settings", school.id, youngest_band=school.youngest_band,
          allowed_tracks=school.allowed_tracks)
    return _school_json(db, school, st.tracks)


class InviteRole(BaseModel):
    role: str = Field(pattern=r"^(educator|school_admin)$")


@sch.post("/invites")
def school_invite(body: InviteRole, request: Request, user: User = Depends(school_admin),
                  db: Session = DB) -> dict:
    school = _school(db, user)
    code = create_invite(db, school, body.role, user.id)
    audit(db, ctx(request), "invite.create", school.id, role=body.role)
    return {"code": code, "role": body.role, "expires_in_days": 14}


@sch.post("/staff/{staff_id}/password")
def reset_staff_password(staff_id: str, request: Request, user: User = Depends(school_admin),
                         db: Session = DB) -> dict:
    target = db.get(User, staff_id)
    if (target is None or target.school_id != user.school_id
            or target.role not in ("educator", "school_admin") or not is_local(target)):
        raise HTTPException(404, "staff member not found")
    pw = temp_password()
    set_password(db, target, pw, must_change=True)
    audit(db, ctx(request), "staff.password_reset", target.id)
    return {"username": db.get(LocalCredential, target.id).username, "temporary_password": pw}


# --- platform administrators ---------------------------------------------------------------

class ActivateBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RotateBody(BaseModel):
    validator_id: str = Field(min_length=1, max_length=64)
    kms_key_arn: str | None = Field(default=None, max_length=255)


class SchoolBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    region: str = Field(default="IN", min_length=2, max_length=8, pattern=r"^[A-Za-z]+$")
    youngest_band: str = Field(default="adult", pattern=r"^(under13|13to17|adult)$")


@adm.get("/schools")
def schools(request: Request, user: User = Depends(admin_only), db: Session = DB) -> list[dict]:
    tracks = state(request).tracks
    return [_school_json(db, s, tracks)
            for s in db.scalars(select(School).order_by(School.name)).all()]


@adm.post("/schools")
def add_school(body: SchoolBody, request: Request, user: User = Depends(admin_only),
               db: Session = DB) -> dict:
    """Create a school and the invitation code for its first school administrator."""
    try:
        school = create_school(db, body.name, body.region, body.youngest_band)
    except AccountError as e:
        raise _bad(e) from None
    code = create_invite(db, school, "school_admin", user.id)
    audit(db, ctx(request), "school.create", school.id, name=school.name,
          youngest_band=school.youngest_band)
    return {**_school_json(db, school, state(request).tracks), "admin_invite": code}


@adm.post("/schools/{school_id}/invites")
def admin_invite(school_id: str, body: InviteRole, request: Request,
                 user: User = Depends(admin_only), db: Session = DB) -> dict:
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(404, "school not found")
    code = create_invite(db, school, body.role, user.id)
    audit(db, ctx(request), "invite.create", school.id, role=body.role)
    return {"code": code, "role": body.role, "expires_in_days": 14}


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


__all__ = ["adm", "edu", "sch", "BANDS"]
