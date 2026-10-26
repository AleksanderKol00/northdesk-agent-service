import pytest
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from northdesk_ticket_agent.domain.models import EscalateAction, RefundAction, ReplyAction
from northdesk_ticket_agent.orchestration.models import ACT_ACTIVITY, Approval
from northdesk_ticket_agent.orchestration.workflow import (
    _action_failure,
    _approval_outcome,
    _describe,
    _error_type,
)


def activity_error(cause: BaseException | None) -> ActivityError:
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type=ACT_ACTIVITY,
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


REFUND = RefundAction(invoice_id="INV-1002", amount_eur=49.0)


@pytest.mark.parametrize(
    ("approval", "expected"),
    [
        (Approval(approved=True), REFUND),
        (
            Approval(approved=False, comment="charged once only"),
            EscalateAction(reason="The refund of 49.00 EUR on invoice INV-1002 was rejected: charged once only"),
        ),
        (
            Approval(approved=False),
            EscalateAction(reason="The refund of 49.00 EUR on invoice INV-1002 was rejected"),
        ),
        # Also what the workflow does when an approval arrives in the same activation as the 24 h timer: it counts.
        (None, EscalateAction(reason="No approval within 24 h for the refund of 49.00 EUR on invoice INV-1002")),
    ],
)
def test_approval_outcome(approval: Approval | None, expected: object) -> None:
    assert _approval_outcome(approval, REFUND) == expected


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            ApplicationError("key was used for a different request", type="HelpdeskConflictError", non_retryable=True),
            "Could not carry out reply: HelpdeskConflictError: key was used for a different request",
        ),
        (
            ApplicationError("response was lost", type="HelpdeskTemporaryError"),
            "Gave up on reply, which may have been carried out; check the Helpdesk before doing it again: "
            "HelpdeskTemporaryError: response was lost",
        ),
        (
            TimeoutError("activity StartToClose timeout", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            "Gave up on reply, which may have been carried out; check the Helpdesk before doing it again: "
            "activity StartToClose timeout",
        ),
    ],
)
def test_action_failure_says_whether_the_action_may_have_happened(cause: BaseException, expected: str) -> None:
    action = ReplyAction(message="See KB-1.", article_ids=["KB-1"])
    assert _action_failure(action, activity_error(cause)) == expected


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (ApplicationError("unknown customer C-999", type="UnknownCustomerError"), "UnknownCustomerError"),
        (
            TimeoutError("activity StartToClose timeout", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]),
            "TimeoutError",
        ),
        (None, "ActivityError"),
    ],
)
def test_error_type_names_the_error_without_its_message(cause: BaseException | None, expected: str) -> None:
    assert _error_type(activity_error(cause)) == expected
