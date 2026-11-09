"""R1 persistence and statelessness, R2 key durability, R5.3 policy versions."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cognitive_twin.db.session import engine_for
from cognitive_twin.skills import load_track

from conftest import answer_correctly, consent, login


def test_progress_survives_restart_and_any_instance_can_serve(make_client):
    a = make_client()
    h = login(a, "asha")
    consent(a, h)
    answer_correctly(a, h, load_track(), 5)
    q = a.post("/api/me/next", headers=h).json()
    before = a.get("/api/me", headers=h).json()["twin"]

    b = make_client()  # a second instance, as after a restart or behind a load balancer
    b.cookies.update(a.cookies)
    after = b.get("/api/me", headers=h).json()
    assert after["twin"]["p_known"] == before["p_known"]
    assert after["twin"]["step"] == before["step"]
    # the open question is resumed, not replaced
    assert b.post("/api/me/next", headers=h).json() == q


def test_restart_and_rotate_credential_still_verifies(make_client, settings):
    """R2.5 acceptance test."""
    track = load_track()
    a = make_client()
    h = login(a, "asha")
    consent(a, h)
    answer_correctly(a, h, track, 100)
    skills = a.get("/api/me", headers=h).json()["twin"]["mastered"]
    old = a.post("/api/me/credentials", json={"skill": skills[0]}, headers=h).json()
    assert old["issued"]
    old_id = old["credential"]["id"]
    old_kids = {p["kid"] for p in old["credential"]["proof"]}

    b = make_client()  # restart every instance
    h = login(b, "admin")
    validators = b.get("/api/admin/validators", headers=h).json()
    for v in validators:  # rotate every validator's key
        r = b.post("/api/admin/keys/rotate", json={"validator_id": v["id"]}, headers=h)
        assert r.status_code == 200 and r.json()["version"] == 2
    keys = b.get("/api/public/keys").json()
    assert {k["kid"] for k in keys if k["status"] == "retired"} == old_kids

    c = make_client()  # and restart again
    v = c.get(f"/api/public/credentials/{old_id}/verify").json()
    assert v["valid"], v

    h = login(c, "asha")
    new = c.post("/api/me/credentials", json={"skill": skills[1]}, headers=h).json()
    assert {p["keyVersion"] for p in new["credential"]["proof"]} == {2}
    assert c.get(f"/api/public/credentials/{new['credential']['id']}/verify").json()["valid"]


def test_revocation_is_reported_with_reason(client):
    track = load_track()
    h = login(client, "asha")
    consent(client, h)
    answer_correctly(client, h, track, 80)
    skill = client.get("/api/me", headers=h).json()["twin"]["mastered"][0]
    cid = client.post("/api/me/credentials", json={"skill": skill},
                      headers=h).json()["credential"]["id"]
    # a learner can't revoke
    assert client.post(f"/api/educator/credentials/{cid}/revoke",
                       json={"reason": "testing"}, headers=h).status_code == 403
    h = login(client, "priya")
    r = client.post(f"/api/educator/credentials/{cid}/revoke",
                    json={"reason": "issued during a proctoring failure"}, headers=h)
    assert r.status_code == 200
    v = client.get(f"/api/public/credentials/{cid}/verify").json()
    assert not v["valid"] and v["status"] == "revoked"
    assert v["reason"] == "issued during a proctoring failure"
    h = login(client, "asha")  # the learner can now earn it again
    again = client.post("/api/me/credentials", json={"skill": skill}, headers=h).json()
    assert again["issued"]


@pytest.mark.parametrize("table", ["ledger_blocks", "credentials", "consent_records",
                                   "audit_log", "policy_activations"])
def test_append_only_tables_reject_updates_and_deletes(client, settings, table):
    h = login(client, "asha")
    consent(client, h)
    if table == "credentials":  # needs a row for the row-level trigger to fire on
        answer_correctly(client, h, load_track(), 80)
        skill = client.get("/api/me", headers=h).json()["twin"]["mastered"][0]
        assert client.post("/api/me/credentials", json={"skill": skill},
                           headers=h).json()["issued"]
    eng = engine_for(settings.database_url)
    for stmt in (f"UPDATE {table} SET created_at = created_at",
                 f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError), eng.begin() as conn:
            conn.execute(text(stmt))
    if eng.dialect.name == "postgresql":
        with pytest.raises(DBAPIError), eng.begin() as conn:
            conn.execute(text(f"TRUNCATE {table} CASCADE"))


def test_tampered_chain_fails_readiness(make_client, settings):
    eng = engine_for(settings.database_url)
    a = make_client()
    h = login(a, "admin")
    assert a.get("/api/admin/ledger/verify", headers=h).json()["chain_intact"]
    with eng.begin() as conn:  # an attacker with raw DB access disables the guard
        if eng.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE ledger_blocks DISABLE TRIGGER USER"))
        else:
            conn.execute(text("DROP TRIGGER ledger_blocks_no_update"))
        conn.execute(text("UPDATE ledger_blocks SET payload_hash = 'x' WHERE \"index\" = 0"))
    b = make_client()  # the startup check catches it
    r = b.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["ledger_chain"] is False


def test_policy_versions_are_recorded_and_revertible(client):
    import json
    from importlib import resources
    h = login(client, "admin")
    pols = client.get("/api/admin/policies", headers=h).json()
    original = next(p["version"] for p in pols if p["active"])
    blob = json.loads(resources.files("cognitive_twin").joinpath("content/policy.json")
                      .read_text())
    blob["pacing_q"]["f0-a1"] = [0.0, 9.9]  # a deliberately different policy
    new = client.post("/api/admin/policies", json=blob, headers=h).json()["version"]
    assert new != original
    assert client.post(f"/api/admin/policies/{new}/activate", json={"reason": "release"},
                       headers=h).status_code == 200

    h2 = login(client, "asha")
    consent(client, h2)
    d = client.post("/api/me/next", headers=h2).json()
    assert d["policy_version"] == new

    h = login(client, "admin")
    client.post(f"/api/admin/policies/{original}/activate", json={"reason": "revert"},
                headers=h)
    h2 = login(client, "asha")
    client.post("/api/me/answer", json={"item_id": d["item"]["id"], "choice": 0}, headers=h2)
    assert client.post("/api/me/next", headers=h2).json()["policy_version"] == original
    log = client.get("/api/me", headers=h2).json()["log"]
    assert {e["policy_version"] for e in log} == {new, original}


@pytest.mark.skipif(not __import__("os").environ.get("TEST_DATABASE_URL"),
                    reason="needs PostgreSQL")
def test_app_role_cannot_update_or_delete_ledger_rows(client, settings):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from cognitive_twin.db.roles import APP_ROLE, create_app_role
    create_app_role(engine_for(settings.database_url), "app-pass-1234567890")
    app_url = make_url(settings.database_url).set(username=APP_ROLE, password="app-pass-1234567890")
    eng = create_engine(app_url)
    with eng.begin() as conn:  # the role can work normally...
        conn.execute(text("SELECT count(*) FROM ledger_blocks"))
        conn.execute(text("INSERT INTO audit_log(action, outcome, details, created_at) "
                          "VALUES ('t', 'ok', '{}', now())"))
    for stmt in ("UPDATE ledger_blocks SET kind = kind", "DELETE FROM credentials",
                 "ALTER TABLE ledger_blocks DISABLE TRIGGER USER"):
        with pytest.raises(DBAPIError) as e, eng.begin() as conn:  # ...but not tamper
            conn.execute(text(stmt))
        assert "permission denied" in str(e.value) or "must be owner" in str(e.value)
    eng.dispose()


@pytest.mark.skipif(not __import__("os").environ.get("TEST_DATABASE_URL"),
                    reason="needs PostgreSQL")
def test_migrations_match_the_models_exactly(settings):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from cognitive_twin.db.models import Base
    with engine_for(settings.database_url).connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_writes_are_committed_before_the_response_is_sent(client):
    """Read-your-writes: a client acting on a response must see what it reported."""
    from fastapi.params import Depends as DependsParam

    from cognitive_twin.deps import get_db
    for route in client.app.routes:
        dep = getattr(route, "dependant", None)
        stack = list(dep.dependencies) if dep else []
        while stack:
            d = stack.pop()
            stack.extend(d.dependencies)
            if d.call is get_db:
                assert d.scope == "function", f"{route.path} commits after responding"
    assert DependsParam  # imported for clarity of intent


def test_cancelled_requests_do_not_leak_connections(settings):
    """A client that disconnects mid-request must not leave its connection checked out."""
    import anyio

    from cognitive_twin.app import create_app
    from cognitive_twin.deps import get_db
    app = create_app(settings)
    eng = app.state.ct.engine
    request = type("R", (), {"app": app})()

    async def cancelled_request():
        gen = get_db(request)
        db = await gen.__anext__()
        db.execute(text("SELECT 1"))  # holds a connection inside a transaction
        with anyio.CancelScope() as scope:
            scope.cancel()
            try:
                await gen.athrow(anyio.get_cancelled_exc_class()())
            except BaseException:  # noqa: BLE001 - the cancellation propagates
                pass

    checked_out_before = eng.pool.checkedout()
    anyio.run(cancelled_request)
    assert eng.pool.checkedout() == checked_out_before
