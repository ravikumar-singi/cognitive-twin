"""Database-backed Proof-of-Capability ledger (requirements R1.2 and R2).

* Blocks, credentials and revocations are append-only rows (triggers reject
  UPDATE, DELETE and TRUNCATE).
* Appends take a transaction-scoped advisory lock on PostgreSQL, so several
  service instances can issue concurrently without forking the chain.
* Each credential records, for every signature, the validator, key id and key
  version that produced it (R2.2). Verification uses the stored public keys
  only, so it survives restarts, key rotation and KMS outages (R2.3, R2.5).
* Revocation appends a revocation block; verify reports it with the reason (R2.4).

Phase-one limit, stated plainly: all validators' keys are operated by this
service. Phase two moves each validator onto its own infrastructure.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..db.models import (
    Credential,
    CredentialRevocation,
    LedgerBlock,
    SigningKey,
    User,
    Validator,
    utcnow,
)
from ..keys import KeyRegistry, b64url, b64url_decode, verify_signature
from ..twin import CognitiveTwin
from .ledger import QUORUM, SLASH_RATE, Evidence, canonical, meets_rubric, sha256

CREDENTIAL_REQUESTS = Counter("ct_credential_requests_total", "Credential requests", ["outcome"])
CREDENTIALS_ISSUED = Counter("ct_credentials_issued_total", "Credentials issued")
CREDENTIALS_REVOKED = Counter("ct_credentials_revoked_total", "Credentials revoked")
LEDGER_CHAIN_OK = Gauge("ct_ledger_chain_ok", "1 if the last full chain check passed, else 0")
LEDGER_LOCK_ID = 724_311  # arbitrary, stable advisory-lock key

PROOF_TYPE = "EcdsaP256Sha256Signature"  # project-specific; not a W3C cryptosuite yet

DEFAULT_VALIDATORS = [
    ("lighthouse-university-a", "Lighthouse University A", 40.0),
    ("lighthouse-university-b", "Lighthouse University B", 35.0),
    ("professional-licensing-body", "Professional Licensing Body", 30.0),
    ("employer-consortium", "Employer Consortium", 25.0),
]


def block_hash(index: int, prev_hash: str, kind: str, payload_hash: str) -> str:
    return sha256(canonical({"index": index, "prev_hash": prev_hash, "kind": kind,
                             "payload_hash": payload_hash}))


@dataclass
class IssueOutcome:
    issued: bool
    reason: str
    votes: list[dict] = field(default_factory=list)
    slashed: list[str] = field(default_factory=list)
    credential: dict | None = None


class LedgerService:
    def __init__(self, db: Session, keys: KeyRegistry):
        self.db = db
        self.keys = keys

    # --- setup -------------------------------------------------------------------------

    def ensure_genesis(self) -> None:
        if self.db.get(LedgerBlock, 0) is None:
            ph = sha256(b"cognitive-twin-genesis")
            self.db.add(LedgerBlock(index=0, prev_hash="0" * 64, kind="genesis", payload_hash=ph,
                                    block_hash=block_hash(0, "0" * 64, "genesis", ph)))
            self.db.flush()

    def bootstrap_dev_validators(self) -> None:
        """Development only: create the default validators with local keys."""
        for vid, name, stake in DEFAULT_VALIDATORS:
            if self.db.get(Validator, vid) is None:
                self.db.add(Validator(id=vid, name=name, stake=stake))
                self.db.flush()
            has_key = self.db.scalar(select(SigningKey).where(SigningKey.validator_id == vid,
                                                              SigningKey.status == "active"))
            if has_key is None:
                self.keys.rotate(vid)

    # --- chain ---------------------------------------------------------------------------

    def _lock(self) -> None:
        if self.db.bind.dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LEDGER_LOCK_ID})

    def append(self, kind: str, payload: dict) -> LedgerBlock:
        self._lock()
        head = self.db.scalar(select(LedgerBlock).order_by(LedgerBlock.index.desc()).limit(1))
        if head is None:
            self.ensure_genesis()
            head = self.db.get(LedgerBlock, 0)
        ph = sha256(canonical(payload))
        idx = head.index + 1
        block = LedgerBlock(index=idx, prev_hash=head.block_hash, kind=kind, payload_hash=ph,
                            block_hash=block_hash(idx, head.block_hash, kind, ph))
        self.db.add(block)
        self.db.flush()
        return block

    def verify_chain(self) -> tuple[bool, int | None]:
        prev: LedgerBlock | None = None
        ok, bad = True, None
        for b in self.db.scalars(select(LedgerBlock).order_by(LedgerBlock.index)):
            expected_prev = prev.block_hash if prev else "0" * 64
            if (b.prev_hash != expected_prev
                    or b.block_hash != block_hash(b.index, b.prev_hash, b.kind, b.payload_hash)
                    or (prev is not None and b.index != prev.index + 1)):
                ok, bad = False, b.index
                break
            prev = b
        LEDGER_CHAIN_OK.set(1 if ok else 0)
        return ok, bad

    def block_count(self) -> int:
        return int(self.db.scalar(select(func.count()).select_from(LedgerBlock)) or 0)

    # --- issuance --------------------------------------------------------------------------

    def issue(self, user: User, twin: CognitiveTwin, skill: str) -> IssueOutcome:
        ev = Evidence.from_twin(twin, skill, subject=user.subject_did)
        validators = self.db.scalars(select(Validator).order_by(Validator.id)).all()
        if not validators:
            raise RuntimeError("no validators are registered")
        total = sum(v.stake for v in validators)
        votes, approving = [], []
        for v in validators:
            ok, why = meets_rubric(ev)
            votes.append({"validator": v.name, "approve": ok, "reason": why, "stake": v.stake})
            if ok:
                approving.append(v)
        issued = sum(v.stake for v in approving) >= QUORUM * total
        slashed = []
        for v, vote in zip(validators, votes, strict=True):
            if vote["approve"] != issued:
                v.stake = round(v.stake * (1 - SLASH_RATE), 2)
                slashed.append(v.name)
        if not issued:
            CREDENTIAL_REQUESTS.labels("refused").inc()
            reason = next((x["reason"] for x in votes if not x["approve"]), "quorum not met")
            return IssueOutcome(False, reason, votes, slashed)

        s = twin.track.skills[skill]
        signer_keys = [(v, self.keys.active_key(v.id)) for v in approving]
        body = {
            "@context": ["https://www.w3.org/ns/credentials/v2"],
            "type": ["VerifiableCredential", "ProofOfCapabilityCredential"],
            "id": "urn:ct:cred:" + secrets.token_hex(12),
            "issuer": "did:ct:dao",
            "validFrom": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "credentialSubject": {
                "id": user.subject_did, "skill": skill, "name": s.name, "rubric": s.rubric,
                "track": twin.track.name, "evidenceCommitment": ev.commitment(),
            },
            "attestation": {
                "threshold": round(QUORUM, 4),
                "totalStake": total,
                "signers": [{"validator": v.id, "kid": k.kid, "keyVersion": k.version,
                             "stake": v.stake} for v, k in signer_keys],
            },
        }
        payload = canonical(body)
        proofs = [{"type": PROOF_TYPE, "validator": v.id, "kid": k.kid, "keyVersion": k.version,
                   "proofValue": b64url(self.keys.sign(k, payload))} for v, k in signer_keys]
        block = self.append("credential", body)
        self.db.add(Credential(id=body["id"], subject_did=user.subject_did, skill=skill,
                               body=body, proofs=proofs, block_index=block.index))
        self.db.flush()
        CREDENTIAL_REQUESTS.labels("issued").inc()
        CREDENTIALS_ISSUED.inc()
        return IssueOutcome(True, "quorum reached", votes, slashed,
                            self.render(self.db.get(Credential, body["id"])))

    def revoke(self, credential_id: str, reason: str, actor_id: str) -> None:
        cred = self.db.get(Credential, credential_id)
        if cred is None:
            raise KeyError(credential_id)
        if self.revocation(credential_id) is not None:
            return
        block = self.append("revocation", {"credential": credential_id, "reason": reason})
        self.db.add(CredentialRevocation(credential_id=credential_id, reason=reason,
                                         revoked_by=actor_id, block_index=block.index))
        self.db.flush()
        CREDENTIALS_REVOKED.inc()

    def revocation(self, credential_id: str) -> CredentialRevocation | None:
        return self.db.scalar(select(CredentialRevocation)
                              .where(CredentialRevocation.credential_id == credential_id))

    # --- reading and verification ------------------------------------------------------------

    def render(self, cred: Credential) -> dict:
        out = dict(cred.body)
        out["proof"] = cred.proofs
        out["ledgerAnchor"] = {"block": cred.block_index,
                               "payloadHash": sha256(canonical(cred.body))}
        return out

    def credentials_for(self, subject_did: str) -> list[dict]:
        rows = self.db.scalars(select(Credential).where(Credential.subject_did == subject_did)
                               .order_by(Credential.created_at)).all()
        out = []
        for c in rows:
            r = self.render(c)
            rev = self.revocation(c.id)
            r["status"] = "revoked" if rev else "active"
            out.append(r)
        return out

    def verify(self, credential_id: str) -> dict | None:
        """Public verification: just enough to decide whether to trust it (R3.5)."""
        cred = self.db.get(Credential, credential_id)
        if cred is None:
            return None
        return self.verify_document(cred.body, cred.proofs, cred.block_index, credential_id)

    def verify_document(self, body: dict, proofs: list, block_index: int,
                        credential_id: str | None = None) -> dict:
        payload = canonical(body)
        att = body.get("attestation", {})
        stake_of = {s["kid"]: s["stake"] for s in att.get("signers", [])}
        signed_stake, bad_sigs = 0.0, 0
        for p in proofs:
            key = self.keys.get(p.get("kid", ""))
            if (key is None or key.validator_id != p.get("validator")
                    or key.version != p.get("keyVersion")):
                bad_sigs += 1
                continue
            try:
                ok = verify_signature(key.public_key_pem, b64url_decode(p["proofValue"]), payload)
            except (ValueError, KeyError):
                ok = False
            if ok:
                signed_stake += stake_of.get(key.kid, 0.0)
            else:
                bad_sigs += 1
        quorum = signed_stake >= att.get("threshold", QUORUM) * att.get("totalStake",
                                                                          float("inf"))
        block = self.db.get(LedgerBlock, block_index)
        prev = self.db.get(LedgerBlock, block_index - 1) if block else None
        anchored = bool(block and prev and block.kind == "credential"
                        and block.payload_hash == sha256(payload)
                        and block.prev_hash == prev.block_hash
                        and block.block_hash == block_hash(block.index, block.prev_hash,
                                                           block.kind, block.payload_hash))
        rev = self.revocation(credential_id) if credential_id else None
        checks = {"signatures": bad_sigs == 0 and bool(proofs), "quorum": quorum,
                  "anchored": anchored}
        if rev is not None:
            status, reason = "revoked", rev.reason
        elif all(checks.values()):
            status, reason = "valid", "signatures, quorum and ledger anchor all check out"
        else:
            status = "invalid"
            reason = "failed: " + ", ".join(k for k, v in checks.items() if not v)
        subj = body.get("credentialSubject", {})
        return {
            "valid": status == "valid", "status": status, "reason": reason, "checks": checks,
            "credential": {"id": body.get("id"), "skill": subj.get("name"),
                           "track": subj.get("track"), "issuer": body.get("issuer"),
                           "validFrom": body.get("validFrom"), "subject": subj.get("id")},
        }
