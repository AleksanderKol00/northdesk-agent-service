import logging
from functools import cache

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import LogForwardingConfig, LoggingConfig, Runtime, TelemetryConfig, TelemetryFilter

from northdesk_ticket_agent.config import Settings

# Temporal Core, the SDK's Rust part, logs on its own (connection and poll errors). Forwarded to this logger, its lines
# become JSON like every other; otherwise they would be plain text on stderr.
CORE_LOGGER = "temporalio.core"


async def connect(settings: Settings) -> Client:
    """A Temporal client for the API and the worker, with the pydantic data converter the workflow's models need.

    Its tracing interceptor applies to the worker built on it too: the API's start passes the trace context in the
    workflow's headers, the workflow passes it on to the activities, and each gets a span in the same trace.
    """
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
        interceptors=[TracingInterceptor()],
        runtime=_runtime(),
    )


def core_logging() -> LoggingConfig:
    """Core's warnings and errors, sent to `CORE_LOGGER` with their target and fields kept out of the message."""
    return LoggingConfig(
        filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),  # the SDK's default levels
        forwarding=LogForwardingConfig(
            logger=logging.getLogger(CORE_LOGGER),
            append_target_to_name=False,
            prepend_target_on_message=False,
            append_log_fields_to_message=False,
        ),
    )


@cache
def _runtime() -> Runtime:
    # One per process: each runtime has a thread pool of its own.
    return Runtime(telemetry=TelemetryConfig(logging=core_logging()))
