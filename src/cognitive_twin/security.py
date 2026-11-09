"""Transport and abuse protections (requirement R3.6).

* Security headers on every response: HSTS outside development, a strict
  Content-Security-Policy (no inline scripts), no framing, no sniffing.
* HTTPS enforcement behind the load balancer (``X-Forwarded-Proto``).
* Fixed-window rate limits per IP and per user. Redis makes the counters
  shared across instances (R1.4); the in-memory backend is for development.
"""

from __future__ import annotations

import threading
import time

import anyio
import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


LIMITER_THREADS = anyio.CapacityLimiter(8)

# credential-guessing and IdP-abuse surface; /auth/session and /auth/config are not
SIGN_IN_PATHS = frozenset({"/auth/login", "/auth/callback", "/auth/dev-login"})


def client_ip(request: Request, trusted_hops: int) -> str:
    if trusted_hops > 0:
        xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",")
               if p.strip()]
        if len(xff) >= trusted_hops:
            return xff[-trusted_hops]
    return request.client.host if request.client else "unknown"


class SecurityHeadersMiddleware:
    """Pure ASGI: adds security headers and enforces HTTPS behind the load balancer."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.s = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not self.s.is_dev and path not in ("/healthz", "/readyz", "/metrics"):
            hdrs = dict(scope.get("headers") or [])
            proto = hdrs.get(b"x-forwarded-proto", scope.get("scheme", "http").encode()).decode()
            if proto != "https":
                return await JSONResponse({"detail": "HTTPS is required"},
                                          status_code=400)(scope, receive, send)
        extra = [(b"content-security-policy", CSP.encode()),
                 (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                 (b"referrer-policy", b"no-referrer"),
                 (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                 (b"cross-origin-opener-policy", b"same-origin")]
        if path.startswith(("/api/", "/auth/")):
            extra.append((b"cache-control", b"no-store"))
        if not self.s.is_dev:
            extra.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in extra if k not in present]
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimiter:
    def __init__(self, redis_url: str = ""):
        self._redis = None
        if redis_url:
            import redis
            self._redis = redis.Redis.from_url(redis_url, socket_timeout=0.5,
                                               socket_connect_timeout=0.5)
        self._mem: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int = 60) -> tuple[bool, int]:
        """Count one request. Returns (allowed, seconds until the window resets)."""
        now = int(time.time())
        bucket = now // window
        reset = window - (now % window)
        k = f"ct:rl:{key}:{bucket}"
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.incr(k)
                pipe.expire(k, window + 5)
                count = int(pipe.execute()[0])
                return count <= limit, reset
            except Exception:  # noqa: BLE001 - fail open on a cache outage, but say so
                import logging
                logging.getLogger("cognitive_twin").warning("rate limiter unavailable")
                return True, reset
        with self._lock:
            b, count = self._mem.get(key, (bucket, 0))
            if b != bucket:
                b, count = bucket, 0
            count += 1
            self._mem[key] = (b, count)
            if len(self._mem) > 100_000:
                self._mem = {kk: v for kk, v in self._mem.items() if v[0] == bucket}
        return count <= limit, reset


class IPRateLimitMiddleware:
    """Pure ASGI per-IP limits. The Redis call runs off the event loop."""

    def __init__(self, app, settings: Settings, limiter: RateLimiter):
        self.app = app
        self.s = settings
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in ("/healthz", "/readyz", "/metrics") or path.startswith("/static/"):
            return await self.app(scope, receive, send)
        ip = client_ip(Request(scope), self.s.trusted_proxy_hops)
        scope.setdefault("state", {})["client_ip"] = ip
        if path in SIGN_IN_PATHS:
            key, limit = f"auth:{ip}", self.s.rate_auth_per_min
        else:
            key, limit = f"ip:{ip}", self.s.rate_ip_per_min
        ok, reset = await anyio.to_thread.run_sync(self.limiter.hit, key, limit,
                                                   limiter=LIMITER_THREADS)
        if not ok:
            return await JSONResponse({"detail": "Too many requests. Please slow down."},
                                      status_code=429,
                                      headers={"Retry-After": str(reset)})(scope, receive, send)
        await self.app(scope, receive, send)
