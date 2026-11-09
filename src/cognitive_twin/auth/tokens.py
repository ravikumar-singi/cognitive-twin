"""Signed, expiring session tokens (HMAC-SHA256).

The session cookie holds only the user id, a CSRF secret and timestamps. Role
and eligibility are re-read from the database on every request, so changes
and deletions take effect immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenError(ValueError):
    pass


def sign(payload: dict, secret: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(token: str, secret: str) -> dict:
    try:
        body, mac = token.split(".", 1)
    except ValueError as e:
        raise TokenError("malformed token") from e
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(mac)):
        raise TokenError("bad signature")
    payload = json.loads(_unb64(body))
    if payload.get("exp", 0) < time.time():
        raise TokenError("expired")
    return payload


def new_session(user_id: str, secret: str, ttl_minutes: int) -> tuple[str, str]:
    """Return (session token, csrf token)."""
    csrf = secrets.token_urlsafe(24)
    now = int(time.time())
    return sign({"uid": user_id, "csrf": csrf, "iat": now, "exp": now + ttl_minutes * 60},
                secret), csrf


def short_lived(payload: dict, secret: str, seconds: int = 600) -> str:
    return sign({**payload, "exp": int(time.time()) + seconds}, secret)
