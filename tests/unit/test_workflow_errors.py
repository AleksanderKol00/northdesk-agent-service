import pytest
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from northdesk_ticket_agent.orchestration.workflow import _describe


def activity_error(cause: BaseException | None) -> ActivityError:
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="ticket.act",
        activity_id="1",
        retry_state=None,
    )
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            ApplicationError("unknown customer C-999", type="UnknownCustomerError"),
            "UnknownCustomerError: unknown customer C-999",
        ),
        (
            TimeoutError("activity StartToClose timeout", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            "activity StartToClose timeout",
        ),
        (None, "Activity task failed"),
    ],
)
def test_describe_names_the_reason_an_activity_failed(cause: BaseException | None, expected: str) -> None:
    assert _describe(activity_error(cause)) == expected
