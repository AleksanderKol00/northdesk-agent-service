import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.domain.models import (
    ActionType,
    Decision,
    EscalateAction,
    ProposedDecision,
    RefundAction,
    ReplyAction,
    Ticket,
)
from northdesk_ticket_agent.domain.rules import check_decision
from northdesk_ticket_agent.orchestration.activities import ActInput, CheckInput, TicketActivities
from northdesk_ticket_agent.orchestration.workflow import (
    ACT_RETRY,
    DECIDE_RETRY,
    Approval,
    Step,
    TicketState,
    TicketWorkflow,
)
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk, HelpdeskReceipt
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPLY = ProposedDecision(
    category="account", priority="normal", action="reply", message="See KB-1.", article_ids=["KB-1"]
)
REFUND = ProposedDecision(
    category="billing", priority="normal", action="refund", invoice_id="INV-1002", amount_eur=49.0
)


def temporary(message: str = "down for a moment") -> ApplicationError:
    return ApplicationError(message, type="HelpdeskTemporaryError")


def permanent(error_type: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=error_type, non_retryable=True)


class FakeActivities:
    """Stand-ins for the three activities, under the same names. Errors are raised the way the real activities raise
    them: an `ApplicationError` typed with the original class name."""

    def __init__(
        self,
        datastore: DataStore,
        proposed: ProposedDecision,
        decide_errors: list[ApplicationError] | None = None,
        act_errors: list[ApplicationError] | None = None,
    ) -> None:
        self._datastore = datastore
        self._proposed = proposed
        self._decide_errors = list(decide_errors or [])
        self._act_errors = list(act_errors or [])
        self.decide_attempts = 0
        self.act_attempts = 0
        self.acted: list[ActInput] = []

    def all(self) -> list[Callable[..., Any]]:
        return [self.decide, self.check, self.act]

    @activity.defn(name="ticket.decide")
    async def decide(self, ticket: Ticket) -> ProposedDecision:
        self.decide_attempts += 1
        if self._decide_errors:
            raise self._decide_errors.pop(0)
        return self._proposed

    @activity.defn(name="ticket.check")
    async def check(self, request: CheckInput) -> Decision:
        store = self._datastore
        invoices = store.get_invoices(request.ticket.customer_id)
        return check_decision(request.proposed, request.ticket, invoices, store.article_ids())

    @activity.defn(name="ticket.act")
    async def act(self, request: ActInput) -> HelpdeskReceipt:
        self.act_attempts += 1
        if self._act_errors:
            raise self._act_errors.pop(0)
        self.acted.append(request)
        return HelpdeskReceipt(reference="REF-1", ticket_id=request.ticket_id, action=request.action)


@asynccontextmanager
async def running(
    env: WorkflowEnvironment, activities: list[Callable[..., Any]], ticket: Ticket
) -> AsyncIterator[WorkflowHandle[TicketWorkflow, TicketState]]:
    """Run a worker with the given activities and start the workflow for the ticket."""
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=task_queue, workflows=[TicketWorkflow], activities=activities):
        yield await env.client.start_workflow(
            TicketWorkflow.run,
            ticket,
            id=f"{ticket.ticket_id}-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def wait_for_step(handle: WorkflowHandle[TicketWorkflow, TicketState], step: Step) -> TicketState:
    for _ in range(200):  # up to 10 s; the workflow runs on the test server, so polling is the only way to watch it
        state = await handle.query(TicketWorkflow.status)
        if state.step == step:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"the workflow never reached {step}, it is at {state.step}")


async def test_answered_ticket(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REPLY)

    async with running(workflow_env, fakes.all(), example_tickets["T-1"]) as handle:
        state = await handle.result()

    reply = ReplyAction(message="See KB-1.", article_ids=["KB-1"])
    assert state.step == Step.DONE
    assert state.decision is not None
    assert state.decision.action == reply
    assert state.action == reply
    assert state.receipt is not None
    assert state.receipt.action == reply
    assert [request.action for request in fakes.acted] == [reply]


async def test_approved_refund_is_issued(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REFUND)

    async with running(workflow_env, fakes.all(), example_tickets["T-2"]) as handle:
        waiting = await wait_for_step(handle, Step.AWAITING_APPROVAL)
        assert fakes.acted == []
        await handle.execute_update(TicketWorkflow.approve, Approval(approved=True))
        state = await handle.result()

    refund = RefundAction(invoice_id="INV-1002", amount_eur=49.0)
    assert waiting.action == refund
    assert state.step == Step.DONE
    assert state.approval == Approval(approved=True)
    assert state.action == refund
    assert [request.action for request in fakes.acted] == [refund]


async def test_rejected_refund_is_escalated(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REFUND)

    async with running(workflow_env, fakes.all(), example_tickets["T-2"]) as handle:
        await wait_for_step(handle, Step.AWAITING_APPROVAL)
        await handle.execute_update(TicketWorkflow.approve, Approval(approved=False, comment="charged once only"))
        state = await handle.result()

    reason = "The refund of 49.00 EUR on invoice INV-1002 was rejected: charged once only"
    assert state.action == EscalateAction(reason=reason)
    assert state.decision is not None
    assert isinstance(state.decision.action, RefundAction)
    assert [request.action for request in fakes.acted] == [EscalateAction(reason=reason)]


async def test_refund_without_an_answer_for_24_hours_is_escalated(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REFUND)

    async with running(workflow_env, fakes.all(), example_tickets["T-2"]) as handle:
        await wait_for_step(handle, Step.AWAITING_APPROVAL)
        before = await workflow_env.get_current_time()
        state = await handle.result()  # the time-skipping server jumps over the 24 h wait
        waited = await workflow_env.get_current_time() - before

    reason = "No approval within 24 h for the refund of 49.00 EUR on invoice INV-1002"
    assert waited.total_seconds() >= 24 * 3600
    assert state.step == Step.DONE
    assert state.approval is None
    assert [request.action for request in fakes.acted] == [EscalateAction(reason=reason)]


async def test_approval_is_refused_when_no_refund_is_waiting(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REFUND)

    async with running(workflow_env, fakes.all(), example_tickets["T-2"]) as handle:
        await wait_for_step(handle, Step.AWAITING_APPROVAL)
        await handle.execute_update(TicketWorkflow.approve, Approval(approved=True))
        with pytest.raises(WorkflowUpdateFailedError):  # a second answer to the same refund
            await handle.execute_update(TicketWorkflow.approve, Approval(approved=False))
        state = await handle.result()

        with pytest.raises(RPCError, match="not found"):  # the workflow is finished
            await handle.execute_update(TicketWorkflow.approve, Approval(approved=True))

    assert state.approval == Approval(approved=True)
    assert len(fakes.acted) == 1


async def test_failed_activity_succeeds_on_retry(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REPLY, decide_errors=[temporary()], act_errors=[temporary(), temporary()])

    async with running(workflow_env, fakes.all(), example_tickets["T-1"]) as handle:
        state = await handle.result()

    assert state.step == Step.DONE
    assert fakes.decide_attempts == 2
    assert fakes.act_attempts == 3
    assert len(fakes.acted) == 1


@pytest.mark.parametrize(
    ("error", "expected_attempts"),
    [
        (permanent("UnknownCustomerError", "unknown customer C-999"), 1),
        (permanent("DecisionParseError", "no readable decision after 3 attempts"), 1),
        (temporary("rate limited"), DECIDE_RETRY.maximum_attempts),  # never succeeds: the retry policy gives up
    ],
)
async def test_ticket_that_cannot_be_decided_is_escalated(
    workflow_env: WorkflowEnvironment,
    datastore: DataStore,
    example_tickets: dict[str, Ticket],
    error: ApplicationError,
    expected_attempts: int,
) -> None:
    fakes = FakeActivities(datastore, REPLY, decide_errors=[error] * DECIDE_RETRY.maximum_attempts)

    async with running(workflow_env, fakes.all(), example_tickets["T-7"]) as handle:
        state = await handle.result()

    reason = f"Could not decide automatically: {error.type}: {error.message}"
    assert fakes.decide_attempts == expected_attempts
    assert state.step == Step.DONE
    assert state.decision is None
    assert state.action == EscalateAction(reason=reason)
    assert [request.action for request in fakes.acted] == [EscalateAction(reason=reason)]


async def test_action_that_keeps_failing_fails_the_workflow(
    workflow_env: WorkflowEnvironment, datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    fakes = FakeActivities(datastore, REPLY, act_errors=[temporary()] * ACT_RETRY.maximum_attempts)

    async with running(workflow_env, fakes.all(), example_tickets["T-1"]) as handle:
        with pytest.raises(WorkflowFailureError) as caught:
            await handle.result()
        state = await handle.query(TicketWorkflow.status)

    error = (
        "Gave up on reply, which may have been carried out; check the Helpdesk before doing it again: "
        "HelpdeskTemporaryError: down for a moment"
    )
    assert fakes.act_attempts == ACT_RETRY.maximum_attempts
    assert state.step == Step.FAILED
    assert state.error == error
    assert isinstance(caught.value.cause, ApplicationError)
    assert caught.value.cause.message == error


@pytest.mark.parametrize(
    ("ticket_id", "action_type", "reason"),
    [
        ("T-1", ActionType.REPLY, None),
        ("T-2", ActionType.ESCALATE, "No approval within 24 h"),
        ("T-3", ActionType.ESCALATE, None),
        ("T-4", ActionType.REPLY, None),
        ("T-5", ActionType.CLOSE, None),
        ("T-6", ActionType.ESCALATE, "Decision failed checks"),
        ("T-7", ActionType.ESCALATE, "UnknownCustomerError"),
    ],
)
async def test_example_tickets_with_the_real_activities(
    workflow_env: WorkflowEnvironment,
    datastore: DataStore,
    example_tickets: dict[str, Ticket],
    ticket_id: str,
    action_type: ActionType,
    reason: str | None,
) -> None:
    # The real activities with the fake LLM: also checks the workflow calls them by their registered names.
    helpdesk = FakeHelpdesk()
    activities = TicketActivities(TicketAgent(FakeLLMClient(), datastore), datastore, helpdesk)

    async with running(workflow_env, activities.all(), example_tickets[ticket_id]) as handle:
        state = await handle.result()

    assert state.step == Step.DONE
    assert state.action is not None
    assert state.action.type == action_type
    if reason is not None:
        assert isinstance(state.action, EscalateAction)
        assert reason in state.action.reason
    assert helpdesk.performed == [state.receipt]
