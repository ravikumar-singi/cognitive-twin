"""Structured logs, metrics and optional tracing (requirement R5.1).

* Logs: one JSON object per line on stdout, with a request id on every line.
  Logs carry user UUIDs, never names or e-mail addresses.
* Metrics: Prometheus format at ``/metrics``.
* Traces: OpenTelemetry, switched on when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
  set and the ``otel`` extra is installed.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector
from sqlalchemy import func, select

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("rid", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("uid", default=None)

HTTP_REQUESTS = Counter("ct_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("ct_http_request_duration_seconds", "HTTP request latency",
                         ["method", "route"],
                         buckets=(.005, .01, .025, .05, .1, .2, .3, .5, 1, 2.5, 5))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
               "request_id": request_id_var.get(), "user_id": user_id_var.get()}
        for k, v in getattr(record, "extra_fields", {}).items():
            out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).disabled = True


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})


log = logging.getLogger("cognitive_twin")


class RequestContextMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware): it never cancels the request when a client
    disconnects, so handlers always finish and release their database session."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        rid = headers.get(b"x-request-id", b"").decode("latin-1")
        if not rid or len(rid) > 64 or not rid.replace("-", "").isalnum():
            rid = uuid.uuid4().hex
        request_id_var.set(rid)
        user_id_var.set(None)
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            HTTP_LATENCY.labels(scope["method"], route).observe(dur)
            if route not in ("/healthz", "/metrics"):
                log_event(log, "request", method=scope["method"], route=route,
                          status=status["code"], duration_ms=round(dur * 1000, 1))


class ActiveLearnersCollector(Collector):
    """Learners with a teaching decision in the last 15 minutes, computed at scrape time."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def collect(self):
        from .db.models import Decision
        g = GaugeMetricFamily("ct_active_learners", "Learners active in the last 15 minutes")
        try:
            with self.session_factory() as db:
                since = datetime.now(UTC) - timedelta(minutes=15)
                n = db.scalar(select(func.count(func.distinct(Decision.user_id)))
                              .where(Decision.created_at >= since)) or 0
            g.add_metric([], float(n))
        except Exception:  # noqa: BLE001 - a scrape must never crash the service
            log.warning("active learner metric unavailable")
        yield g


_collector: ActiveLearnersCollector | None = None


def register_active_learners(session_factory) -> None:
    global _collector
    if _collector is not None:
        REGISTRY.unregister(_collector)
    _collector = ActiveLearnersCollector(session_factory)
    REGISTRY.register(_collector)


def setup_tracing(app, engine) -> bool:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL endpoint set but the otel extra is not installed; tracing off")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": "cognitive-twin"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")
    SQLAlchemyInstrumentor().instrument(engine=engine)
    return True
