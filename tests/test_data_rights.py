"""R4: consent records, export, deletion. Config safety for phase one."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from cognitive_twin.config import ConfigError, Settings
from cognitive_twin.db.models import Attempt, ConsentRecord, Credential, Decision, Twin, User
from cognitive_twin.db.session import engine_for, session_factory
from cognitive_twin.skills import load_track

from conftest import answer_correctly, consent, login


def test_consent_is_versioned_and_never_overwritten(client, settings):
    h = login(client, "asha")
    st = client.get("/api/me", headers=h).json()["consent"]
    assert not st["learning_granted"] and not st["neural_sensing_available"]
    client.post("/api/me/consent", json={"learning": True, "neural_sensing": True}, headers=h)
    client.post("/api/me/consent", json={"learning": True, "neural_sensing": False}, headers=h)
    hist = client.get("/api/me/consent/history", headers=h).json()
    assert len(hist) == 4
    assert [r["granted"] for r in hist if r["scope"] == "neural_sensing"] == [True, False]
    assert all(r["policy_version"] == settings.consent_policy_version and
               len(r["policy_text_sha256"]) == 64 for r in hist)


def test_new_policy_version_requires_renewed_consent(make_client):
    a = make_client()
    h = login(a, "asha")
    consent(a, h)
    assert a.post("/api/me/next", headers=h).status_code == 200
    b = make_client(consent_policy_version="2027-01-pilot-v2")
    b.cookies.update(a.cookies)
    st = b.get("/api/me", headers=h).json()["consent"]
    assert st["learning_needs_renewal"] and not st["learning_granted"]
    assert b.post("/api/me/next", headers=h).json()["code"] == "consent_required"


def test_neural_consent_is_stored_but_sensing_stays_off(client):
    h = login(client, "asha")
    client.post("/api/me/consent", json={"learning": True, "neural_sensing": True}, headers=h)
    me = client.get("/api/me", headers=h).json()
    assert me["consent"]["neural_sensing_granted"] is True
    assert me["sensing_active"] is False


def test_export_contains_my_data(client):
    h = login(client, "asha")
    consent(client, h)
    answer_correctly(client, h, load_track(), 6)
    r = client.get("/api/me/export", headers=h)
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["profile"]["email"] == "asha@example.edu"
    assert len(data["attempts"]) >= 5 and data["decisions"] and data["consent_history"]


def test_deletion_removes_personal_data_but_keeps_unlinkable_credentials(client, settings):
    track = load_track()
    h = login(client, "asha")
    consent(client, h)
    answer_correctly(client, h, track, 80)
    skill = client.get("/api/me", headers=h).json()["twin"]["mastered"][0]
    cred = client.post("/api/me/credentials", json={"skill": skill},
                       headers=h).json()["credential"]
    uid = client.get("/api/me", headers=h).json()["user"]["id"]

    assert client.post("/api/me/delete", json={"confirm": "yes"}, headers=h).status_code == 400
    assert client.post("/api/me/delete", json={"confirm": "delete my data"},
                       headers=h).status_code == 200
    assert client.get("/api/me", headers=h).status_code == 401

    with session_factory(engine_for(settings.database_url))() as db:
        assert db.get(User, uid) is None and db.get(Twin, (uid, "ml-foundations")) is None
        for model in (Attempt, Decision):
            assert db.scalar(select(func.count()).select_from(model)
                             .where(model.user_id == uid)) == 0
        # the credential stays, under a DID nothing links back to
        c = db.get(Credential, cred["id"])
        assert c is not None
        assert db.scalar(select(User).where(User.subject_did == c.subject_did)) is None
        # consent history records the withdrawal
        last = db.scalars(select(ConsentRecord).where(ConsentRecord.user_id == uid)
                          .order_by(ConsentRecord.id.desc())).first()
        assert last.source == "deletion" and last.granted is False
    assert client.get(f"/api/public/credentials/{cred['id']}/verify").json()["valid"]


def _prod(**kw) -> Settings:
    good = dict(env="production", database_url="postgresql+psycopg://u:p@db/ct",
                redis_url="redis://cache:6379/0", session_secret="x" * 48,
                oidc_issuer="https://idp.example.edu", oidc_client_id="ct",
                key_provider="aws-kms", dev_login=False, allowed_hosts=["learn.example.edu"],
                public_base_url="https://learn.example.edu")
    good.update(kw)
    return replace(Settings(), **good)


def test_production_config_accepts_a_proper_setup():
    _prod().validate()


@pytest.mark.parametrize("bad", [
    {"database_url": "sqlite:///x.db"}, {"key_provider": "local"},
    {"session_secret": "short"}, {"oidc_issuer": "", "local_accounts": False}, {"dev_login": True},
    {"allowed_hosts": ["*"]}, {"public_base_url": "http://learn.example.edu"},
    {"redis_url": ""}, {"neural_enabled": True},
])
def test_production_config_rejects_unsafe_settings(bad):
    with pytest.raises(ConfigError):
        _prod(**bad).validate()
