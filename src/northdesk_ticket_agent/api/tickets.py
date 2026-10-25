import logging
from typing import Any

from fastapi import APIRouter, Response, status

from northdesk_ticket_agent.api.dependencies import TicketId, Workflows
from northdesk_ticket_agent.api.schemas import TicketStarted, TicketStatus
from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.logs import bind_log_context
from northdesk_ticket_agent.orchestration.workflow import Approval

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"description": "Unknown ticket"}}


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_200_OK: {"model": TicketStarted, "description": "Already sent; nothing new started"}},
)
async def submit_ticket(ticket: Ticket, response: Response, workflows: Workflows) -> TicketStarted:
    """Start processing a ticket and return at once. Sending the same ticket ID again starts nothing new."""
    # The workflow ID is the ticket ID, so it is known before Temporal answers (and logged even if it doesn't).
    bind_log_context(ticket_id=ticket.ticket_id, workflow_id=ticket.ticket_id)
    started = await workflows.start(ticket)
    if started.created:
        logger.info("Ticket accepted; workflow started")
    else:
        logger.info("Ticket was sent before; returning its workflow")
        response.status_code = status.HTTP_200_OK
    return TicketStarted(ticket_id=ticket.ticket_id, workflow_id=started.workflow_id, created=started.created)


@router.get("/{ticket_id}", responses=NOT_FOUND)
async def get_ticket(ticket_id: TicketId, workflows: Workflows) -> TicketStatus:
    """The ticket's current step, its checked decision, and what was done."""
    state = await workflows.status(ticket_id)
    return TicketStatus(workflow_id=ticket_id, **state.model_dump())


@router.post(
    "/{ticket_id}/approval",
    responses={
        **NOT_FOUND,
        status.HTTP_409_CONFLICT: {"description": "No refund is waiting for approval, or it was already answered"},
    },
)
async def approve_refund(ticket_id: TicketId, approval: Approval, workflows: Workflows) -> TicketStatus:
    """Approve or reject the refund the ticket is waiting on. Returns the state with the action that follows."""
    state = await workflows.approve(ticket_id, approval)
    logger.info(
        "Refund %s",
        "approved" if approval.approved else "rejected",
        extra={"approved": approval.approved, "action": state.action.type if state.action else None},
    )
    return TicketStatus(workflow_id=ticket_id, **state.model_dump())
