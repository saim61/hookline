"""OpenTelemetry tracing, opt-in.

Metrics tell you *that* p99 ingest latency doubled. A trace tells you *which* span inside
one slow request took the time - the subscriber lookup, the insert, or waiting on a
connection from the pool. They answer different questions and neither replaces the other.

Off unless `HOOKLINE_OTEL_ENDPOINT` is set. A tracing SDK that exports nowhere still costs
per-span allocation on every request, and silently dropping spans is worse than not
pretending to trace: it looks instrumented and is not.
"""

from typing import TYPE_CHECKING

from hookline.config import Settings
from hookline.observability.logging import get_logger

if TYPE_CHECKING:
    # Only for the annotation. Importing FastAPI here at runtime would make the worker,
    # which never builds an app, pay for it.
    from fastapi import FastAPI

log = get_logger("hookline.tracing")

_configured = False


def setup_tracing(settings: Settings) -> bool:
    """Returns whether tracing was actually enabled."""
    global _configured
    if _configured or not settings.otel_endpoint:
        return _configured

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("otel_endpoint set but opentelemetry is not installed")
        return False

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.app_name,
                "service.version": "0.1.0",
                "deployment.environment": "development" if settings.debug else "production",
            }
        )
    )
    # Batched, not simple: SimpleSpanProcessor exports synchronously on span end, which
    # puts a network round trip to the collector inside the request it is measuring.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
    )
    trace.set_tracer_provider(provider)
    _configured = True
    log.info("tracing enabled", endpoint=settings.otel_endpoint)
    return True


def instrument_app(app: "FastAPI", settings: Settings) -> None:
    """Attach auto-instrumentation. Safe to call when tracing is off - it does nothing."""
    if not setup_tracing(settings):
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from hookline.db.session import get_engine

    FastAPIInstrumentor.instrument_app(
        app,
        # The probes and the scrape endpoint would otherwise be the overwhelming majority
        # of spans, and none of them are ever the thing being investigated.
        excluded_urls="health,ready,metrics",
    )
    # sync_engine: the instrumentation hooks SQLAlchemy's Core events, which live on the
    # underlying sync engine even when everything above it is async.
    SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
    HTTPXClientInstrumentor().instrument()


def instrument_worker(settings: Settings) -> None:
    """The worker has no ASGI app, so it instruments only its outbound calls."""
    if not setup_tracing(settings):
        return

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from hookline.db.session import get_engine

    SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
    HTTPXClientInstrumentor().instrument()
