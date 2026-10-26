"""Traces with OpenTelemetry, sent over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

A ticket's trace starts at `POST /tickets` (FastAPI's instrumentation) and goes on through Temporal: the tracing
interceptor on the client (see `orchestration/client.py`) passes the context to the workflow in its start headers, and
from the workflow to each activity it schedules. The agent adds a span for each call to the model and each tool it
runs. An approval is a request of its own, so it starts a trace of its own; the workflow handles the update in the
ticket's trace, with a link back to the approval's.

The workflow's own spans (`RunWorkflow`, `HandleUpdate`) last no time: the interceptor ends them at once, since a
workflow may wait for days and be replayed on another worker. The activities' spans have real durations.

Without an endpoint no tracer provider is set, and every span is OpenTelemetry's no-op. The JSON log lines carry the
current `trace_id` and `span_id` (see `logs.py`), so a trace leads to its lines and a line to its trace.

As in the logs, span attributes hold IDs, types and counts: never the ticket's text or anything the model wrote.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

API_SERVICE = "northdesk-api"
WORKER_SERVICE = "northdesk-worker"


def configure_tracing(service_name: str, otlp_endpoint: str | None) -> TracerProvider | None:
    """Export spans to the OTLP/HTTP collector at `otlp_endpoint`, or do nothing without one.

    Returns the provider, to be shut down when the process stops: that sends the spans still waiting in the batch.
    """
    if otlp_endpoint is None:
        return None
    # The base URL, as in OTEL_EXPORTER_OTLP_ENDPOINT; the exporter needs the traces path spelled out.
    provider = tracer_provider(service_name, OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
    trace.set_tracer_provider(provider)
    return provider


def tracer_provider(service_name: str, exporter: SpanExporter) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider
