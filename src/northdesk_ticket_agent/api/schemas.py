from pydantic import BaseModel

from northdesk_ticket_agent.orchestration.workflow import TicketState


class TicketStarted(BaseModel):
    ticket_id: str
    workflow_id: str
    created: bool  # False when the ticket was already sent: the existing workflow is returned, not a new one


class TicketStatus(TicketState):
    workflow_id: str
