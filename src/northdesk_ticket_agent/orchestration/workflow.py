"""The ticket workflow: decide → check → wait for approval (refunds only) → act.

The workflow holds no business logic of its own and never talks to the outside world: the steps that do run as
activities, retried under the policies below. What the workflow guarantees is the order: a decision is always checked
before anything acts on it, and a refund is never issued without a person's approval.

Anything that goes wrong before the action (an unknown customer, an unreadable answer, an LLM that stays down) turns
the ticket into an escalation with the reason, so a person picks it up. If the action itself fails for good, the
workflow fails with the reason; the state, readable through the `status` query, says where it stopped.
"""

from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel
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
    from northdesk_ticket_agent.orchestration.activities import ActInput, CheckInput
    from northdesk_ticket_agent.services.helpdesk import HelpdeskReceipt

APPROVAL_TIMEOUT = timedelta(hours=24)

# The agent loop runs in one activity: up to `agent_max_steps` LLM calls, each with its own timeout.
DECIDE_TIMEOUT = timedelta(minutes=5)
CHECK_TIMEOUT = timedelta(seconds=10)
ACT_TIMEOUT = timedelta(seconds=30)

# Errors a retry cannot fix are raised non-retryable by the activities, so these limits only cap temporary failures.
DECIDE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_attempts=3)
CHECK_RETRY = RetryPolicy(maximum_attempts=3)
ACT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


class Step(StrEnum):
    DECIDING = "deciding"
    CHECKING = "checking"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTING = "acting"
    DONE = "done"
    FAILED = "failed"


class Approval(BaseModel):
    approved: bool
    comment: str | None = None


class TicketState(BaseModel):
    """Where a ticket is, and what was decided and done. Returned by the `status` query and as the workflow result.

    `decision` is the checked decision (None when the agent could not decide). `action` is what is carried out; it
    differs from `decision.action` when a refund is rejected or not answered in time, or when deciding failed.
    """

    ticket_id: str
    step: Step = Step.DECIDING
    decision: Decision | None = None
    approval: Approval | None = None
    action: Action | None = None
    receipt: HelpdeskReceipt | None = None
    error: str | None = None


@workflow.defn(name="TicketWorkflow")
class TicketWorkflow:
    @workflow.init
    def __init__(self, ticket: Ticket) -> None:
        # Built in __init__, so the `status` query and the `approve` update always find a state.
        self._state = TicketState(ticket_id=ticket.ticket_id)

    @workflow.run
    async def run(self, ticket: Ticket) -> TicketState:
        state = self._state

        try:
            proposed = await workflow.execute_activity(
                "ticket.decide",
                ticket,
                result_type=ProposedDecision,
                start_to_close_timeout=DECIDE_TIMEOUT,
                retry_policy=DECIDE_RETRY,
            )
            state.step = Step.CHECKING
            state.decision = await workflow.execute_activity(
                "ticket.check",
                CheckInput(ticket=ticket, proposed=proposed),
                result_type=Decision,
                start_to_close_timeout=CHECK_TIMEOUT,
                retry_policy=CHECK_RETRY,
            )
        except ActivityError as err:
            state.action = EscalateAction(reason=f"Could not decide automatically: {_describe(err)}")
        else:
            state.action = state.decision.action
            if isinstance(state.action, RefundAction):
                state.action = await self._wait_for_approval(state, state.action)

        state.step = Step.ACTING
        try:
            state.receipt = await workflow.execute_activity(
                "ticket.act",
                ActInput(ticket_id=ticket.ticket_id, action=state.action),
                result_type=HelpdeskReceipt,
                start_to_close_timeout=ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
        except ActivityError as err:
            state.step = Step.FAILED
            state.error = f"Could not carry out {state.action.type}: {_describe(err)}"
            raise ApplicationError(state.error, type="ActionFailed", non_retryable=True) from err

        state.step = Step.DONE
        return state

    async def _wait_for_approval(self, state: TicketState, refund: RefundAction) -> Action:
        """Return the refund once a person approves it; an escalation if they reject it or don't answer in time."""
        state.step = Step.AWAITING_APPROVAL
        what = f"refund of {refund.amount_eur:.2f} EUR on invoice {refund.invoice_id}"
        try:
            await workflow.wait_condition(lambda: state.approval is not None, timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            return EscalateAction(reason=f"No approval within {_hours(APPROVAL_TIMEOUT)} h for the {what}")

        approval = state.approval
        if approval is not None and approval.approved:
            return refund
        comment = approval.comment if approval is not None else None
        return EscalateAction(reason=f"The {what} was rejected" + (f": {comment}" if comment else ""))

    @workflow.query(name="status")
    def status(self) -> TicketState:
        return self._state

    @workflow.update(name="approve")
    def approve(self, approval: Approval) -> TicketState:
        """Approve or reject the refund the workflow is waiting on. Refused by the validator when nothing is waiting."""
        self._state.approval = approval
        return self._state

    @approve.validator
    def _only_while_awaiting(self, approval: Approval) -> None:
        if self._state.step != Step.AWAITING_APPROVAL or self._state.approval is not None:
            raise ValueError("no refund is waiting for approval")


def _describe(err: ActivityError) -> str:
    """The reason an activity failed: the original error type and message, or Temporal's own (e.g. a timeout)."""
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type:
        return f"{cause.type}: {cause.message}"
    return str(cause or err)


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)
