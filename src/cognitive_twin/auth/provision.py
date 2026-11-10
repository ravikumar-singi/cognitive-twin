"""Create or update a user from institution identity claims."""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import Cohort, CohortMember, User, utcnow
from ..schools import school_for_sso, set_guardian_consent
from .oidc import Identity

DEV_ISSUER = "dev://local"
DEV_SCHOOL = "Demo School"

# Development personas: what a school's identity provider would assert about each person.
_DEMO = {"ct_institution": DEV_SCHOOL}
DEV_PERSONAS: dict[str, dict] = {
    "asha": {"name": "Asha (adult learner)", "role": "learner", "ct_age_band": "adult",
             "ct_region": "IN", "ct_cohorts": ["ML Pilot A"], **_DEMO},
    "ben": {"name": "Ben (adult learner, UK)", "role": "learner", "ct_age_band": "adult",
            "ct_region": "UK", "ct_cohorts": ["ML Pilot A"], **_DEMO},
    "chen": {"name": "Chen (adult learner, EU)", "role": "learner", "ct_age_band": "adult",
             "ct_region": "EU", "ct_cohorts": ["ML Pilot B"], **_DEMO},
    "maya": {"name": "Maya (age 15)", "role": "learner", "ct_age_band": "13to17",
             "ct_guardian_consent": True, "ct_region": "IN", "ct_cohorts": ["Algebra 9B"],
             **_DEMO},
    "leo": {"name": "Leo (age 10)", "role": "learner", "birthdate": "2016-03-14",
            "ct_guardian_consent": True, "ct_region": "IN", "ct_cohorts": ["Maths 5A"],
            **_DEMO},
    "dev-minor": {"name": "Dana (age 14, no guardian consent yet)", "role": "learner",
                  "birthdate": "2012-04-01", "ct_region": "IN", "ct_cohorts": ["ML Pilot A"],
                  **_DEMO},
    "priya": {"name": "Priya (teacher, ML Pilot A)", "role": "educator", "ct_age_band": "adult",
              "ct_region": "IN", "ct_cohorts": ["ML Pilot A"], **_DEMO},
    "ravi": {"name": "Ravi (teacher, maths)", "role": "educator", "ct_age_band": "adult",
             "ct_region": "IN", "ct_cohorts": ["Maths 5A", "Algebra 9B"], **_DEMO},
    "head": {"name": "Meera (school administrator)", "role": "school_admin",
             "ct_age_band": "adult", "ct_region": "IN", "ct_cohorts": [], **_DEMO},
    "admin": {"name": "Sam (platform administrator)", "role": "admin", "ct_age_band": "adult",
              "ct_region": "IN", "ct_cohorts": [], **_DEMO},
}


def _school_name(ident: Identity) -> str:
    if ident.institution:
        return ident.institution
    host = ident.issuer.split("://", 1)[-1].split("/", 1)[0]
    return host or "Unnamed school"


def upsert_user(db: Session, ident: Identity) -> User:
    school = None
    if ident.role != "admin":
        # SSO schools enrol whoever their IdP says; the IdP's records decide age and consent
        school = school_for_sso(db, _school_name(ident), ident.region, "under13")
    q = select(User).where(User.idp_issuer == ident.issuer, User.idp_subject == ident.subject)
    user = db.scalar(q)
    if user is None:
        try:  # two simultaneous first sign-ins (two tabs) must not both fail
            with db.begin_nested():
                user = User(idp_issuer=ident.issuer, idp_subject=ident.subject,
                            subject_did="did:ct:learner:" + secrets.token_hex(16),
                            display_name=ident.name, role=ident.role, region=ident.region)
                db.add(user)
        except IntegrityError:
            user = db.scalar(q)
    user.school_id = school.id if school else None
    user.email = ident.email
    user.display_name = ident.name
    user.role = ident.role
    user.region = ident.region
    user.age_band = ident.age_band if ident.role == "learner" else "adult"
    user.adult_verified = user.age_band == "adult"
    user.institution = school.name if school else ident.institution
    user.last_login_at = utcnow()
    db.flush()
    set_guardian_consent(db, user, ident.guardian_ok and user.age_band in ("under13", "13to17"))
    sync_cohorts(db, user, ident.cohorts)
    return user


def _cohort_id(db: Session, school_id: str, name: str) -> str:
    """Get or create a class; safe when many people from a new class sign in at once."""
    q = select(Cohort.id).where(Cohort.school_id == school_id, Cohort.name == name)
    cid = db.scalar(q)
    if cid is not None:
        return cid
    try:
        with db.begin_nested():
            c = Cohort(school_id=school_id, name=name)
            db.add(c)
        return c.id
    except IntegrityError:
        return db.scalar(q)


def sync_cohorts(db: Session, user: User, names: list[str]) -> None:
    """For SSO schools, the school's records are the source of truth for class membership."""
    member_role = "educator" if user.role == "educator" else "learner"
    wanted: set[str] = set()
    if user.role in ("learner", "educator") and user.school_id:
        for name in names:
            wanted.add(_cohort_id(db, user.school_id, name))
    db.execute(delete(CohortMember).where(CohortMember.user_id == user.id,
                                          CohortMember.cohort_id.not_in(wanted or {""})))
    have = set(db.scalars(select(CohortMember.cohort_id)
                          .where(CohortMember.user_id == user.id)).all())
    for cid in wanted - have:
        db.add(CohortMember(cohort_id=cid, user_id=user.id, member_role=member_role))
    db.flush()
