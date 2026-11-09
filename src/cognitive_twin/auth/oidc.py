"""OpenID Connect sign-in with the partner institution's IdP (requirement R3.1).

Authorization-code flow with PKCE, state and nonce. ID tokens and API bearer
tokens are validated against the issuer's published JWKS (signature, issuer,
audience, expiry). Institution claims supply role, adulthood, region and
cohorts (R4.1, R4.4): nothing is self-declared.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from ..config import Settings

ROLES = ("learner", "educator", "admin")


class OIDCError(RuntimeError):
    pass


@dataclass
class Identity:
    issuer: str
    subject: str
    email: str | None
    name: str
    role: str
    adult_verified: bool
    region: str
    institution: str | None
    cohorts: list[str]


def adult_from_claims(claims: dict, settings: Settings, today: date | None = None) -> bool:
    """Adulthood comes only from institution records: a verified flag or a birthdate."""
    if claims.get(settings.claim_adult) is True:
        return True
    bd = claims.get(settings.claim_birthdate)
    if isinstance(bd, str):
        try:
            born = date.fromisoformat(bd[:10])
        except ValueError:
            return False
        t = today or date.today()
        age = t.year - born.year - ((t.month, t.day) < (born.month, born.day))
        return age >= settings.min_age
    return False


def identity_from_claims(claims: dict, settings: Settings) -> Identity:
    role = str(claims.get(settings.claim_role, "learner")).lower()
    if role not in ROLES:
        role = "learner"
    region = str(claims.get(settings.claim_region, "")).upper()[:8]
    if not region:
        raise OIDCError(f"the identity provider did not supply {settings.claim_region!r}; "
                        "region must come from institution records")
    cohorts = claims.get(settings.claim_cohorts) or []
    if isinstance(cohorts, str):
        cohorts = [c.strip() for c in cohorts.split(",") if c.strip()]
    name = (claims.get("name") or claims.get("given_name") or claims.get("email")
            or "Learner")[:120]
    return Identity(issuer=claims["iss"], subject=str(claims["sub"]), email=claims.get("email"),
                    name=name, role=role, adult_verified=adult_from_claims(claims, settings),
                    region=region, institution=claims.get(settings.claim_institution),
                    cohorts=[str(c)[:120] for c in cohorts][:50])


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


class OIDCClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=10)
        self._meta: dict | None = None
        self._meta_at = 0.0
        self._jwks: PyJWKClient | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        with self._lock:
            if self._meta is None or time.time() - self._meta_at > 3600:
                url = f"{self.s.oidc_issuer}/.well-known/openid-configuration"
                r = self.http.get(url)
                if r.status_code != 200:
                    raise OIDCError("cannot load identity provider metadata")
                meta = r.json()
                if meta.get("issuer", "").rstrip("/") != self.s.oidc_issuer:
                    raise OIDCError("identity provider issuer mismatch")
                self._meta, self._meta_at = meta, time.time()
                self._jwks = PyJWKClient(meta["jwks_uri"], cache_keys=True, lifespan=3600)
            return self._meta

    def authorize_url(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        meta = self.metadata()
        q = {"response_type": "code", "client_id": self.s.oidc_client_id,
             "redirect_uri": redirect_uri, "scope": self.s.oidc_scopes, "state": state,
             "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{meta['authorization_endpoint']}?{urlencode(q)}"

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> dict:
        meta = self.metadata()
        r = self.http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.s.oidc_client_id, "client_secret": self.s.oidc_client_secret,
            "code_verifier": verifier})
        if r.status_code != 200:
            raise OIDCError("token exchange failed")
        return r.json()

    def validate(self, token: str, audience: str, nonce: str | None = None) -> dict:
        self.metadata()
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256", "PS256"],
                                audience=audience, issuer=self._meta["issuer"],
                                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
                                leeway=60)
        except jwt.PyJWTError as e:
            raise OIDCError(f"invalid token: {e.__class__.__name__}") from e
        if nonce is not None and claims.get("nonce") != nonce:
            raise OIDCError("nonce mismatch")
        return claims
