"""Temporal activities: the steps of a ticket's life that talk to the outside world.

Each activity turns the errors it knows into an `ApplicationError` whose `type` is the original exception's class name,
marked retryable or not. Temporal then retries only what a retry can fix, and the workflow can tell the reasons apart
(an unknown customer is escalated differently from an outage). Anything unexpected is left as is, so it is retried
under the workflow's retry policy, which caps the number of attempts.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.agent.errors import AgentError
from northdesk_ticket_agent.domain.models import Action, Decision, ProposedDecision, Ticket
from northdesk_ticket_agent.domain.rules import check_decision
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk, HelpdeskReceipt


class CheckInput(BaseModel):
    ticket: Ticket
    proposed: ProposedDecision


class ActInput(BaseModel):
    ticket_id: str
    action: Action


class TicketActivities:
    """The activities, sharing one agent, data store and Helpdesk client built once by the worker."""

    def __init__(self, agent: TicketAgent, store: DataStore, helpdesk: FakeHelpdesk) -> None:
        self._agent = agent
        self._store = store
        self._helpdesk = helpdesk

    def all(self) -> list[Callable[..., Any]]:
        return [self.decide, self.check, self.act]

    @activity.defn(name="ticket.decide")
    async def decide(self, ticket: Ticket) -> ProposedDecision:
        try:
            return await self._agent.decide(ticket)
        except (ServiceError, AgentError) as exc:
            # An AgentError (unknown customer, unreadable decision, step limit) is never temporary: asking the model
            # again won't change the outcome.
            raise _application_error(exc) from exc

    @activity.defn(name="ticket.check")
    async def check(self, request: CheckInput) -> Decision:
        ticket = request.ticket
        return check_decision(
            request.proposed,
            ticket,
            invoices=self._store.get_invoices(ticket.customer_id),
            kb_article_ids=self._store.article_ids(),
        )

    @activity.defn(name="ticket.act")
    async def act(self, request: ActInput) -> HelpdeskReceipt:
        # One key per ticket and action type: a retry after a lost response returns the first receipt instead of
        # acting twice, and a different action of the same type on the same ticket is refused as a conflict. The fake
        # Helpdesk keeps keys in memory, so this holds only while one worker process handles every retry (see README).
        idempotency_key = f"{request.ticket_id}:{request.action.type}"
        try:
            return await self._helpdesk.perform(request.ticket_id, request.action, idempotency_key)
        except ServiceError as exc:
            raise _application_error(exc) from exc


def _application_error(exc: ServiceError | AgentError) -> ApplicationError:
    retryable = isinstance(exc, TemporaryServiceError)
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=not retryable)
