"""Temporal activities: the steps of a ticket's life that talk to the outside world.

Each activity turns the errors it knows into an `ApplicationError` whose `type` is the original exception's class name,
marked retryable or not. Temporal then retries only what a retry can fix, and the workflow can tell the reasons apart
(an unknown customer is escalated differently from an outage). Anything unexpected is left as is, so it is retried
under the workflow's retry policy, which caps the number of attempts; except in `check`, which is pure code, so any
error there fails at once.

Each activity runs in a `log_context` with the ticket's IDs, so every line logged meanwhile carries them, including the
agent's and the Helpdesk's. A failed attempt is logged by the SDK itself ("Completing activity as failed"). Lines name
types and counts, never what the model wrote: before `check`, any of it may echo the ticket.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.agent.errors import AgentError
from northdesk_ticket_agent.domain.models import Decision, ProposedDecision, Ticket
from northdesk_ticket_agent.domain.rules import check_decision
from northdesk_ticket_agent.logs import log_context
from northdesk_ticket_agent.orchestration.models import (
    ACT_ACTIVITY,
    CHECK_ACTIVITY,
    DECIDE_ACTIVITY,
    ActInput,
    CheckInput,
)
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError
from northdesk_ticket_agent.services.helpdesk import Helpdesk, HelpdeskReceipt


class TicketActivities:
    """The activities, sharing one agent, data store and Helpdesk client built once by the worker."""

    def __init__(self, agent: TicketAgent, store: DataStore, helpdesk: Helpdesk) -> None:
        self._agent = agent
        self._store = store
        self._helpdesk = helpdesk

    def all(self) -> list[Callable[..., Any]]:
        return [self.decide, self.check, self.act]

    @activity.defn(name=DECIDE_ACTIVITY)
    async def decide(self, ticket: Ticket) -> ProposedDecision:
        with _ticket_log_context(ticket.ticket_id):
            try:
                proposed = await self._agent.decide(ticket)
            except (ServiceError, AgentError) as exc:
                # An AgentError (unknown customer, unreadable decision, step limit) is never temporary: asking the
                # model again won't change the outcome.
                raise _application_error(exc) from exc
            # Not the proposed action: it is an unchecked string from the model. `check` logs the checked one.
            activity.logger.info("Agent proposed a decision")
            return proposed

    @activity.defn(name=CHECK_ACTIVITY)
    async def check(self, request: CheckInput) -> Decision:
        ticket = request.ticket
        with _ticket_log_context(ticket.ticket_id):
            try:
                decision = check_decision(
                    request.proposed,
                    ticket,
                    invoices=self._store.get_invoices(ticket.customer_id),
                    kb_article_ids=self._store.article_ids(),
                )
            except Exception as exc:
                # The checks are pure code on in-memory data: a retry would fail the same way, so fail once. Only the
                # type: the message may quote the model's values.
                raise ApplicationError(
                    f"Checking the decision failed: {type(exc).__name__}", type=type(exc).__name__, non_retryable=True
                ) from exc
            fields = {"action": decision.action.type, "category": decision.category, "priority": decision.priority}
            # Checked values, so they can go on the activity's span: a trace search can then find, say, the refunds.
            trace.get_current_span().set_attributes(
                {f"ticket.{key}": str(value) for key, value in fields.items()}
                | {"ticket.violations": len(decision.violations)}
            )
            if decision.violations:
                # The count only: the violations quote the model's values, which may repeat the ticket's text.
                activity.logger.warning(
                    "Decision broke %d rule(s); escalating",
                    len(decision.violations),
                    extra={**fields, "violations": len(decision.violations)},
                )
            else:
                activity.logger.info("Decision passed the checks: %s", decision.action.type, extra=fields)
            return decision

    @activity.defn(name=ACT_ACTIVITY)
    async def act(self, request: ActInput) -> HelpdeskReceipt:
        # One key per ticket and action type: a retry after a lost response returns the first receipt instead of
        # acting twice. The fake Helpdesk keeps keys in memory, so run one worker (see worker.py).
        idempotency_key = f"{request.ticket_id}:{request.action.type}"
        with _ticket_log_context(request.ticket_id):
            # No line here: the Helpdesk logs whether it acted or repeated a receipt, and the workflow logs the outcome.
            try:
                return await self._helpdesk.perform(request.ticket_id, request.action, idempotency_key)
            except ServiceError as exc:
                raise _application_error(exc) from exc


def _ticket_log_context(ticket_id: str) -> AbstractContextManager[None]:
    return log_context(ticket_id=ticket_id, workflow_id=activity.info().workflow_id)


def _application_error(exc: ServiceError | AgentError) -> ApplicationError:
    retryable = isinstance(exc, TemporaryServiceError)
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=not retryable)
