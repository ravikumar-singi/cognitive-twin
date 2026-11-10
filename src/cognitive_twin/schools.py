"""Schools, classes and accounts for schools without single sign-on.

A school is the unit of trust: its records say how old a learner is and whether a
parent or guardian has agreed, and its staff are the only people who can see its
learners. Schools with an identity provider sign in through it; the rest use class
codes and usernames. Nothing here sends e-mail, so no address is needed for a child.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import (
    Cohort,
    CohortMember,
    ConsentRecord,
    Invite,
    LocalCredential,
    School,
    User,
    utcnow,
)
from .skills import Track

BANDS = ("under13", "13to17", "adult")
LOCAL_ISSUER = "local://"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
MAX_FAILED = 5
LOCKOUT = timedelta(minutes=15)
INVITE_TTL = timedelta(days=14)
MAX_WAITING_PER_CLASS = 60
STUDENT_MIN_PASSWORD = 8
STAFF_MIN_PASSWORD = 10
GUARDIAN_ATTESTATION_TEXT = (
    "The school confirms that it holds the agreement of this learner's parent or guardian "
    "for the learner to use Cognitive Twin for school-directed learning."
)
_SCRYPT = (2**14, 8, 1)
_DUMMY_HASH = ""


class AccountError(ValueError):
    """A problem the person can fix; the message is shown to them."""


def is_local(user: User) -> bool:
    return user.idp_issuer.startswith(LOCAL_ISSUER)


# --- passwords ----------------------------------------------------------------------

def hash_password(password: str) -> str:
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, key = stored.split("$")
        want = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def check_password(password: str, role: str, *avoid: str) -> None:
    need = STUDENT_MIN_PASSWORD if role == "learner" else STAFF_MIN_PASSWORD
    if len(password) < need:
        raise AccountError(f"Choose a password with at least {need} characters.")
    if len(password) > 200:
        raise AccountError("That password is too long.")
    low = password.lower()
    if len(set(low)) < 4 or any(a and a.lower() == low for a in avoid):
        raise AccountError("That password is too easy to guess. Try a few words together.")


def temp_password() -> str:
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(3))


def new_code(n: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def clean_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper())[:32]


def _code_hash(code: str) -> str:
    return hashlib.sha256(clean_code(code).encode()).hexdigest()


# --- schools ------------------------------------------------------------------------

def create_school(db: Session, name: str, region: str, youngest_band: str = "adult",
                  sign_in: str = "local") -> School:
    name = " ".join(name.split())[:120]
    if not name:
        raise AccountError("The school needs a name.")
    if youngest_band not in BANDS:
        raise AccountError(f"youngest age group must be one of {', '.join(BANDS)}")
    if db.scalar(select(School).where(School.name == name)):
        raise AccountError("A school with that name already exists.")
    school = School(name=name, region=region.upper()[:8] or "IN", youngest_band=youngest_band,
                    sign_in=sign_in)
    db.add(school)
    db.flush()
    return school


def school_for_sso(db: Session, name: str, region: str, youngest_band: str) -> School:
    """Get or create the school an identity provider speaks for (safe under races)."""
    name = " ".join(name.split())[:120]
    q = select(School).where(School.name == name)
    school = db.scalar(q)
    if school is None:
        try:
            with db.begin_nested():
                school = School(name=name, region=region, youngest_band=youngest_band,
                                sign_in="sso")
                db.add(school)
        except IntegrityError:
            school = db.scalar(q)
    return school


def allowed_tracks(school: School | None, tracks: dict[str, Track]) -> list[str]:
    if school is None or not school.allowed_tracks:
        return list(tracks)
    return [t for t in tracks if t in school.allowed_tracks]


def create_invite(db: Session, school: School, role: str, created_by: str) -> str:
    if role not in ("educator", "school_admin"):
        raise AccountError("Invitations are for teachers and school administrators.")
    code = new_code(12)
    db.add(Invite(code_hash=_code_hash(code), school_id=school.id, role=role,
                  created_by=created_by, expires_at=utcnow() + INVITE_TTL))
    db.flush()
    return "-".join(code[i:i + 4] for i in range(0, 12, 4))


# --- accounts -----------------------------------------------------------------------

def _first_name(name: str) -> str:
    name = re.sub(r"[^\w .'-]", "", " ".join(name.split()), flags=re.UNICODE)
    return name[:40]


def _new_username(db: Session, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", name.lower())[:12] or "user"
    for _ in range(20):
        candidate = f"{slug}-{new_code(4).lower()}"
        if not db.scalar(select(LocalCredential).where(LocalCredential.username == candidate)):
            return candidate
    raise AccountError("Could not create a username. Please try again.")


def create_local_user(db: Session, school: School | None, name: str, role: str, password: str,
                      must_change: bool = False, age_band: str = "unknown",
                      guardian_ok: bool = False) -> tuple[User, str]:
    name = _first_name(name)
    if not name:
        raise AccountError("Please enter a name.")
    if role == "learner":
        name = name.split()[0]  # first name only: all a class needs to tell learners apart
    username = _new_username(db, name)
    if not must_change:
        check_password(password, role, username, name)
    if role != "learner":
        age_band = "adult"
    sid = school.id if school else "platform"
    user = User(school_id=school.id if school else None, idp_issuer=LOCAL_ISSUER + sid,
                idp_subject=username, email=None, display_name=name, role=role,
                institution=school.name if school else None,
                region=school.region if school else "IN", age_band=age_band,
                adult_verified=age_band == "adult", guardian_ok=False,
                subject_did="did:ct:learner:" + secrets.token_hex(16))
    db.add(user)
    db.flush()
    db.add(LocalCredential(user_id=user.id, username=username,
                           password_hash=hash_password(password), must_change=must_change))
    if guardian_ok and age_band in ("under13", "13to17"):
        set_guardian_consent(db, user, True)
    db.flush()
    return user, username


def set_guardian_consent(db: Session, user: User, ok: bool) -> None:
    """Record the school's attestation; the append-only history keeps every change."""
    if bool(user.guardian_ok) == ok:
        return
    user.guardian_ok = ok
    db.add(ConsentRecord(user_id=user.id, scope="guardian", granted=ok,
                         policy_version="school-attestation-v1",
                         policy_text_sha256=hashlib.sha256(
                             GUARDIAN_ATTESTATION_TEXT.encode()).hexdigest(),
                         source="school"))


def set_age_band(db: Session, user: User, age_band: str, guardian_ok: bool) -> None:
    if age_band not in BANDS:
        raise AccountError("Choose the learner's age group.")
    user.age_band = age_band
    user.adult_verified = age_band == "adult"
    if age_band == "under13":
        user.email = None
        user.display_name = user.display_name.split()[0] if user.display_name else "Learner"
    set_guardian_consent(db, user, guardian_ok and age_band != "adult")
    db.flush()


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Check a username and password. Locks the account after repeated failures."""
    global _DUMMY_HASH
    cred = db.scalar(select(LocalCredential)
                     .where(LocalCredential.username == username.strip().lower()[:40])
                     .with_for_update())
    if cred is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("not-a-real-password")
        verify_password(password, _DUMMY_HASH)  # same work whether or not the name exists
        return None
    now = utcnow()
    locked = cred.locked_until
    if locked is not None and locked.tzinfo is None:
        locked = locked.replace(tzinfo=now.tzinfo)
    if locked is not None and locked > now:
        return None
    if not verify_password(password[:200], cred.password_hash):
        cred.failed_attempts += 1
        if cred.failed_attempts >= MAX_FAILED:
            cred.locked_until = now + LOCKOUT
            cred.failed_attempts = 0
        db.flush()
        return None
    cred.failed_attempts = 0
    cred.locked_until = None
    user = db.get(User, cred.user_id)
    user.last_login_at = now
    db.flush()
    return user


def set_password(db: Session, user: User, password: str, must_change: bool) -> None:
    cred = db.get(LocalCredential, user.id)
    if cred is None:
        raise AccountError("This person signs in through their school's own sign-in page.")
    if not must_change:
        check_password(password, user.role, cred.username, user.display_name)
    cred.password_hash = hash_password(password)
    cred.must_change = must_change
    cred.failed_attempts = 0
    cred.locked_until = None
    cred.session_epoch += 1  # signs out every existing session
    db.flush()


def accept_invite(db: Session, code: str, name: str, password: str) -> tuple[User, str]:
    inv = db.scalar(select(Invite).where(Invite.code_hash == _code_hash(code))
                    .with_for_update())
    now = utcnow()
    if inv is not None:
        exp = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(
            tzinfo=now.tzinfo)
    if inv is None or inv.used_at is not None or exp < now:
        raise AccountError("That invitation code isn't valid. Ask for a new one.")
    school = db.get(School, inv.school_id)
    user, username = create_local_user(db, school, name, inv.role, password)
    inv.used_at = now
    db.flush()
    return user, username


# --- classes ------------------------------------------------------------------------

def create_class(db: Session, school: School, name: str, track_id: str,
                 teacher: User | None) -> Cohort:
    name = " ".join(name.split())[:120]
    if not name:
        raise AccountError("Give the class a name.")
    if db.scalar(select(Cohort).where(Cohort.school_id == school.id, Cohort.name == name)):
        raise AccountError("Your school already has a class with that name.")
    c = Cohort(school_id=school.id, name=name, track_id=track_id, join_code=_free_code(db))
    db.add(c)
    db.flush()
    if teacher is not None and teacher.role == "educator":
        db.add(CohortMember(cohort_id=c.id, user_id=teacher.id, member_role="educator"))
        db.flush()
    return c


def _free_code(db: Session) -> str:
    for _ in range(20):
        code = new_code(8)
        if not db.scalar(select(Cohort).where(Cohort.join_code == code)):
            return code
    raise AccountError("Could not create a class code. Please try again.")


def new_join_code(db: Session, cohort: Cohort, enabled: bool = True) -> str | None:
    cohort.join_code = _free_code(db) if enabled else None
    db.flush()
    return cohort.join_code


def join_class(db: Session, code: str, first_name: str, password: str
               ) -> tuple[User, str, Cohort]:
    cohort = db.scalar(select(Cohort).where(Cohort.join_code == clean_code(code)))
    if cohort is None or not clean_code(code):
        raise AccountError("That class code isn't right. Check it with your teacher.")
    waiting = db.scalar(select(func.count()).select_from(CohortMember)
                        .join(User, User.id == CohortMember.user_id)
                        .where(CohortMember.cohort_id == cohort.id,
                               User.age_band == "unknown"))
    if waiting >= MAX_WAITING_PER_CLASS:
        raise AccountError("This class has too many people waiting. Tell your teacher.")
    school = db.get(School, cohort.school_id)
    user, username = create_local_user(db, school, first_name, "learner", password)
    db.add(CohortMember(cohort_id=cohort.id, user_id=user.id, member_role="learner"))
    db.flush()
    return user, username, cohort


def add_student(db: Session, cohort: Cohort, first_name: str, age_band: str,
                guardian_ok: bool) -> tuple[User, str, str]:
    if age_band not in BANDS:
        raise AccountError("Choose the learner's age group.")
    school = db.get(School, cohort.school_id)
    pw = temp_password()
    user, username = create_local_user(db, school, first_name, "learner", pw, must_change=True,
                                       age_band=age_band, guardian_ok=guardian_ok)
    db.add(CohortMember(cohort_id=cohort.id, user_id=user.id, member_role="learner"))
    db.flush()
    return user, username, pw
