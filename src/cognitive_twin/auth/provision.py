"""Create or update a user from institution identity claims."""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import Cohort, CohortMember, User, utcnow
from .oidc import Identity

DEV_ISSUER = "dev://local"

# Development personas: what an institution IdP would assert about each person.
DEV_PERSONAS: dict[str, dict] = {
    "asha": {"name": "Asha (learner)", "role": "learner", "ct_adult_verified": True,
             "ct_region": "IN", "ct_cohorts": ["ML Pilot A"], "ct_institution": "Demo Univ"},
    "ben": {"name": "Ben (learner, UK)", "role": "learner", "ct_adult_verified": True,
            "ct_region": "UK", "ct_cohorts": ["ML Pilot A"], "ct_institution": "Demo Univ"},
    "chen": {"name": "Chen (learner, EU)", "role": "learner", "ct_adult_verified": True,
             "ct_region": "EU", "ct_cohorts": ["ML Pilot B"], "ct_institution": "Demo Univ"},
    "dev-minor": {"name": "Dana (under 18)", "role": "learner", "birthdate": "2012-04-01",
                  "ct_region": "IN", "ct_cohorts": ["ML Pilot A"],
                  "ct_institution": "Demo Univ"},
    "priya": {"name": "Priya (educator, Pilot A)", "role": "educator",
              "ct_adult_verified": True, "ct_region": "IN", "ct_cohorts": ["ML Pilot A"],
              "ct_institution": "Demo Univ"},
    "admin": {"name": "Sam (administrator)", "role": "admin", "ct_adult_verified": True,
              "ct_region": "IN", "ct_cohorts": [], "ct_institution": "Demo Univ"},
}


def upsert_user(db: Session, ident: Identity) -> User:
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
    user.email = ident.email
    user.display_name = ident.name
    user.role = ident.role
    user.region = ident.region
    user.adult_verified = ident.adult_verified
    user.institution = ident.institution
    user.last_login_at = utcnow()
    db.flush()
    sync_cohorts(db, user, ident.cohorts)
    return user


def _cohort_id(db: Session, name: str) -> str:
    """Get or create a cohort; safe when many people from a new cohort sign in at once."""
    c = db.scalar(select(Cohort).where(Cohort.name == name))
    if c is not None:
        return c.id
    try:
        with db.begin_nested():
            c = Cohort(name=name)
            db.add(c)
        return c.id
    except IntegrityError:
        return db.scalar(select(Cohort.id).where(Cohort.name == name))


def sync_cohorts(db: Session, user: User, names: list[str]) -> None:
    """The institution's records are the source of truth for cohort membership."""
    member_role = "educator" if user.role == "educator" else "learner"
    wanted: set[str] = set()
    if user.role != "admin":
        for name in names:
            wanted.add(_cohort_id(db, name))
    db.execute(delete(CohortMember).where(CohortMember.user_id == user.id,
                                          CohortMember.cohort_id.not_in(wanted or {""})))
    have = set(db.scalars(select(CohortMember.cohort_id)
                          .where(CohortMember.user_id == user.id)).all())
    for cid in wanted - have:
        db.add(CohortMember(cohort_id=cid, user_id=user.id, member_role=member_role))
    db.flush()
