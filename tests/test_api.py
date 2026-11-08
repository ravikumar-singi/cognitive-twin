from fastapi.testclient import TestClient

from cognitive_twin.api import app

client = TestClient(app)


def enrol(**kw):
    body = {"name": "Test", "age": 22, "region": "IN", "neural_opt_in": True, **kw}
    r = client.post("/api/learners", json=body)
    return r


def test_dashboard_and_health():
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"


def test_minor_without_guardian_consent_is_rejected():
    assert enrol(age=15, guardian_consent=False).status_code == 403


def test_eu_learner_gets_no_affect_inference():
    r = enrol(region="EU").json()
    assert r["sensing_active"] is False


def test_human_answer_flow():
    sid = enrol(simulated=False).json()["id"]
    d = client.post(f"/api/learners/{sid}/next").json()
    if d["action"] == "break":  # learners may decline a suggested break
        d = client.post(f"/api/learners/{sid}/next", json={"skip_break": True}).json()
    assert d["action"] == "teach" and d["explanation"]["reasons"]
    assert "answer" not in d["item"]  # the client never sees the key
    r = client.post(f"/api/learners/{sid}/answer", json={"item_id": d["item"]["id"], "choice": 0})
    assert r.status_code == 200 and "correct" in r.json()
    body = {"item_id": d["item"]["id"], "choice": 0}
    stale = client.post(f"/api/learners/{sid}/answer", json=body)
    assert stale.status_code == 409


def test_autoplay_to_credential_and_verify():
    sid = enrol().json()["id"]
    view = client.post(f"/api/learners/{sid}/autoplay", json={"steps": 120}).json()
    mastered = view["twin"]["mastered"]
    assert mastered, "a trained policy should master at least one skill in 120 steps"
    r = client.post(f"/api/learners/{sid}/credentials", json={"skill": mastered[0]}).json()
    if r["issued"]:
        v = client.get(f"/api/credentials/{r['credential']['id']}/verify").json()
        assert v["verification"]["valid"]
    assert client.get("/api/ledger").json()["chain_intact"]
    assert client.get("/api/policy").json()["tutor_arm_pulls"]


def test_human_learners_are_not_nagged_with_breaks():
    sid = enrol(simulated=False).json()["id"]
    actions = []
    for _ in range(10):
        d = client.post(f"/api/learners/{sid}/next").json()
        actions.append(d["action"])
        if d["action"] == "teach":
            body = {"item_id": d["item"]["id"], "choice": 0}
            client.post(f"/api/learners/{sid}/answer", json=body)
        else:
            break
    assert "break" not in actions[:8]
