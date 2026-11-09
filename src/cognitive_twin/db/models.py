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

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idp_issuer: Mapped[str] = mapped_column(String(255))
    idp_subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16))  # learner | educator | admin
    institution: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(8))
    adult_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # random, so ledger entries cannot be linked back once the user is deleted
    subject_did: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    twin: Mapped[Twin | None] = relationship(back_populates="user", cascade="all, delete-orphan",
                                             passive_deletes=True)
    __table_args__ = (UniqueConstraint("idp_issuer", "idp_subject", name="uq_user_idp"),)


class Cohort(Base):
    __tablename__ = "cohorts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
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
    user: Mapped[User] = relationship(back_populates="twin")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
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
    scope: Mapped[str] = mapped_column(String(32))  # "learning" | "neural_sensing"
    granted: Mapped[bool] = mapped_column(Boolean)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_text_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))  # "learner" | "deletion" | ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_consent_user_scope", "user_id", "scope", "id"),)


# --- policies -----------------------------------------------------------------------

class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    blob: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyActivation(Base):
    __tablename__ = "policy_activations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(ForeignKey("policy_versions.version"))
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
