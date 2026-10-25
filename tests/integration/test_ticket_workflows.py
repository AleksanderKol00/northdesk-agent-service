"""`TemporalTicketWorkflows` against Temporal's test server, with the real activities and the fake LLM."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.domain.models import ActionType, ProposedDecision, RefundAction, Ticket
from northdesk_ticket_agent.main import create_app
from northdesk_ticket_agent.orchestration.activities import TicketActivities
from northdesk_ticket_agent.orchestration.errors import (
    NothingToApproveError,
    TicketNotFoundError,
    TicketWorkflowsUnavailableError,
)
from northdesk_ticket_agent.orchestration.tickets import StartedTicket, TemporalTicketWorkflows
from northdesk_ticket_agent.orchestration.workflow import Approval, Step, TicketState, TicketWorkflow
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient

pytestmark = pytest.mark.asyncio(loop_scope="session")


def fresh(ticket: Ticket) -> Ticket:
    """The ticket under a new ID: workflow IDs are ticket IDs, and the test server is shared by the whole session."""
    return ticket.model_copy(update={"ticket_id": f"{ticket.ticket_id}-{uuid.uuid4()}"})


async def wait_for_step(workflows: TemporalTicketWorkflows, ticket_id: str, step: Step) -> TicketState:
    for _ in range(200):  # up to 10 s
        state = await workflows.status(ticket_id)
        if state.step == step:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"the workflow never reached {step}, it is at {state.step}")


async def test_start_uses_the_ticket_id_as_workflow_id(
    temporal_workflows: TemporalTicketWorkflows, workflow_env: WorkflowEnvironment, example_tickets: dict[str, Ticket]
) -> None:
    ticket = fresh(example_tickets["T-1"])

    started = await temporal_workflows.start(ticket)

    assert started == StartedTicket(workflow_id=ticket.ticket_id, created=True)
    state = await workflow_env.client.get_workflow_handle_for(TicketWorkflow.run, ticket.ticket_id).result()
    assert state.step == Step.DONE
    assert state.action is not None
    assert state.action.type == ActionType.REPLY


async def test_the_same_ticket_is_not_processed_twice(
    temporal_workflows: TemporalTicketWorkflows, helpdesk: FakeHelpdesk, example_tickets: dict[str, Ticket]
) -> None:
    ticket = fresh(example_tickets["T-1"])
    await temporal_workflows.start(ticket)

    again_while_running = await temporal_workflows.start(ticket)
    await wait_for_step(temporal_workflows, ticket.ticket_id, Step.DONE)
    again_when_done = await temporal_workflows.start(ticket)

    assert again_while_running == again_when_done == StartedTicket(workflow_id=ticket.ticket_id, created=False)
    assert len(helpdesk.performed) == 1


async def test_a_refund_is_approved_and_issued(
    temporal_workflows: TemporalTicketWorkflows, helpdesk: FakeHelpdesk, example_tickets: dict[str, Ticket]
) -> None:
    ticket = fresh(example_tickets["T-2"])
    await temporal_workflows.start(ticket)
    waiting = await wait_for_step(temporal_workflows, ticket.ticket_id, Step.AWAITING_APPROVAL)

    answered = await temporal_workflows.approve(ticket.ticket_id, Approval(approved=True))
    done = await wait_for_step(temporal_workflows, ticket.ticket_id, Step.DONE)

    assert isinstance(waiting.action, RefundAction)
    assert answered.approval == Approval(approved=True)
    assert answered.action == waiting.action
    assert done.action == waiting.action
    assert [receipt.action for receipt in helpdesk.performed] == [waiting.action]


async def test_unknown_ticket_is_not_found(temporal_workflows: TemporalTicketWorkflows) -> None:
    with pytest.raises(TicketNotFoundError):
        await temporal_workflows.status("T-404")
    with pytest.raises(TicketNotFoundError):
        await temporal_workflows.approve("T-404", Approval(approved=True))


async def test_nothing_to_approve_while_deciding(
    workflow_env: WorkflowEnvironment, datastore: DataStore, helpdesk: FakeHelpdesk, example_tickets: dict[str, Ticket]
) -> None:
    # `ticket.decide` is held until the approval is refused, so the workflow is surely still running.
    real = TicketActivities(TicketAgent(FakeLLMClient(), datastore), datastore, helpdesk)
    release = asyncio.Event()

    @activity.defn(name="ticket.decide")
    async def held_decide(ticket: Ticket) -> ProposedDecision:
        await release.wait()
        return await real.decide(ticket)

    task_queue = f"test-{uuid.uuid4()}"
    activities: list[Callable[..., Any]] = [held_decide, real.check, real.act]
    async with Worker(workflow_env.client, task_queue=task_queue, workflows=[TicketWorkflow], activities=activities):
        workflows = TemporalTicketWorkflows(workflow_env.client, task_queue)
        ticket = fresh(example_tickets["T-2"])
        await workflows.start(ticket)

        with pytest.raises(NothingToApproveError) as refused:
            await workflows.approve(ticket.ticket_id, Approval(approved=True))
        release.set()
        state = await wait_for_step(workflows, ticket.ticket_id, Step.AWAITING_APPROVAL)
        await workflows.approve(ticket.ticket_id, Approval(approved=False))
        await wait_for_step(workflows, ticket.ticket_id, Step.DONE)  # leaves nothing running on the shared server

    assert isinstance(refused.value.__cause__, WorkflowUpdateFailedError)
    assert state.approval is None


async def test_no_worker_answering_is_unavailable(
    isolated_workflow_env: WorkflowEnvironment, example_tickets: dict[str, Ticket]
) -> None:
    # Started on a task queue no worker polls: the server knows the workflow, but nobody answers for it.
    client = isolated_workflow_env.client
    workflows = TemporalTicketWorkflows(client, f"test-{uuid.uuid4()}", rpc_timeout=timedelta(seconds=1))
    ticket = fresh(example_tickets["T-2"])
    await workflows.start(ticket)

    with pytest.raises(TicketWorkflowsUnavailableError):
        await workflows.status(ticket.ticket_id)
    with pytest.raises(TicketWorkflowsUnavailableError):
        await workflows.approve(ticket.ticket_id, Approval(approved=True))


@workflow.defn(name="TicketWorkflow", sandboxed=False)
class BrokenStatusWorkflow:
    """Registered under the real workflow's name, with a `status` query that fails in the worker."""

    @workflow.run
    async def run(self, ticket: Ticket) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.query(name="status")
    def status(self) -> TicketState:
        raise RuntimeError("the state cannot be read")


async def test_a_failing_query_is_unavailable(
    isolated_workflow_env: WorkflowEnvironment, example_tickets: dict[str, Ticket]
) -> None:
    client = isolated_workflow_env.client
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[BrokenStatusWorkflow]):
        workflows = TemporalTicketWorkflows(client, task_queue)
        ticket = fresh(example_tickets["T-1"])
        await workflows.start(ticket)

        with pytest.raises(TicketWorkflowsUnavailableError):
            await workflows.status(ticket.ticket_id)
        await client.get_workflow_handle(ticket.ticket_id).terminate()


async def test_nothing_to_approve_once_finished(
    temporal_workflows: TemporalTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    ticket = fresh(example_tickets["T-1"])
    await temporal_workflows.start(ticket)
    await wait_for_step(temporal_workflows, ticket.ticket_id, Step.DONE)

    with pytest.raises(NothingToApproveError):
        await temporal_workflows.approve(ticket.ticket_id, Approval(approved=True))


async def test_the_api_end_to_end(
    temporal_workflows: TemporalTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    ticket = fresh(example_tickets["T-2"])
    app = create_app(temporal_workflows)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api,
    ):
        started = await api.post("/tickets", json=ticket.model_dump())
        await wait_for_step(temporal_workflows, ticket.ticket_id, Step.AWAITING_APPROVAL)
        approved = await api.post(f"/tickets/{ticket.ticket_id}/approval", json={"approved": True})
        await wait_for_step(temporal_workflows, ticket.ticket_id, Step.DONE)
        status = await api.get(f"/tickets/{ticket.ticket_id}")

    assert started.status_code == 202
    assert approved.status_code == 200
    assert status.status_code == 200
    assert status.json()["step"] == "done"
    assert status.json()["receipt"]["action"]["type"] == "refund"
