"""Traces with OpenTelemetry, sent over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

The exporter is configured by OpenTelemetry's own environment variables (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SDK_DISABLED`, ...), read by the SDK from the process environment, not from `.env`.

A ticket's trace starts at `POST /tickets` (FastAPI's instrumentation) and goes on through Temporal: the tracing
interceptor on the client (see `orchestration/client.py`) passes the context to the workflow in its start headers, and
from the workflow to each activity it schedules. The agent adds a span for each call to the model and each tool it
runs. An approval is a request of its own, so it starts a trace of its own; the workflow handles the update in the
ticket's trace, with a link back to the approval's.

The workflow's own spans (`RunWorkflow`, `HandleUpdate`) last no time: the interceptor ends them at once, since a
workflow may wait for days and be replayed on another worker. The activities' spans have real durations.

Without an endpoint (or with `OTEL_SDK_DISABLED=true`) no tracer provider is set, and every span is OpenTelemetry's
no-op. The JSON log lines carry the current `trace_id` and `span_id` (see `logs.py`), so a trace leads to its lines
and a line to its trace.

As in the logs, span attributes hold IDs, types and counts: never the ticket's text or anything the model wrote.
"""

import os
import threading
from collections.abc import Callable
from datetime import timedelta

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
    OTEL_SDK_DISABLED,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

API_SERVICE = "northdesk-api"
WORKER_SERVICE = "northdesk-worker"

# How long a stop waits for the last spans to go out. The worker's graceful shutdown takes up to 8 s of Docker's 10 s
# before SIGKILL, and a collector that is down would otherwise hold the stop for the exporter's whole timeout (10 s).
FLUSH_TIMEOUT = timedelta(seconds=1)


def configure_tracing(service_name: str) -> Callable[[], None]:
    """Export spans over OTLP/HTTP when OpenTelemetry's environment names an endpoint; else do nothing.

    Returns the function to call when the process stops: it sends the spans still waiting in the batch, giving up
    after `FLUSH_TIMEOUT`.
    """
    if not _exporting():
        return _nothing_to_stop
    provider = tracer_provider(service_name, OTLPSpanExporter())  # reads the endpoint, headers, ... from the env
    trace.set_tracer_provider(provider)
    return lambda: stop_within(provider, FLUSH_TIMEOUT)


def tracer_provider(service_name: str, exporter: SpanExporter) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def stop_within(provider: TracerProvider, timeout: timedelta) -> None:
    """Shut `provider` down, waiting at most `timeout`: its own shutdown takes no timeout. A daemon thread left
    exporting doesn't keep the process alive, and the provider's exit hook then finds it already shut down."""
    stopping = threading.Thread(target=provider.shutdown, name="tracing-shutdown", daemon=True)
    stopping.start()
    stopping.join(timeout.total_seconds())


def _exporting() -> bool:
    if os.environ.get(OTEL_SDK_DISABLED, "").strip().lower() == "true":
        return False
    return any(
        os.environ.get(name, "").strip() for name in (OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, OTEL_EXPORTER_OTLP_ENDPOINT)
    )


def _nothing_to_stop() -> None:
    pass
