"""Observability bootstrap (RFC-8): Prometheus + OpenTelemetry + Sentry.

All three are **opt-in via env vars** so the same code path works for
dev (none enabled), production-self-hosted (Prom + OTel) and SaaS
(everything including Sentry).

Toggles:

* ``PROM_DISABLED=1``    — skip the Prometheus instrumentator entirely
* ``OTLP_ENDPOINT=...``  — empty disables tracing; otherwise grpc OTLP
* ``SENTRY_DSN=...``     — empty disables Sentry

Each block is wrapped in a defensive try/except: a broken observability
import must NEVER fail-stop the app. The worst case is a deployment with
no metrics — visible in Grafana, fixable in the next push.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def install_metrics(app: Any) -> None:
    """Mount /metrics on the FastAPI app via prometheus-fastapi-instrumentator.

    Call this once during app construction, BEFORE adding routers — the
    instrumentator wraps the ASGI middleware stack at import time.
    """
    if _env("PROM_DISABLED") in ("1", "true", "True"):
        log.info("observability: prometheus disabled via PROM_DISABLED")
        return
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            should_ignore_untemplated=True,
            excluded_handlers=["/metrics", "/healthz", "/readyz"],
        ).instrument(app).expose(
            app, endpoint="/metrics", include_in_schema=False, tags=["observability"]
        )
        log.info("observability: /metrics endpoint mounted")
    except Exception as exc:  # noqa: BLE001 — observability failure never fails the app
        log.warning("observability: prometheus instrumentation skipped: %s", exc)


def install_tracing(service_name: str = "autotest-backend") -> None:
    """Wire OpenTelemetry SDK with an OTLP gRPC exporter to ``OTLP_ENDPOINT``.

    No-op when the endpoint env var is empty (the most common dev case).
    Auto-instruments the SQLAlchemy sync engine that backs Celery tasks and
    the FastAPI app. The async engine is hooked separately via
    :func:`instrument_app` once the FastAPI app instance exists.
    """
    endpoint = _env("OTLP_ENDPOINT")
    if not endpoint:
        log.info("observability: OTLP_ENDPOINT empty — tracing disabled")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        env = _env("AUTOTEST_ENV", "dev")
        # Plaintext gRPC unless the endpoint is explicitly https. In-cluster
        # OTLP collectors (Tempo, OTel-Collector) default to plaintext on
        # 4317; only public/managed endpoints (Honeycomb, Datadog, etc.) use
        # TLS. Without `insecure=True` the gRPC client tries SNI-handshake
        # against a plaintext peer and the trace pipe stays silently down.
        insecure = not endpoint.lower().startswith("https://")
        # Strip any explicit scheme — OTLPSpanExporter prefers host:port form.
        bare = endpoint.split("://", 1)[1] if "://" in endpoint else endpoint
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name, "deployment.environment": env})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=bare, insecure=insecure))
        )
        trace.set_tracer_provider(provider)
        log.info("observability: OTel tracing -> %s (insecure=%s)", bare, insecure)
    except Exception as exc:  # noqa: BLE001
        log.warning("observability: OTel init skipped: %s", exc)


def instrument_app(app: Any) -> None:
    """Apply per-component OTel instrumentors after the FastAPI app exists.

    Imported lazily so unit tests that never touch the app do not require
    OTel deps installed.
    """
    if not _env("OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        log.warning("observability: FastAPI instrumentor skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        # Celery side: sync engine. FastAPI side: async engine — instrument
        # the underlying sync_engine that asyncpg wraps.
        from app.database import engine as async_engine

        SQLAlchemyInstrumentor().instrument(engine=async_engine.sync_engine)
    except Exception as exc:  # noqa: BLE001
        log.warning("observability: SQLAlchemy instrumentor skipped: %s", exc)


def instrument_celery() -> None:
    """Hook OTel into Celery (call from ``tasks/celery_app.py`` once)."""
    if not _env("OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        log.warning("observability: Celery instrumentor skipped: %s", exc)


def install_sentry(component: str) -> None:
    """Initialise Sentry SDK if ``SENTRY_DSN`` is set.

    ``component`` is "backend" or "celery" — used for the Sentry tag so
    issues from the two paths can be filtered separately.
    """
    dsn = _env("SENTRY_DSN")
    if not dsn:
        log.info("observability: SENTRY_DSN empty — Sentry disabled")
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=_env("AUTOTEST_ENV", "dev"),
            release=_env("AUTOTEST_RELEASE") or None,
            traces_sample_rate=float(_env("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            integrations=[
                FastApiIntegration() if component == "backend" else CeleryIntegration(),
                SqlalchemyIntegration(),
            ],
        )
        sentry_sdk.set_tag("component", component)
        log.info("observability: Sentry initialised (%s)", component)
    except Exception as exc:  # noqa: BLE001
        log.warning("observability: Sentry init skipped: %s", exc)
