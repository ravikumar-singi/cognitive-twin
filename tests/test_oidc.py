"""R3.1 single sign-on: the full authorization-code + PKCE flow against a mock IdP."""

import base64
import hashlib
import time
from dataclasses import replace
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cognitive_twin.app import create_app
from cognitive_twin.auth.oidc import OIDCClient, adult_from_claims
from cognitive_twin.config import Settings

ISS = "https://idp.example.edu"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIdP:
    def __init__(self):
        self.codes: dict[str, dict] = {}
        self.claims: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISS, "authorization_endpoint": f"{ISS}/authorize",
                "token_endpoint": f"{ISS}/token", "jwks_uri": f"{ISS}/jwks"})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            entry = self.codes.pop(form["code"][0], None)
            if entry is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(
                form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            if challenge != entry["challenge"]:
                return httpx.Response(400, json={"error": "pkce"})
            now = int(time.time())
            tok = jwt.encode({"iss": ISS, "aud": "ct-web", "sub": "u-123", "iat": now,
                              "exp": now + 300, "nonce": entry["nonce"], **self.claims},
                             KEY, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": tok, "access_token": "x"})
        return httpx.Response(404)


class StubJWKS:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture
def sso(settings):
    idp = FakeIdP()
    s = replace(settings, oidc_issuer=ISS, oidc_client_id="ct-web", oidc_client_secret="s",
                dev_login=False)
    app = create_app(s)
    oc = OIDCClient(s, http=httpx.Client(transport=httpx.MockTransport(idp.handler)))
    oc.metadata()
    oc._jwks = StubJWKS()
    app.state.ct.oidc = oc
    with TestClient(app) as c:
        yield c, idp


def _sign_in(c: TestClient, idp: FakeIdP, claims: dict) -> httpx.Response:
    idp.claims = claims
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["ct-web"]
    idp.codes["abc"] = {"challenge": q["code_challenge"][0], "nonce": q["nonce"][0]}
    return c.get(f"/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False)


def test_sso_login_provisions_user_from_institution_claims(sso):
    c, idp = sso
    r = _sign_in(c, idp, {"name": "Rita", "email": "rita@example.edu", "ct_role": "learner",
                          "birthdate": "1999-02-03", "ct_region": "IN",
                          "ct_cohorts": ["ML Pilot A"]})
    assert r.status_code == 302 and r.headers["location"] == "/"
    me = c.get("/api/me").json()
    assert me["user"]["name"] == "Rita" and me["user"]["region"] == "IN"
    assert me["consent"]["eligible"] is True


def test_dev_login_is_off_when_sso_is_used(sso):
    c, _ = sso
    assert c.post("/auth/dev-login", json={"persona": "asha"}).status_code == 404


def test_state_mismatch_is_rejected(sso):
    c, idp = sso
    idp.claims = {"ct_region": "IN"}
    c.get("/auth/login", follow_redirects=False)
    assert c.get("/auth/callback?code=abc&state=forged",
                 follow_redirects=False).status_code == 400


def test_region_must_come_from_the_institution(sso):
    c, idp = sso
    assert _sign_in(c, idp, {"name": "NoRegion", "ct_adult_verified": True}).status_code == 401


def test_minor_by_institution_birthdate_is_not_eligible(sso):
    c, idp = sso
    _sign_in(c, idp, {"name": "Kid", "birthdate": "2013-06-01", "ct_region": "IN"})
    assert c.get("/api/me").json()["consent"]["eligible"] is False


def test_adulthood_rules():
    s = Settings()
    assert adult_from_claims({"ct_adult_verified": True}, s)
    assert not adult_from_claims({"ct_adult_verified": "true"}, s)  # must be a real boolean
    assert adult_from_claims({"birthdate": "2008-11-09"}, s, today=date(2026, 11, 9))
    assert not adult_from_claims({"birthdate": "2008-11-10"}, s, today=date(2026, 11, 9))
    assert not adult_from_claims({}, s)
