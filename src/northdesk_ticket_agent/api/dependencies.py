from typing import Annotated

from fastapi import Depends, Request

from northdesk_ticket_agent.orchestration.tickets import TicketWorkflows


def get_workflows(request: Request) -> TicketWorkflows:
    """The `TicketWorkflows` the app's lifespan put on `app.state`."""
    workflows: TicketWorkflows = request.app.state.workflows
    return workflows


Workflows = Annotated[TicketWorkflows, Depends(get_workflows)]
