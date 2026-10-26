from pydantic import BaseModel

from northdesk_ticket_agent.domain.models import Action, Decision
from northdesk_ticket_agent.orchestration.models import Approval, Step, TicketState


class TicketStarted(BaseModel):
    ticket_id: str
    workflow_id: str
    created: bool  # False when the ticket was already sent: the existing workflow is returned, not a new one


class TicketStatus(BaseModel):
    """A ticket's state as the API shows it. Its own model, so the workflow's state can change without changing it."""

    ticket_id: str
    workflow_id: str
    step: Step
    decision: Decision | None
    approval: Approval | None
    action: Action | None
    helpdesk_reference: str | None  # set once the action is carried out
    error: str | None

    @classmethod
    def from_state(cls, state: TicketState) -> TicketStatus:
        return cls(
            ticket_id=state.ticket_id,
            workflow_id=state.ticket_id,
            step=state.step,
            decision=state.decision,
            approval=state.approval,
            action=state.action,
            helpdesk_reference=state.receipt.reference if state.receipt else None,
            error=state.error,
        )
