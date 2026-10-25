from typing import Annotated

from fastapi import Depends, Request

from northdesk_ticket_agent.logs import bind_log_context
from northdesk_ticket_agent.orchestration.tickets import TicketWorkflows


def get_workflows(request: Request) -> TicketWorkflows:
    """The `TicketWorkflows` the app's lifespan put on `app.state`."""
    workflows: TicketWorkflows = request.app.state.workflows
    return workflows


Workflows = Annotated[TicketWorkflows, Depends(get_workflows)]


async def ticket_id_in_logs(ticket_id: str) -> str:
    """The `{ticket_id}` path parameter, bound to the request's log context with its workflow ID (the same value), so
    every line about the request carries both, the access line included."""
    bind_log_context(ticket_id=ticket_id, workflow_id=ticket_id)
    return ticket_id


TicketId = Annotated[str, Depends(ticket_id_in_logs)]
