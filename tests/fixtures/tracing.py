from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class Spans:
    """The spans finished while the `spans` fixture was active."""

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    def all(self) -> list[ReadableSpan]:
        return list(self._exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.all() if span.name == name]

    def in_trace(self, trace_id: int) -> list[ReadableSpan]:
        return [span for span in self.all() if span.context is not None and span.context.trace_id == trace_id]


@pytest.fixture(scope="session")
def _span_exporter() -> InMemorySpanExporter:
    # OpenTelemetry takes a global tracer provider once per process, so it is set for the session, the first time a
    # test asks for spans. From then on every span is kept in memory; `spans` clears them before each test.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))  # at once, so a test sees a span as soon as it ends
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_span_exporter: InMemorySpanExporter) -> Iterator[Spans]:
    """Every span finished during the test, recorded through the global tracer provider as the API and worker do."""
    _span_exporter.clear()
    yield Spans(_span_exporter)
    _span_exporter.clear()
