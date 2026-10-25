import io
import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio import activity, workflow

from northdesk_ticket_agent.logs import (
    UVICORN_ACCESS_LOGGER,
    JsonFormatter,
    bind_log_context,
    configure_logging,
    log_context,
)
from northdesk_ticket_agent.orchestration.client import CORE_LOGGER, core_logging
from northdesk_ticket_agent.orchestration.worker import configure_worker_logging

logger = logging.getLogger("tests.logs")


def record(message: str = "hello", **extra: Any) -> logging.LogRecord:
    return logger.makeRecord(logger.name, logging.INFO, __file__, 1, message, (), None, extra=extra)


def formatted(rec: logging.LogRecord) -> dict[str, Any]:
    line: dict[str, Any] = json.loads(JsonFormatter().format(rec))
    return line


def test_a_line_has_time_level_logger_and_message() -> None:
    line = formatted(record("ticket %s", ticket_id="T-1"))

    assert line["level"] == "INFO"
    assert line["logger"] == "tests.logs"
    assert line["message"] == "ticket %s"
    assert line["ticket_id"] == "T-1"
    assert line["time"].endswith("+00:00")


def test_extra_fields_are_kept_and_internal_ones_dropped() -> None:
    line = formatted(
        record(action="refund", __temporal_error_identifier="ActivityFailure", color_message="\x1b[1mhello\x1b[0m")
    )

    assert line["action"] == "refund"
    assert not any(key.startswith("_") for key in line)
    assert "color_message" not in line


def test_values_json_cannot_encode_are_written_as_text() -> None:
    line = formatted(record(path=object()))

    assert line["path"].startswith("<object object")


def test_an_exception_and_a_stack_are_written_out() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    rec.stack_info = "Stack (most recent call last): here"

    line = formatted(rec)

    assert "ValueError: boom" in line["exception"]
    assert line["stack"].startswith("Stack")


def test_log_context_fields_reach_every_line_inside_it_only() -> None:
    with log_context(ticket_id="T-1", workflow_id="T-1"):
        with log_context(step="act"):
            inner = formatted(record())
        outer = formatted(record())
    after = formatted(record())

    assert (inner["ticket_id"], inner["workflow_id"], inner["step"]) == ("T-1", "T-1", "act")
    assert outer["ticket_id"] == "T-1"
    assert "step" not in outer
    assert "ticket_id" not in after


def test_explicit_extra_wins_over_the_context() -> None:
    with log_context(ticket_id="T-1"):
        line = formatted(record(ticket_id="T-2"))

    assert line["ticket_id"] == "T-2"


def test_bind_adds_to_the_innermost_context_for_later_lines() -> None:
    with log_context():
        bind_log_context(ticket_id="T-1")
        line = formatted(record())

    assert line["ticket_id"] == "T-1"


def test_bind_outside_a_context_does_nothing() -> None:
    bind_log_context(ticket_id="T-1")

    assert "ticket_id" not in formatted(record())


def test_the_ticket_workflows_ids_are_lifted_from_temporal_details() -> None:
    details = {"workflow_id": "T-1", "workflow_type": "TicketWorkflow", "attempt": 2}

    from_workflow = formatted(record(temporal_workflow=details))
    from_activity = formatted(record(temporal_activity=details))

    for line in (from_workflow, from_activity):
        assert line["workflow_id"] == "T-1"
        assert line["ticket_id"] == "T-1"
    assert from_activity["temporal_activity"]["attempt"] == 2


def test_a_line_from_temporal_core_keeps_its_target_and_fields() -> None:
    core = SimpleNamespace(target="temporalio_sdk_core::worker", fields={"workflow_id": "T-1", "attempt": 3})

    line = formatted(record("Failing workflow task", temporal_log=core))

    assert line["message"] == "Failing workflow task"
    assert line["core_target"] == "temporalio_sdk_core::worker"
    assert line["core_fields"] == {"workflow_id": "T-1", "attempt": 3}
    assert line["workflow_id"] == "T-1"
    assert "temporal_log" not in line


def test_core_logs_are_forwarded_to_python_logging() -> None:
    config = core_logging()

    assert config.forwarding is not None
    assert config.forwarding.logger.name == CORE_LOGGER
    assert not config.forwarding.append_log_fields_to_message  # they become fields of the JSON line instead


def test_another_workflow_gets_no_ticket_id() -> None:
    line = formatted(record(temporal_workflow={"workflow_id": "other", "workflow_type": "OtherWorkflow"}))

    assert line["workflow_id"] == "other"
    assert "ticket_id" not in line


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_sends_every_logger_through_one_json_handler() -> None:
    logging.getLogger("uvicorn").addHandler(logging.StreamHandler(io.StringIO()))
    stream = io.StringIO()

    configure_logging("WARNING", stream)
    logging.getLogger("uvicorn.error").warning("from uvicorn")
    logging.getLogger("tests.logs").info("below the level")
    logging.getLogger(UVICORN_ACCESS_LOGGER).warning("replaced by our access line")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["logger"], line["message"]) for line in lines] == [("uvicorn.error", "from uvicorn")]
    assert logging.getLogger("uvicorn").handlers == []


@pytest.mark.usefixtures("restore_logging")
def test_the_worker_keeps_temporal_details_out_of_the_message() -> None:
    configure_worker_logging("INFO")

    assert activity.logger.activity_info_on_message is False
    assert workflow.logger.workflow_info_on_message is False
