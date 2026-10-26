import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.tracing import API_SERVICE, configure_tracing


@pytest.fixture
def provider_set(monkeypatch: pytest.MonkeyPatch) -> list[trace.TracerProvider]:
    """The providers `configure_tracing` sets, kept from the real global one, which takes a single provider."""
    providers: list[trace.TracerProvider] = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    return providers


def test_no_endpoint_traces_nothing(provider_set: list[trace.TracerProvider]) -> None:
    assert configure_tracing(API_SERVICE, None) is None
    assert provider_set == []


def test_an_endpoint_exports_over_otlp_http_under_the_service_name(provider_set: list[trace.TracerProvider]) -> None:
    provider = configure_tracing(API_SERVICE, "http://jaeger:4318/")

    assert isinstance(provider, TracerProvider)
    assert provider_set == [provider]
    assert provider.resource.attributes["service.name"] == API_SERVICE
    [processor] = provider._active_span_processor._span_processors
    assert isinstance(processor, BatchSpanProcessor)
    exporter = processor._batch_processor._exporter
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == "http://jaeger:4318/v1/traces"
    provider.shutdown()


@pytest.mark.parametrize("endpoint", [None, "", "  "])
def test_a_blank_endpoint_is_none(endpoint: str | None) -> None:
    assert Settings(_env_file=None, otel_exporter_otlp_endpoint=endpoint).otel_exporter_otlp_endpoint is None
