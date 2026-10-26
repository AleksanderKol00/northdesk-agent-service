"""Structured logs: one JSON object per line, carrying the IDs that tie a line to its ticket.

`configure_logging` sends every logger (ours, uvicorn's, Temporal's) through one JSON handler. A line's fields come
from three places, later ones winning on a clash:

- Temporal's loggers (`workflow.logger`, `activity.logger`) attach the running workflow or activity as
  `temporal_workflow` / `temporal_activity`, and Temporal Core's forwarded lines may name one too. Its `workflow_id` is
  copied to the top level, and also as `ticket_id`: the only workflow in this service is the ticket's, whose ID is the
  ticket ID. So the SDK's own lines (a failed activity attempt) are found by the same filter as ours.
- `log_context(...)`: fields for every line logged inside the block, from any module. The activities use it, so the
  agent's and the Helpdesk's lines carry the ticket's IDs without knowing about them.
- `extra={...}` on the call itself.

Following a ticket end to end is then one filter, e.g. `jq 'select(.ticket_id == "T-2")'`. A line logged inside an
OpenTelemetry span also carries its `trace_id` and `span_id` (see `tracing.py`), which lead from the line to the trace.

Ticket text is untrusted and may hold personal data: log IDs, types and counts, never the subject, body or reply.
Libraries that may log payloads at DEBUG (`LIBRARY_LOGGERS`) are kept at INFO, so `LOG_LEVEL=DEBUG` adds only our lines.
"""

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from opentelemetry import trace

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

TEMPORAL_DETAILS = ("temporal_workflow", "temporal_activity")

# uvicorn and the FastAPI CLI give these loggers handlers of their own; they are sent through ours instead.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")
# Replaced by the API's own access line, which knows the ticket (see api/access_log.py).
UVICORN_ACCESS_LOGGER = "uvicorn.access"
# Libraries whose DEBUG lines can carry payloads: Temporal's SDK writes each activity's result, which for
# `ticket.decide` is what the model wrote. They stay at INFO when LOG_LEVEL=DEBUG, so DEBUG adds only our own lines.
# Top-level names, so their submodules (private ones included) are covered without being named.
LIBRARY_LOGGERS = ("temporalio", "openai", "httpx", "httpcore")

# Attributes every LogRecord has; anything else on a record came from `extra`. uvicorn's `color_message` is its message
# with terminal colour codes, which have no place in JSON.
_RECORD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",
    "temporal_log",  # a forwarded Temporal Core line; its parts are added by `_core_fields`
}

_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add `fields` to every line logged inside the block, in this task and in the tasks it starts."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def bind_log_context(**fields: Any) -> None:
    """Add fields to the innermost `log_context`, also for the lines its owner logs once the block is done with them.

    The API's access line is logged by the middleware around the endpoint. An endpoint that learns the ticket ID from
    the request body binds it here, so the access line carries it too. Outside any `log_context`, this does nothing.
    """
    current = _context.get()
    if current is not None:
        current.update(fields)


class JsonFormatter(logging.Formatter):
    """Formats a record as one line of JSON: time (UTC), level, logger, message, then the fields described above."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        line |= _trace_ids()
        line |= _temporal_ids(record)
        line |= _core_fields(record)
        # Read when the line is formatted, which a stream handler does in the caller's thread and context.
        line |= _context.get() or {}
        line |= {
            key: value
            for key, value in vars(record).items()
            if key not in _RECORD_ATTRIBUTES and not key.startswith("_")  # the SDK marks internal fields with "__"
        }
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, default=str, ensure_ascii=False)


def json_handler(stream: TextIO | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(level: LogLevel = "INFO", stream: TextIO | None = None) -> None:
    """Send every log line as JSON to `stream` (stdout by default), replacing the handlers set up before."""
    logging.basicConfig(level=level, handlers=[json_handler(stream)], force=True)
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)  # the root's level applies
    logging.getLogger(UVICORN_ACCESS_LOGGER).disabled = True
    for name in LIBRARY_LOGGERS:
        # At any other level the root's level applies, as for every other logger.
        logging.getLogger(name).setLevel(logging.INFO if level == "DEBUG" else logging.NOTSET)


def _trace_ids() -> dict[str, Any]:
    """The current span's IDs, in the hex form tracing backends show and search by."""
    span = trace.get_current_span().get_span_context()
    if not span.is_valid:
        return {}
    return {"trace_id": trace.format_trace_id(span.trace_id), "span_id": trace.format_span_id(span.span_id)}


def _core_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The target and fields of a line forwarded from Temporal Core (the SDK's Rust part), if this is one."""
    core = getattr(record, "temporal_log", None)
    if core is None:
        return {}
    fields = dict(core.fields)
    line: dict[str, Any] = {"core_target": core.target, "core_fields": fields}
    if isinstance(fields.get("workflow_id"), str):
        line |= _ids(fields["workflow_id"])
    return line


def _temporal_ids(record: logging.LogRecord) -> dict[str, Any]:
    for key in TEMPORAL_DETAILS:
        details = getattr(record, key, None)
        if isinstance(details, Mapping) and "workflow_id" in details:
            return _ids(details["workflow_id"])
    return {}


def _ids(workflow_id: Any) -> dict[str, Any]:
    """Every workflow here is a ticket's, with the ticket ID as its workflow ID."""
    return {"workflow_id": workflow_id, "ticket_id": workflow_id}
