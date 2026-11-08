"""Proof-of-Capability ledger with a validator DAO.

A credential is minted only when validators holding at least two thirds of
total stake independently review the evidence and sign it. Validators that
vote against the final outcome are slashed. Credentials follow the shape of
W3C Verifiable Credentials 2.0 and are anchored in a hash-chained,
append-only log.

Evidence is built from answer history only. It never contains neural
features: those shape teaching, never grading.

MVP limits, stated plainly: the chain is a single-process log rather than a
distributed L2, and the evidence uses a salted hash commitment where the
roadmap calls for zero-knowledge proofs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..twin import MASTERY, CognitiveTwin

QUORUM = 2 / 3
SLASH_RATE = 0.10
NEURAL_KEYS = {"attention", "cognitive_load", "load", "signal_quality", "eeg", "fnirs"}


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def learner_did(learner_id: str) -> str:
    return "did:ct:learner:" + sha256(learner_id.encode())[:24]


# --- evidence --------------------------------------------------------------------

@dataclass
class Evidence:
    subject: str
    skill: str
    attempts: list[dict]
    belief: float
    salt: str = field(default_factory=lambda: secrets.token_hex(16))

    @classmethod
    def from_twin(cls, twin: CognitiveTwin, skill: str, window: int = 6) -> Evidence:
        attempts = [
            {"item_id": a.item_id, "difficulty": a.difficulty, "correct": a.correct,
             "step": a.step}
            for a in twin.recent_for(skill, window)
        ]
        ev = cls(learner_did(twin.learner_id), skill, attempts, round(twin.p_known[skill], 4))
        ev.assert_no_neural_data()
        return ev

    def assert_no_neural_data(self) -> None:
        keys = {k for a in self.attempts for k in a}
        leaked = keys & NEURAL_KEYS
        if leaked:
            raise ValueError(f"neural fields must never enter evidence: {leaked}")

    def commitment(self) -> str:
        return sha256(canonical({"salt": self.salt, "skill": self.skill,
                                 "attempts": self.attempts, "belief": self.belief}))


def meets_rubric(ev: Evidence) -> tuple[bool, str]:
    if ev.belief < MASTERY:
        return False, f"belief {ev.belief:.2f} below mastery {MASTERY}"
    hard_correct = sum(1 for a in ev.attempts if a["correct"] and a["difficulty"] >= 2)
    if hard_correct < 3:
        return False, f"only {hard_correct} correct answers at difficulty 2+ (need 3)"
    accuracy = sum(a["correct"] for a in ev.attempts) / max(1, len(ev.attempts))
    if accuracy < 2 / 3:
        return False, f"recent accuracy {accuracy:.0%} below 67%"
    return True, "rubric met"


# --- validators ----------------------------------------------------------------

@dataclass
class Validator:
    name: str
    stake: float
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate, repr=False)
    always_approve: bool = False  # simulates a careless or captured validator

    @property
    def did(self) -> str:
        pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return "did:ct:validator:" + pub.hex()[:24]

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.key.public_key()

    def review(self, ev: Evidence) -> tuple[bool, str]:
        return (True, "approved without review") if self.always_approve else meets_rubric(ev)

    def sign(self, payload: bytes) -> str:
        return self.key.sign(payload).hex()


# --- ledger ------------------------------------------------------------------------

@dataclass
class IssueResult:
    issued: bool
    reason: str
    votes: list[dict]
    credential: dict | None = None
    slashed: list[str] = field(default_factory=list)


@dataclass
class Ledger:
    validators: list[Validator]
    blocks: list[dict] = field(default_factory=list)
    credentials: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.blocks:
            self.blocks.append({"index": 0, "prev_hash": "0" * 64, "timestamp": 0,
                                "kind": "genesis", "payload_hash": sha256(b"cognitive-twin")})

    @classmethod
    def with_default_dao(cls) -> Ledger:
        return cls([
            Validator("Lighthouse University A", 40),
            Validator("Lighthouse University B", 35),
            Validator("Professional Licensing Body", 30),
            Validator("Employer Consortium", 25),
        ])

    def _block_hash(self, block: dict) -> str:
        return sha256(canonical(block))

    def _append(self, kind: str, payload: dict) -> dict:
        prev = self.blocks[-1]
        block = {"index": prev["index"] + 1, "prev_hash": self._block_hash(prev),
                 "timestamp": int(time.time()), "kind": kind,
                 "payload_hash": sha256(canonical(payload))}
        self.blocks.append(block)
        return block

    @property
    def total_stake(self) -> float:
        return sum(v.stake for v in self.validators)

    def request_credential(self, twin: CognitiveTwin, skill: str) -> IssueResult:
        ev = Evidence.from_twin(twin, skill)
        votes = []
        for v in self.validators:
            ok, why = v.review(ev)
            votes.append({"validator": v.name, "did": v.did, "stake": v.stake,
                          "approve": ok, "reason": why})
        approving = sum(x["stake"] for x in votes if x["approve"])
        issued = approving >= QUORUM * self.total_stake
        slashed = []
        for v, vote in zip(self.validators, votes, strict=True):
            if vote["approve"] != issued:
                v.stake = round(v.stake * (1 - SLASH_RATE), 2)
                slashed.append(v.name)
        if not issued:
            reason = next((x["reason"] for x in votes if not x["approve"]), "quorum not met")
            return IssueResult(False, reason, votes, None, slashed)
        cred = self._mint(twin, skill, ev, [v for v, x in zip(self.validators, votes, strict=True)
                                            if x["approve"]])
        return IssueResult(True, "quorum reached", votes, cred, slashed)

    def _mint(self, twin: CognitiveTwin, skill: str, ev: Evidence,
              signers: list[Validator]) -> dict:
        s = twin.track.skills[skill]
        body = {
            "@context": ["https://www.w3.org/ns/credentials/v2"],
            "type": ["VerifiableCredential", "ProofOfCapabilityCredential"],
            "id": "urn:ct:cred:" + secrets.token_hex(8),
            "issuer": "did:ct:dao",
            "validFrom": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "credentialSubject": {
                "id": ev.subject, "skill": skill, "name": s.name, "rubric": s.rubric,
                "track": twin.track.name, "evidenceCommitment": ev.commitment(),
            },
            "attestation": {
                "threshold": round(QUORUM, 4),
                "totalStake": self.total_stake,
                "signers": [{"did": v.did, "stake": v.stake} for v in signers],
            },
        }
        payload = canonical(body)
        cred = dict(body)
        cred["proof"] = [
            {"type": "Ed25519Signature2020", "verificationMethod": v.did,
             "publicKeyHex": v.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
             "proofValue": v.sign(payload)}
            for v in signers
        ]
        block = self._append("credential", body)
        cred["ledgerAnchor"] = {"block": block["index"], "payloadHash": block["payload_hash"]}
        self.credentials[body["id"]] = cred
        return cred

    # --- verification ------------------------------------------------------------

    def verify_chain(self) -> bool:
        return all(self.blocks[i]["prev_hash"] == self._block_hash(self.blocks[i - 1])
                   for i in range(1, len(self.blocks)))

    def verify_credential(self, cred: dict) -> dict:
        body = {k: v for k, v in cred.items() if k not in ("proof", "ledgerAnchor")}
        payload = canonical(body)
        att = body.get("attestation", {})
        stake_of = {s["did"]: s["stake"] for s in att.get("signers", [])}
        valid_stake = 0.0
        for p in cred.get("proof", []):
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(p["publicKeyHex"])).verify(
                    bytes.fromhex(p["proofValue"]), payload)
                valid_stake += stake_of.get(p["verificationMethod"], 0.0)
            except (InvalidSignature, ValueError):
                pass
        anchor = cred.get("ledgerAnchor", {})
        idx = anchor.get("block", -1)
        anchored = (0 < idx < len(self.blocks)
                    and self.blocks[idx]["payload_hash"] == sha256(payload))
        known = {v.did for v in self.validators}  # signers must be DAO members
        signers_known = all(p["verificationMethod"] in known for p in cred.get("proof", []))
        quorum = valid_stake >= att.get("threshold", QUORUM) * att.get("totalStake", float("inf"))
        ok = anchored and signers_known and quorum and self.verify_chain()
        return {"valid": ok, "anchored": anchored, "signers_known": signers_known,
                "signed_stake": valid_stake, "chain_intact": self.verify_chain()}
