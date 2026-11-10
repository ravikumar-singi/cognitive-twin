"""Relational schema (requirement R1).

Two kinds of table:

* **Personal learning data** (users, twins, attempts, decisions, guardian
  interventions, cohort membership) is deleted on request (R4.5) by
  cascading from ``users``.
* **Append-only records** (ledger blocks, credentials, revocations, consent
  records, policy activations, audit log) can only be inserted. Database
  triggers created in the migration reject UPDATE, DELETE and TRUNCATE (R1.2,
  R4.2). These tables hold user UUIDs or pseudonymous DIDs only, never names
  or e-mail addresses, so deleting a user leaves nothing personal behind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

APPEND_ONLY_TABLES = ("ledger_blocks", "credentials", "credential_revocations",
                      "consent_records", "policy_activations", "audit_log")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


# --- people ------------------------------------------------------------------------

class School(Base):
    """A school or other institution. Its records decide who may learn and how."""
    __tablename__ = "schools"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    sign_in: Mapped[str] = mapped_column(String(8), default="local")  # sso | local
    region: Mapped[str] = mapped_column(String(8), default="IN")
    # youngest learners this school enrols: under13 | 13to17 | adult
    youngest_band: Mapped[str] = mapped_column(String(10), default="adult")
    allowed_tracks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # None = all
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str | None] = mapped_column(ForeignKey("schools.id"), index=True)
    idp_issuer: Mapped[str] = mapped_column(String(255))
    idp_subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(120))
    # learner | educator | school_admin | admin (platform operator)
    role: Mapped[str] = mapped_column(String(16))
    institution: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(8))
    adult_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # From school records, never self-declared: under13 | 13to17 | adult | unknown
    age_band: Mapped[str] = mapped_column(String(10), default="unknown",
                                          server_default="unknown")
    # The school confirms it holds a parent's or guardian's consent (minors only).
    guardian_ok: Mapped[bool] = mapped_column(Boolean, default=False,
                                              server_default=false())
    # random, so ledger entries cannot be linked back once the user is deleted
    subject_did: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    twins: Mapped[list[Twin]] = relationship(back_populates="user",
                                             cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (UniqueConstraint("idp_issuer", "idp_subject", name="uq_user_idp"),)


class Cohort(Base):
    __tablename__ = "cohorts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    track_id: Mapped[str | None] = mapped_column(String(60))  # the class's subject
    join_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("school_id", "name", name="uq_cohort_school_name"),)


class LocalCredential(Base):
    """Username and password for people at schools without single sign-on."""
    __tablename__ = "local_credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    must_change: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # bumped on every password change; sessions carrying an older value are refused
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Invite(Base):
    """One-time code that lets a school administrator or teacher create their account."""
    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CohortMember(Base):
    __tablename__ = "cohort_members"
    cohort_id: Mapped[str] = mapped_column(ForeignKey("cohorts.id", ondelete="CASCADE"),
                                           primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    member_role: Mapped[str] = mapped_column(String(16))  # learner | educator


# --- learning state -----------------------------------------------------------------

class Twin(Base):
    __tablename__ = "twins"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    track_id: Mapped[str] = mapped_column(String(60), primary_key=True,
                                          server_default="ml-foundations")
    p_known: Mapped[dict] = mapped_column(JSON)
    attention: Mapped[float] = mapped_column(Float, default=0.5)
    load: Mapped[float] = mapped_column(Float, default=0.5)
    steps_since_break: Mapped[int] = mapped_column(Integer, default=0)
    step: Mapped[int] = mapped_column(Integer, default=0)
    break_declined_at: Mapped[int] = mapped_column(Integer, default=-100)
    agent_state: Mapped[dict] = mapped_column(JSON, default=dict)
    pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="twins")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(String(60), server_default="ml-foundations")
    item_id: Mapped[str] = mapped_column(String(40))
    skill: Mapped[str] = mapped_column(String(60))
    difficulty: Mapped[int] = mapped_column(Integer)
    modality: Mapped[str] = mapped_column(String(40))
    correct: Mapped[bool] = mapped_column(Boolean)
    step: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[str] = mapped_column(String(60), server_default="ml-foundations")
    step: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    proposed_action: Mapped[str] = mapped_column(String(16))
    skill: Mapped[str | None] = mapped_column(String(60))
    difficulty: Mapped[int | None] = mapped_column(Integer)
    modality: Mapped[str | None] = mapped_column(String(40))
    item_id: Mapped[str | None] = mapped_column(String(40))
    policy_version: Mapped[str] = mapped_column(String(64))  # R5.3
    explanation: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)
    __table_args__ = (Index("ix_decisions_user_step", "user_id", "step"),)


class GuardianIntervention(Base):
    __tablename__ = "guardian_interventions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(String(60), server_default="ml-foundations")
    step: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(20))
    rule: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- consent (append-only) -----------------------------------------------------------

class ConsentRecord(Base):
    __tablename__ = "consent_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36))  # no FK: outlives deletion
    scope: Mapped[str] = mapped_column(String(32))  # learning | neural_sensing | guardian
    granted: Mapped[bool] = mapped_column(Boolean)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_text_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))  # learner | school | deletion
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_consent_user_scope", "user_id", "scope", "id"),)


# --- policies -----------------------------------------------------------------------

class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    track_id: Mapped[str] = mapped_column(String(60), server_default="ml-foundations")
    sha256: Mapped[str] = mapped_column(String(64))
    blob: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyActivation(Base):
    __tablename__ = "policy_activations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(ForeignKey("policy_versions.version"))
    track_id: Mapped[str] = mapped_column(String(60), server_default="ml-foundations")
    activated_by: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- keys and ledger ------------------------------------------------------------------

class Validator(Base):
    __tablename__ = "validators"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    stake: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SigningKey(Base):
    """Public half of every signing key ever used. Never deleted (R2.3)."""
    __tablename__ = "signing_keys"
    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    validator_id: Mapped[str] = mapped_column(ForeignKey("validators.id"))
    version: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(16))
    provider_ref: Mapped[str] = mapped_column(String(255))
    algorithm: Mapped[str] = mapped_column(String(16), default="ES256")
    public_key_pem: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10))  # active | retired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("validator_id", "version", name="uq_key_version"),)


class LedgerBlock(Base):
    __tablename__ = "ledger_blocks"
    index: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    prev_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    payload_hash: Mapped[str] = mapped_column(String(64))
    block_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_did: Mapped[str] = mapped_column(String(80), index=True)
    skill: Mapped[str] = mapped_column(String(60))
    body: Mapped[dict] = mapped_column(JSON)
    proofs: Mapped[list] = mapped_column(JSON)
    block_index: Mapped[int] = mapped_column(ForeignKey("ledger_blocks.index"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CredentialRevocation(Base):
    __tablename__ = "credential_revocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("credentials.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    revoked_by: Mapped[str] = mapped_column(String(36))
    block_index: Mapped[int] = mapped_column(ForeignKey("ledger_blocks.index"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- audit (append-only) ---------------------------------------------------------------

class AuditEvent(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(20))
    ip: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)
