"""R3: identity, roles, authorisation, CSRF, rate limits, headers. R4.1/R4.4: eligibility."""

import pytest

from cognitive_twin.skills import load_track

from conftest import answer_correctly, consent, login

LEARNER_ROUTES = [("get", "/api/me"), ("post", "/api/me/next"), ("post", "/api/me/consent"),
                  ("get", "/api/me/export")]
EDUCATOR_ROUTES = [("get", "/api/educator/cohorts")]
ADMIN_ROUTES = [("get", "/api/admin/policies"), ("get", "/api/admin/audit"),
                ("get", "/api/admin/keys"), ("post", "/api/admin/keys/rotate")]


@pytest.mark.parametrize("method,path", LEARNER_ROUTES + EDUCATOR_ROUTES + ADMIN_ROUTES)
def test_every_private_route_requires_sign_in(client, method, path):
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize("method,path", EDUCATOR_ROUTES + ADMIN_ROUTES)
def test_learners_cannot_reach_staff_routes(client, method, path):
    h = login(client, "asha")
    r = getattr(client, method)(path, headers=h) if method == "get" else \
        client.post(path, json={}, headers=h)
    assert r.status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_educators_cannot_reach_admin_routes(client, method, path):
    h = login(client, "priya")
    r = getattr(client, method)(path, headers=h) if method == "get" else \
        client.post(path, json={}, headers=h)
    assert r.status_code == 403


def test_csrf_token_required_for_cookie_sessions(client):
    h = login(client, "asha")
    assert client.post("/api/me/consent", json={"learning": True}).status_code == 403
    assert client.post("/api/me/consent", json={"learning": True},
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/me/consent", json={"learning": True}, headers=h).status_code == 200


def test_tampered_session_cookie_is_rejected(client):
    login(client, "asha")
    token = client.cookies.get("ct_session")
    client.cookies.set("ct_session", token[:-3] + "AAA")
    assert client.get("/api/me").status_code == 401


def test_educator_sees_only_their_cohort(client):
    for p in ("asha", "ben", "chen"):  # asha, ben in Pilot A; chen in Pilot B
        h = login(client, p)
        consent(client, h)
    ids = {}
    h = login(client, "admin")
    for c in client.get("/api/educator/cohorts", headers=h).json():
        for lr in client.get(f"/api/educator/cohorts/{c['id']}/learners", headers=h).json():
            ids[lr["name"].split()[0]] = lr["id"]
    assert set(ids) == {"Asha", "Ben", "Chen"}

    h = login(client, "priya")
    cohorts = client.get("/api/educator/cohorts", headers=h).json()
    assert [c["name"] for c in cohorts] == ["ML Pilot A"]
    names = {x["name"].split()[0] for x in
             client.get(f"/api/educator/cohorts/{cohorts[0]['id']}/learners", headers=h).json()}
    assert names >= {"Asha", "Ben"} and "Chen" not in names
    assert client.get(f"/api/educator/learners/{ids['Asha']}", headers=h).status_code == 200
    # outside the cohort looks exactly like "doesn't exist"
    assert client.get(f"/api/educator/learners/{ids['Chen']}", headers=h).status_code == 404


def test_learner_and_educator_can_trigger_issuance_and_it_is_audited(client):
    track = load_track()
    h = login(client, "asha")
    consent(client, h)
    answer_correctly(client, h, track, 120)
    mastered = client.get("/api/me").json()["twin"]["mastered"]
    assert len(mastered) >= 2
    r = client.post("/api/me/credentials", json={"skill": mastered[0]}, headers=h).json()
    assert r["issued"]
    asha_id = client.get("/api/me").json()["user"]["id"]

    h = login(client, "priya")
    r = client.post(f"/api/educator/learners/{asha_id}/credentials",
                    json={"skill": mastered[1]}, headers=h).json()
    assert r["issued"]

    h = login(client, "admin")
    events = client.get("/api/admin/audit", headers=h).json()
    req = [e for e in events if e["action"] == "credential.request"]
    assert {e["role"] for e in req} == {"learner", "educator"}
    assert all(e["outcome"] == "issued" and e["request_id"] for e in req)


def test_public_verify_returns_only_what_is_needed(client):
    track = load_track()
    h = login(client, "asha")
    consent(client, h)
    answer_correctly(client, h, track, 80)
    skill = client.get("/api/me").json()["twin"]["mastered"][0]
    cred = client.post("/api/me/credentials", json={"skill": skill}, headers=h).json()
    client.cookies.clear()
    v = client.get(f"/api/public/credentials/{cred['credential']['id']}/verify").json()
    assert v["valid"] and v["status"] == "valid"
    assert set(v) == {"valid", "status", "reason", "checks", "credential"}
    assert set(v["credential"]) == {"id", "skill", "track", "issuer", "validFrom", "subject"}
    assert "Asha" not in str(v) and "@" not in str(v)


def test_minors_are_not_eligible(client):
    h = login(client, "dev-minor")
    r = client.post("/api/me/consent", json={"learning": True}, headers=h)
    assert r.status_code == 403 and r.json()["code"] == "not_eligible"
    assert client.post("/api/me/next", headers=h).json()["code"] == "not_eligible"


def test_region_comes_from_the_institution_and_drives_rules(client):
    h = login(client, "chen")
    me = client.get("/api/me", headers=h).json()
    assert me["user"]["region"] == "EU"
    assert me["sensing_active"] is False


def test_per_user_rate_limit(make_client):
    c = make_client(rate_user_per_min=5)
    h = login(c, "asha")
    codes = [c.get("/api/me", headers=h).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429


def test_per_ip_rate_limit_on_sign_in_only(make_client):
    c = make_client(rate_auth_per_min=3)
    codes = [c.post("/auth/dev-login", json={"persona": "asha"}).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[-1] == 429
    # checking who is signed in is not a sign-in attempt and must not lock people out
    assert all(c.get("/auth/session").status_code == 200 for _ in range(10))


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in r.headers["content-security-policy"].split("script-src")[1]\
        .split(";")[0]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/public/keys").headers["cache-control"] == "no-store"


def test_invalid_input_is_rejected(client):
    h = login(client, "asha")
    consent(client, h)
    assert client.post("/api/me/answer", json={"item_id": "x'; drop table", "choice": 1},
                       headers=h).status_code == 422
    assert client.post("/api/me/answer", json={"item_id": "vec-1", "choice": 9},
                       headers=h).status_code == 422


def test_concurrent_first_sign_ins_to_a_new_cohort(settings):
    """Many learners from a brand-new cohort signing in at once must all succeed."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func, select

    from cognitive_twin.auth.oidc import identity_from_claims
    from cognitive_twin.auth.provision import upsert_user
    from cognitive_twin.db.models import Cohort
    from cognitive_twin.db.session import (
        create_all_for_tests,
        engine_for,
        session_factory,
        transaction,
    )
    eng = engine_for(settings.database_url)
    create_all_for_tests(eng)
    sessions = session_factory(eng)

    def sign_in(i):
        claims = {"iss": "https://idp", "sub": f"u{i}", "name": f"L{i}", "ct_region": "IN",
                  "ct_adult_verified": True, "ct_cohorts": ["Brand New Cohort"]}
        with transaction(sessions) as db:
            upsert_user(db, identity_from_claims(claims, settings))
        return True

    with ThreadPoolExecutor(8) as pool:
        assert all(pool.map(sign_in, range(16)))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Cohort)
                         .where(Cohort.name == "Brand New Cohort")) == 1
