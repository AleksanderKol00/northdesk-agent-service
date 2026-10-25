import json

from pydantic import ValidationError

from northdesk_ticket_agent.agent.errors import (
    AgentStepLimitError,
    DecisionParseError,
    UnknownCustomerError,
)
from northdesk_ticket_agent.agent.prompts import SYSTEM_PROMPT, render_ticket
from northdesk_ticket_agent.agent.tools import (
    SUBMIT_DECISION,
    TOOL_SPECS,
    ToolExecutor,
    format_validation_error,
)
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.domain.models import ProposedDecision, Ticket
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.llm.base import LLMClient, Message
from northdesk_ticket_agent.services.llm.factory import build_llm_client

_NO_DECISION_REMINDER = f"Reply only with tool calls. Call {SUBMIT_DECISION} to finish."


class TicketAgent:
    """Asks the LLM what to do with a ticket, running its tool calls until it decides."""

    def __init__(
        self,
        llm: LLMClient,
        store: DataStore,
        max_steps: int = 8,
        max_decision_attempts: int = 3,
    ) -> None:
        self._llm = llm
        self._store = store
        self._max_steps = max_steps
        self._max_decision_attempts = max_decision_attempts

    @classmethod
    def from_settings(cls, settings: Settings, store: DataStore) -> TicketAgent:
        return cls(
            llm=build_llm_client(settings),
            store=store,
            max_steps=settings.agent_max_steps,
            max_decision_attempts=settings.agent_max_decision_attempts,
        )

    async def decide(self, ticket: Ticket) -> ProposedDecision:
        if self._store.get_customer(ticket.customer_id) is None:
            raise UnknownCustomerError(f"unknown customer {ticket.customer_id}")

        tools = ToolExecutor(self._store, ticket)
        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=render_ticket(ticket)),
        ]
        failed_attempts = 0

        for _ in range(self._max_steps):
            response = await self._llm.complete(messages, TOOL_SPECS)
            messages.append(response.as_message())

            if not response.tool_calls:
                failed_attempts += 1
                self._raise_if_out_of_attempts(failed_attempts, "model answered without a tool")
                messages.append(Message(role="user", content=_NO_DECISION_REMINDER))
                continue

            decision: ProposedDecision | None = None
            for call in response.tool_calls:
                if call.name != SUBMIT_DECISION:
                    result = tools.run(call.name, call.arguments)
                elif decision is not None:
                    result = json.dumps({"error": "only one decision is allowed"})
                else:
                    try:
                        decision = ProposedDecision.model_validate_json(call.arguments)
                        result = json.dumps({"status": "received"})
                    except ValidationError as exc:
                        failed_attempts += 1
                        self._raise_if_out_of_attempts(failed_attempts, str(exc))
                        result = format_validation_error(exc)
                # Every tool call gets an answer, so the conversation stays well formed.
                messages.append(Message(role="tool", tool_call_id=call.id, content=result))

            if decision is not None:
                return decision

        raise AgentStepLimitError(f"no decision after {self._max_steps} steps")

    def _raise_if_out_of_attempts(self, failed_attempts: int, detail: str) -> None:
        if failed_attempts >= self._max_decision_attempts:
            raise DecisionParseError(
                f"no valid decision after {failed_attempts} attempts; last problem: {detail}"
            )
