import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from temporalio import activity, workflow

from northdesk_ticket_agent.logs import UVICORN_ACCESS_LOGGER, UVICORN_LOGGERS, json_handler
from northdesk_ticket_agent.orchestration.worker import SDK_ACTIVITY_LOGGER


class LogLines:
    """What was logged as JSON while the `json_logs` fixture was active, one parsed object per line."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def all(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def about(self, ticket_id: str) -> list[dict[str, Any]]:
        """The lines a filter on the ticket ID finds."""
        return [line for line in self.all() if line.get("ticket_id") == ticket_id]

    def from_logger(self, name: str) -> list[dict[str, Any]]:
        return [line for line in self.all() if line["logger"] == name]


@pytest.fixture
def json_logs(restore_logging: None) -> LogLines:
    """Every INFO-or-higher line logged during the test, formatted as the worker and the API format it.

    It adds a handler rather than calling `configure_logging`, which would remove pytest's own.
    """
    lines = LogLines()
    root = logging.getLogger()
    root.addHandler(json_handler(lines.stream))
    root.setLevel(logging.INFO)
    activity.logger.activity_info_on_message = False  # as `configure_worker_logging` does
    workflow.logger.workflow_info_on_message = False
    return lines


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Puts back the global logging setup that a test changes by calling `configure_logging`."""
    loggers = [logging.getLogger(name) for name in ("", *UVICORN_LOGGERS, UVICORN_ACCESS_LOGGER, SDK_ACTIVITY_LOGGER)]
    saved = [(logger, logger.handlers[:], logger.propagate, logger.level, logger.disabled) for logger in loggers]
    saved_temporal = (activity.logger.activity_info_on_message, workflow.logger.workflow_info_on_message)
    try:
        yield
    finally:
        for logger, handlers, propagate, level, disabled in saved:
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)
            logger.disabled = disabled
        activity.logger.activity_info_on_message, workflow.logger.workflow_info_on_message = saved_temporal
