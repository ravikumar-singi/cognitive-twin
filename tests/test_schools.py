"""Schools, class codes, age groups, guardian consent and several subjects."""

from __future__ import annotations

from dataclasses import replace

from cognitive_twin.config import Settings
from cognitive_twin.skills import load_tracks

from conftest import login

TRACKS = load_tracks()


def _csrf(r) -> dict:
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.headers["x-csrf-token"]}


def _local(c, username: str, password: str) -> dict:
    c.cookies.clear()
    return _csrf(c.post("/auth/local-login", json={"username": username, "password": password}))


def _school_with_class(c, name="Hill Primary", youngest="under13", track="middle-school-maths"):
    """Platform admin -> school admin -> teacher -> class. Returns everything a test needs."""
    h = login(c, "admin")
    r = c.post("/api/admin/schools", json={"name": name, "region": "IN",
                                            "youngest_band": youngest}, headers=h)
    assert r.status_code == 200, r.text
    school = r.json()
    c.cookies.clear()
    r = c.post("/auth/invite", json={"code": school["admin_invite"], "name": "Head Teacher",
                                     "password": "head-teacher-pass"})
    hs = _csrf(r)
    head_user = r.json()["username"]
    code = c.post("/api/school/invites", json={"role": "educator"}, headers=hs).json()["code"]
    c.cookies.clear()
    r = c.post("/auth/invite", json={"code": code, "name": "Ms Rao",
                                     "password": "maths-teacher-pass"})
    ht = _csrf(r)
    teacher_user = r.json()["username"]
    r = c.post("/api/educator/cohorts", json={"name": "Class 5A", "track": track}, headers=ht)
    assert r.status_code == 200, r.text
    return {"school": school, "head": head_user, "teacher": teacher_user, "ht": ht,
            "cls": r.json()}


def _join(c, code: str, name="Leo Kumar", pw="sunny-tiger-42"):
    c.cookies.clear()
    r = c.post("/auth/join", json={"code": code, "first_name": name, "password": pw})
    return _csrf(r), r.json()["username"]


def _student(c, learners, username):
    return next(x for x in learners if x["username"] == username)


def test_full_school_journey_for_a_young_learner(client):
    s = _school_with_class(client)
    code = s["cls"]["join_code"]
    hl, leo = _join(client, code.lower()[:4] + "-" + code.lower()[4:])  # codes are forgiving
    assert leo.startswith("leo-")
    sess = client.get("/auth/session").json()["user"]
    assert sess["name"] == "Leo" and sess["age_band"] == "unknown"  # first name only

    # waiting for the teacher: told why, can't start
    me = client.get("/api/me/subjects", headers=hl).json()
    assert me["default"] == "middle-school-maths"
    status = client.get("/api/me?track=middle-school-maths", headers=hl).json()["consent"]
    assert "teacher still needs to approve" in status["not_eligible_reason"]
    r = client.post("/api/me/consent?track=middle-school-maths", json={"learning": True},
                    headers=hl)
    assert r.status_code == 403 and r.json()["code"] == "not_eligible"

    # teacher sees Leo waiting and approves him as under 13 with guardian consent
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    learners = client.get(f"/api/educator/cohorts/{s['cls']['id']}/learners", headers=ht).json()
    row = _student(client, learners, leo)
    assert row["waiting"] and row["local"]
    r = client.post(f"/api/educator/learners/{row['id']}/approve",
                    json={"age_band": "under13", "guardian_ok": True}, headers=ht)
    assert r.status_code == 200 and r.json()["guardian_ok"] is True

    # Leo learns maths with child-friendly wording; the headband is never recorded for him
    hl = _local(client, leo, "sunny-tiger-42")
    st = client.get("/api/me?track=middle-school-maths", headers=hl).json()["consent"]
    assert st["not_eligible_reason"] is None and "parent or guardian" in st["policy_text"]
    r = client.post("/api/me/consent?track=middle-school-maths",
                    json={"learning": True, "neural_sensing": True}, headers=hl)
    assert r.status_code == 200, r.text
    hist = client.get("/api/me/consent/history", headers=hl).json()
    assert any(x["scope"] == "guardian" and x["source"] == "school" and x["granted"]
               for x in hist)
    assert not any(x["scope"] == "neural_sensing" and x["granted"] for x in hist)

    # shorter stretches for under-13s: never more than 12 questions without a break
    track = TRACKS["middle-school-maths"]
    run = longest = 0
    for _ in range(40):
        d = client.post("/api/me/next?track=middle-school-maths", json={"skip_break": True},
                        headers=hl).json()
        if d["action"] == "break":
            run = 0
            client.post("/api/me/break?track=middle-school-maths", headers=hl)
            continue
        assert d["item"]["id"].startswith("msm-")
        item = track.items[d["item"]["id"]]
        client.post("/api/me/answer?track=middle-school-maths",
                    json={"item_id": item.id, "choice": item.answer}, headers=hl)
        run += 1
        longest = max(longest, run)
    assert 0 < longest <= 12

    # a child can't delete their own account; their teacher can, after exporting it
    r = client.post("/api/me/delete", json={"confirm": "delete my data"}, headers=hl)
    assert r.status_code == 403
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    data = client.get(f"/api/educator/learners/{row['id']}/export", headers=ht).json()
    assert data["profile"]["email"] is None and data["profile"]["age_band"] == "under13"
    assert data["subjects"][0]["subject"] == "middle-school-maths"
    r = client.post(f"/api/educator/learners/{row['id']}/delete", json={"confirm": "delete"},
                    headers=ht)
    assert r.status_code == 200
    client.cookies.clear()
    r = client.post("/auth/local-login", json={"username": leo, "password": "sunny-tiger-42"})
    assert r.status_code == 401


def test_minor_without_guardian_consent_waits(client):
    s = _school_with_class(client)
    hl, maya = _join(client, s["cls"]["join_code"], "Maya", "river-stone-77")
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    lid = _student(client, client.get(f"/api/educator/cohorts/{s['cls']['id']}/learners",
                                      headers=ht).json(), maya)["id"]
    client.post(f"/api/educator/learners/{lid}/approve",
                json={"age_band": "13to17", "guardian_ok": False}, headers=ht)
    hl = _local(client, maya, "river-stone-77")
    reason = client.get("/api/me", headers=hl).json()["consent"]["not_eligible_reason"]
    assert "parent or guardian" in reason
    assert client.post("/api/me/next", headers=hl).status_code == 403


def test_school_can_set_a_stricter_age_limit(client):
    s = _school_with_class(client, name="City Secondary", youngest="13to17")
    hl, kid = _join(client, s["cls"]["join_code"], "Ann", "purple-cloud-9")
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    lid = _student(client, client.get(f"/api/educator/cohorts/{s['cls']['id']}/learners",
                                      headers=ht).json(), kid)["id"]
    client.post(f"/api/educator/learners/{lid}/approve",
                json={"age_band": "under13", "guardian_ok": True}, headers=ht)
    hl = _local(client, kid, "purple-cloud-9")
    reason = client.get("/api/me", headers=hl).json()["consent"]["not_eligible_reason"]
    assert "hasn't opened" in reason


def test_teacher_adds_students_and_resets_passwords(client):
    s = _school_with_class(client)
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    r = client.post(f"/api/educator/cohorts/{s['cls']['id']}/students",
                    json={"first_name": "Zoe Smith", "age_band": "adult"}, headers=ht)
    assert r.status_code == 200, r.text
    zoe, temp = r.json()["username"], r.json()["temporary_password"]

    hz = _local(client, zoe, temp)  # a temporary password only allows choosing a new one
    r = client.get("/api/me", headers=hz)
    assert r.status_code == 403 and r.headers["x-ct-code"] == "password_change_required"
    assert client.get("/auth/session").json()["user"]["must_change_password"] is True
    assert client.post("/auth/password", json={"new": "short"}, headers=hz).status_code == 400
    hz = _csrf(client.post("/auth/password", json={"new": "zoe-new-password"}, headers=hz))
    assert client.get("/api/me", headers=hz).status_code == 200
    old_cookie = client.cookies.get("ct_session")

    # a reset signs Zoe out everywhere and issues a new temporary password
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    lid = _student(client, client.get(f"/api/educator/cohorts/{s['cls']['id']}/learners",
                                      headers=ht).json(), zoe)["id"]
    r = client.post(f"/api/educator/learners/{lid}/password", headers=ht)
    assert r.status_code == 200 and r.json()["username"] == zoe
    client.cookies.clear()
    client.cookies.set("ct_session", old_cookie)
    assert client.get("/api/me").status_code == 401
    assert client.get("/auth/session").json()["user"] is None


def test_lockout_after_repeated_wrong_passwords(client):
    s = _school_with_class(client)
    _, leo = _join(client, s["cls"]["join_code"])
    client.cookies.clear()
    for _ in range(5):
        r = client.post("/auth/local-login", json={"username": leo, "password": "wrong-wrong"})
        assert r.status_code == 401
    r = client.post("/auth/local-login", json={"username": leo, "password": "sunny-tiger-42"})
    assert r.status_code == 401  # paused for 15 minutes even with the right password
    r = client.post("/auth/local-login", json={"username": "nobody-xx", "password": "x"})
    assert r.status_code == 401  # same answer for an unknown name


def test_schools_and_classes_are_isolated(client):
    a = _school_with_class(client, name="School A")
    b = _school_with_class(client, name="School B")
    _, leo = _join(client, a["cls"]["join_code"])
    ha = _local(client, a["teacher"], "maths-teacher-pass")
    lid = _student(client, client.get(f"/api/educator/cohorts/{a['cls']['id']}/learners",
                                      headers=ha).json(), leo)["id"]

    hb = _local(client, b["teacher"], "maths-teacher-pass")
    assert [c["name"] for c in client.get("/api/educator/cohorts", headers=hb).json()] == [
        "Class 5A"]
    assert client.get(f"/api/educator/cohorts/{a['cls']['id']}/learners",
                      headers=hb).status_code == 404
    for path in (f"/api/educator/learners/{lid}", f"/api/educator/learners/{lid}/export"):
        assert client.get(path, headers=hb).status_code == 404
    assert client.post(f"/api/educator/learners/{lid}/approve",
                       json={"age_band": "adult"}, headers=hb).status_code == 404
    assert client.post(f"/api/educator/learners/{lid}/password", headers=hb).status_code == 404

    hhb = _local(client, b["head"], "head-teacher-pass")  # other school's administrator
    assert client.get(f"/api/educator/learners/{lid}", headers=hhb).status_code == 404
    hha = _local(client, a["head"], "head-teacher-pass")  # own school's administrator
    assert client.get(f"/api/educator/learners/{lid}", headers=hha).status_code == 200
    # a learner can't use teacher or school routes
    hl = _local(client, leo, "sunny-tiger-42")
    assert client.get("/api/school", headers=hl).status_code == 403
    assert client.post(f"/api/educator/cohorts/{a['cls']['id']}/students",
                       json={"first_name": "X", "age_band": "adult"},
                       headers=hl).status_code == 403


def test_join_codes_can_be_turned_off_and_invites_are_single_use(client):
    s = _school_with_class(client)
    ht = _local(client, s["teacher"], "maths-teacher-pass")
    old = s["cls"]["join_code"]
    assert client.post(f"/api/educator/cohorts/{s['cls']['id']}/code", json={"enabled": False},
                       headers=ht).json()["join_code"] is None
    client.cookies.clear()
    r = client.post("/auth/join", json={"code": old, "first_name": "Late",
                                        "password": "late-comer-123"})
    assert r.status_code == 400 and "class code" in r.json()["detail"]

    hs = _local(client, s["head"], "head-teacher-pass")
    code = client.post("/api/school/invites", json={"role": "educator"}, headers=hs).json()["code"]
    client.cookies.clear()
    assert client.post("/auth/invite", json={"code": code, "name": "T1",
                                             "password": "teacher-one-pass"}).status_code == 200
    client.cookies.clear()
    assert client.post("/auth/invite", json={"code": code, "name": "T2",
                                             "password": "teacher-two-pass"}).status_code == 400


def test_subjects_are_separate_and_schools_choose_them(client):
    h = login(client, "asha")
    subs = client.get("/api/me/subjects", headers=h).json()
    assert {x["id"] for x in subs["subjects"]} == set(TRACKS)
    for tid in TRACKS:
        r = client.post(f"/api/me/consent?track={tid}", json={"learning": True}, headers=h)
        assert r.status_code == 200
        d = client.post(f"/api/me/next?track={tid}", headers=h).json()
        assert d["item"]["id"] in TRACKS[tid].items  # each subject's own questions
    assert client.get("/api/me?track=nope", headers=h).status_code == 404

    # the school administrator narrows the subjects on offer
    s = _school_with_class(client, name="Code Club")
    hs = _local(client, s["head"], "head-teacher-pass")
    r = client.post("/api/school/settings", json={"allowed_tracks": ["python-basics"]},
                    headers=hs)
    assert r.json()["allowed_tracks"] == ["python-basics"]
    hl, leo = _join(client, s["cls"]["join_code"])
    assert [x["id"] for x in client.get("/api/me/subjects", headers=hl).json()["subjects"]] == [
        "python-basics"]
    assert client.get("/api/me?track=general-science", headers=hl).status_code == 404


def test_sso_personas_cover_children(client):
    h = login(client, "leo")  # IdP birthdate says 10, guardian consent from school records
    sess = client.get("/auth/session").json()["user"]
    assert sess["age_band"] == "under13" and sess["name"] == "Leo"
    data = client.get("/api/me/export", headers=h).json()
    assert data["profile"]["email"] is None
    assert client.get("/api/me", headers=h).json()["consent"]["not_eligible_reason"] is None
    h = login(client, "dev-minor")
    assert "parent or guardian" in client.get("/api/me", headers=h).json()["consent"][
        "not_eligible_reason"]
    h = login(client, "ravi")  # SSO teacher sees the demo school's maths class
    names = {c["name"] for c in client.get("/api/educator/cohorts", headers=h).json()}
    assert {"Maths 5A", "Algebra 9B"} <= names


def test_production_may_run_with_class_codes_only():
    s = replace(Settings(), env="production", database_url="postgresql://x/y",
                session_secret="s" * 40, redis_url="redis://r", key_provider="aws-kms",
                dev_login=False, allowed_hosts=["learn.example.edu"],
                public_base_url="https://learn.example.edu", oidc_issuer="", oidc_client_id="")
    s.validate()
