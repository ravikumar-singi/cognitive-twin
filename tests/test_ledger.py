import copy

import pytest

from cognitive_twin.ledger.ledger import Evidence, Ledger, Validator
from cognitive_twin.skills import load_track
from cognitive_twin.twin import CognitiveTwin


def mastered_twin(skill="vectors"):
    twin = CognitiveTwin("alice", load_track())
    for item, d in [("vec-3", 2), ("vec-4", 2), ("vec-5", 3), ("vec-6", 3), ("vec-3", 2)]:
        twin.observe_answer(item, skill, d, "worked_example", True)
    assert twin.p_known[skill] >= 0.95
    return twin


def test_credential_issued_signed_and_verifiable():
    ledger = Ledger.with_default_dao()
    res = ledger.request_credential(mastered_twin(), "vectors")
    assert res.issued and not res.slashed
    v = ledger.verify_credential(res.credential)
    assert v["valid"] and v["chain_intact"]
    assert res.credential["credentialSubject"]["id"].startswith("did:ct:learner:")
    assert "alice" not in str(res.credential)  # pseudonymous subject


def test_refused_without_evidence():
    ledger = Ledger.with_default_dao()
    res = ledger.request_credential(CognitiveTwin("bob", load_track()), "vectors")
    assert not res.issued and res.credential is None


def test_tampered_credential_fails_verification():
    ledger = Ledger.with_default_dao()
    cred = ledger.request_credential(mastered_twin(), "vectors").credential
    forged = copy.deepcopy(cred)
    forged["credentialSubject"]["skill"] = "overfitting"
    assert not ledger.verify_credential(forged)["valid"]


def test_careless_validator_is_outvoted_and_slashed():
    careless = Validator("Rubber Stamp Inc", 30, always_approve=True)
    ledger = Ledger([Validator("A", 40), Validator("B", 35), careless])
    res = ledger.request_credential(CognitiveTwin("carol", load_track()), "vectors")
    assert not res.issued
    assert res.slashed == ["Rubber Stamp Inc"] and careless.stake == 27.0


def test_evidence_never_contains_neural_fields():
    ev = Evidence.from_twin(mastered_twin(), "vectors")
    assert all(set(a) == {"item_id", "difficulty", "correct", "step"} for a in ev.attempts)
    ev.attempts[0]["attention"] = 0.9
    with pytest.raises(ValueError):
        ev.assert_no_neural_data()


def test_chain_detects_rewritten_history():
    ledger = Ledger.with_default_dao()
    ledger.request_credential(mastered_twin(), "vectors")
    ledger.request_credential(mastered_twin(), "vectors")
    ledger.blocks[1]["payload_hash"] = "0" * 64
    assert not ledger.verify_chain()
