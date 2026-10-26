"""The ticket workflow: decide → check → wait for approval (refunds only) → act.

The workflow holds no business logic of its own and never talks to the outside world: the steps that do run as
activities, retried under the policies below. What the workflow guarantees is the order: a decision is always checked
before anything acts on it, and a refund is never issued without a person's approval.

Anything that goes wrong before the action (an unknown customer, an unreadable answer, an LLM that stays down) turns
the ticket into an escalation with the reason, so a person picks it up. If the action itself fails for good, the
workflow fails with the reason; the state, readable through the `status` query, says where it stopped.

It logs through `workflow.logger`, which stays quiet during replay and tags each line with the workflow, which the JSON
logs turn into its `workflow_id` and `ticket_id`.
"""

from contextlib import suppress
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from northdesk_ticket_agent.domain.models import (
        Action,
        Decision,
        EscalateAction,
        ProposedDecision,
        RefundAction,
        Ticket,
    )
    from northdesk_ticket_agent.orchestration.models import (
        ACT_ACTIVITY,
        CHECK_ACTIVITY,
        DECIDE_ACTIVITY,
        ActInput,
        Approval,
        CheckInput,
        Step,
        TicketState,
    )
    from northdesk_ticket_agent.services.helpdesk import HelpdeskReceipt

APPROVAL_TIMEOUT = timedelta(hours=24)

# The agent loop runs in one activity: up to `agent_max_steps` LLM calls, each with its own timeout.
DECIDE_TIMEOUT = timedelta(minutes=5)
CHECK_TIMEOUT = timedelta(seconds=10)
ACT_TIMEOUT = timedelta(seconds=30)

# Errors a retry cannot fix are raised non-retryable by the activities, so these limits only cap temporary failures.
# They are long enough to ride out a short outage: about 1.5 min of backoff for the LLM, about 15 min for the Helpdesk.
# Giving up on `decide` only costs an escalation; giving up on `act` fails the ticket, so it waits longer.
DECIDE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=8,
)
CHECK_RETRY = RetryPolicy(maximum_attempts=3)
ACT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=20,
)


@workflow.defn
class TicketWorkflow:
    @workflow.init
    def __init__(self, ticket: Ticket) -> None:
        # Built in __init__, so the `status` query and the `approve` update always find a state.
        self._state = TicketState(ticket_id=ticket.ticket_id)

    @workflow.run
    async def run(self, ticket: Ticket) -> TicketState:
        state = self._state
        workflow.logger.info("Ticket workflow started")

        try:
            proposed = await workflow.execute_activity(
                DECIDE_ACTIVITY,
                ticket,
                result_type=ProposedDecision,
                start_to_close_timeout=DECIDE_TIMEOUT,
                retry_policy=DECIDE_RETRY,
            )
            state.step = Step.CHECKING
            state.decision = await workflow.execute_activity(
                CHECK_ACTIVITY,
                CheckInput(ticket=ticket, proposed=proposed),
                result_type=Decision,
                start_to_close_timeout=CHECK_TIMEOUT,
                retry_policy=CHECK_RETRY,
            )
        except ActivityError as err:
            state.action = EscalateAction(reason=f"Could not decide automatically: {_describe(err)}")
            # The type only: the message can quote the model's arguments (an unreadable decision), and so the ticket.
            workflow.logger.warning("Could not decide automatically; escalating", extra={"error": _error_type(err)})
        else:
            state.action = state.decision.action
            if isinstance(state.action, RefundAction):
                state.action = await self._wait_for_approval(state, state.action)

        state.step = Step.ACTING
        try:
            state.receipt = await workflow.execute_activity(
                ACT_ACTIVITY,
                ActInput(ticket_id=ticket.ticket_id, action=state.action),
                result_type=HelpdeskReceipt,
                start_to_close_timeout=ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
        except ActivityError as err:
            state.step = Step.FAILED
            state.error = _action_failure(state.action, err)
            workflow.logger.error("Action failed for good: %s", state.error, extra={"action": state.action.type})
            raise ApplicationError(state.error, type="ActionFailed", non_retryable=True) from err

        state.step = Step.DONE
        workflow.logger.info(
            "Ticket done: %s",
            state.action.type,
            extra={"action": state.action.type, "reference": state.receipt.reference},
        )
        return state

    async def _wait_for_approval(self, state: TicketState, refund: RefundAction) -> Action:
        """Return the refund once a person approves it; an escalation if they reject it or don't answer in time."""
        state.step = Step.AWAITING_APPROVAL
        workflow.logger.info(
            "Waiting for approval of a refund", extra={"invoice_id": refund.invoice_id, "amount_eur": refund.amount_eur}
        )
        # An answer that arrives together with the timer was already accepted by the update, so it still counts.
        with suppress(TimeoutError):
            await workflow.wait_condition(lambda: state.approval is not None, timeout=APPROVAL_TIMEOUT)
        outcome = _approval_outcome(state.approval, refund)
        # Not the reason: it carries the reviewer's free-text comment.
        if state.approval is None:
            workflow.logger.info("No answer in time; escalating the refund")
        elif state.approval.approved:
            workflow.logger.info("Refund approved")
        else:
            workflow.logger.info("Refund rejected; escalating")
        return outcome

    @workflow.query(name="status")
    def status(self) -> TicketState:
        return self._state

    # The update is named after the method ("approve"). Passing `name=` would lose the async typing of the result.
    @workflow.update
    async def approve(self, approval: Approval) -> TicketState:
        """Approve or reject the refund the workflow is waiting on. Refused by the validator when nothing is waiting.

        Returns once the workflow has moved past the approval, so the state shows the action that follows from it.
        """
        self._state.approval = approval
        await workflow.wait_condition(lambda: self._state.step != Step.AWAITING_APPROVAL)
        return self._state

    @approve.validator
    def _only_while_awaiting(self, approval: Approval) -> None:
        answered = self._state.approval
        if answered is not None:
            raise ValueError(f"the refund was already {'approved' if answered.approved else 'rejected'}")
        if self._state.step != Step.AWAITING_APPROVAL:
            raise ValueError("no refund is waiting for approval")


def _approval_outcome(approval: Approval | None, refund: RefundAction) -> Action:
    """The refund if a person approved it; otherwise an escalation saying it was rejected or not answered in time."""
    if approval is not None and approval.approved:
        return refund
    what = f"refund of {refund.amount_eur:.2f} EUR on invoice {refund.invoice_id}"
    if approval is None:
        return EscalateAction(reason=f"No approval within {_hours(APPROVAL_TIMEOUT)} h for the {what}")
    return EscalateAction(reason=f"The {what} was rejected" + (f": {approval.comment}" if approval.comment else ""))


def _action_failure(action: Action, err: ActivityError) -> str:
    """Why the action failed for good, and whether it may still have happened.

    A non-retryable error (a conflicting idempotency key) is raised before the Helpdesk acts. After temporary errors or
    timeouts the last attempt may have acted and lost its response, so the action may have been carried out.
    """
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.non_retryable:
        return f"Could not carry out {action.type}: {_describe(err)}"
    return (
        f"Gave up on {action.type}, which may have been carried out; check the Helpdesk before doing it again: "
        f"{_describe(err)}"
    )


def _describe(err: ActivityError) -> str:
    """The reason an activity failed: the original error type and message, or Temporal's own (e.g. a timeout)."""
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type:
        return f"{cause.type}: {cause.message}"
    return str(cause or err)


def _error_type(err: ActivityError) -> str:
    """The original error's type (e.g. `UnknownCustomerError`), or Temporal's own (e.g. `TimeoutError`)."""
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type:
        return cause.type
    return type(cause or err).__name__


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)
