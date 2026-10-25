from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.main import create_app
from northdesk_ticket_agent.orchestration.errors import (
    NothingToApproveError,
    TicketNotFoundError,
    TicketWorkflowsUnavailableError,
)
from northdesk_ticket_agent.orchestration.tickets import StartedTicket
from northdesk_ticket_agent.orchestration.workflow import Approval, Step, TicketState


class StubTicketWorkflows:
    """An in-memory stand-in for the Temporal side of the API: started tickets wait at `deciding` until a test moves
    them on through `states`, and an approval is taken only while a refund is waiting, as the workflow does. With
    `down` set, every call fails as it does when Temporal or the worker does not answer."""

    def __init__(self) -> None:
        self.started: list[Ticket] = []
        self.states: dict[str, TicketState] = {}
        self.down = False

    def _check_up(self) -> None:
        if self.down:
            raise TicketWorkflowsUnavailableError

    async def start(self, ticket: Ticket) -> StartedTicket:
        self._check_up()
        if ticket.ticket_id in self.states:
            return StartedTicket(workflow_id=ticket.ticket_id, created=False)
        self.started.append(ticket)
        self.states[ticket.ticket_id] = TicketState(ticket_id=ticket.ticket_id)
        return StartedTicket(workflow_id=ticket.ticket_id, created=True)

    async def status(self, ticket_id: str) -> TicketState:
        self._check_up()
        if ticket_id not in self.states:
            raise TicketNotFoundError(ticket_id)
        return self.states[ticket_id]

    async def approve(self, ticket_id: str, approval: Approval) -> TicketState:
        state = await self.status(ticket_id)
        if state.step != Step.AWAITING_APPROVAL or state.approval is not None:
            raise NothingToApproveError(ticket_id)
        state.approval = approval
        return state


@pytest.fixture
def workflows() -> StubTicketWorkflows:
    return StubTicketWorkflows()


@pytest.fixture
def client(workflows: StubTicketWorkflows) -> Iterator[TestClient]:
    with TestClient(create_app(workflows)) as test_client:
        yield test_client
