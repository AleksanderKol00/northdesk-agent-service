import threading
import time
from collections.abc import Sequence
from datetime import timedelta

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from northdesk_ticket_agent.tracing import API_SERVICE, configure_tracing, stop_within, tracer_provider

OTEL_VARIABLES = ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SDK_DISABLED"]


@pytest.fixture
def provider_set(monkeypatch: pytest.MonkeyPatch) -> list[trace.TracerProvider]:
    """The providers `configure_tracing` sets, kept from the real global one, which takes a single provider. Starts
    from an environment with none of OpenTelemetry's variables."""
    for name in OTEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    providers: list[trace.TracerProvider] = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    return providers


def exporter_of(provider: trace.TracerProvider) -> object:
    assert isinstance(provider, TracerProvider)
    [processor] = provider._active_span_processor._span_processors
    assert isinstance(processor, BatchSpanProcessor)
    return processor._batch_processor._exporter


@pytest.mark.parametrize("endpoint", [None, "", "  "])
def test_no_endpoint_traces_nothing(
    provider_set: list[trace.TracerProvider], monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> None:
    if endpoint is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)

    configure_tracing(API_SERVICE)()

    assert provider_set == []


def test_an_endpoint_exports_over_otlp_http_under_the_service_name(
    provider_set: list[trace.TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318/")

    stop = configure_tracing(API_SERVICE)

    [provider] = provider_set
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == API_SERVICE
    exporter = exporter_of(provider)
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == "http://jaeger:4318/v1/traces"
    stop()


def test_a_traces_only_endpoint_is_used_as_given(
    provider_set: list[trace.TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example/traces")

    stop = configure_tracing(API_SERVICE)

    [provider] = provider_set
    exporter = exporter_of(provider)
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == "https://collector.example/traces"
    stop()


def test_the_sdk_can_be_disabled(provider_set: list[trace.TracerProvider], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    configure_tracing(API_SERVICE)()

    assert provider_set == []


class StuckExporter(SpanExporter):
    """A collector that never answers."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.release.wait()
        return SpanExportResult.FAILURE


def test_a_stop_gives_up_on_a_collector_that_does_not_answer() -> None:
    exporter = StuckExporter()
    provider = tracer_provider(API_SERVICE, exporter)
    provider.get_tracer(__name__).start_span("waiting").end()

    started = time.monotonic()
    stop_within(provider, timedelta(milliseconds=200))
    elapsed = time.monotonic() - started

    exporter.release.set()  # lets the export thread end
    assert elapsed < 2
