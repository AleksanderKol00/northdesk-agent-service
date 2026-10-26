"""What the ticket workflow, its activities and its callers exchange: the activity names, their inputs, the workflow's
state and the approval it waits for.

Everything here goes through Temporal's history, so a change must stay readable by workflows already running.
"""

from enum import StrEnum

from pydantic import BaseModel

from northdesk_ticket_agent.domain.models import Action, Decision, ProposedDecision, Ticket
from northdesk_ticket_agent.services.helpdesk import HelpdeskReceipt

# The workflow calls the activities by name, so tests can register fakes under the same names.
DECIDE_ACTIVITY = "ticket.decide"
CHECK_ACTIVITY = "ticket.check"
ACT_ACTIVITY = "ticket.act"


class CheckInput(BaseModel):
    ticket: Ticket
    proposed: ProposedDecision


class ActInput(BaseModel):
    ticket_id: str
    action: Action


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
