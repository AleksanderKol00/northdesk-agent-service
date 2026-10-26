import json

from pydantic import ValidationError

from northdesk_ticket_agent.domain.models import ActionType, Category, Priority, Ticket
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.llm.base import ToolSpec

SUBMIT_DECISION = "submit_decision"

# get_customer and get_invoices take no arguments on purpose: they always answer for the
# ticket's own customer, so text in a ticket cannot point the agent at someone else's data.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_customer",
        description="Get the plan and status of the customer who sent this ticket.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="get_invoices",
        description="List the invoices of the customer who sent this ticket.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="search_kb",
        description="Find knowledge base articles that match a question.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=SUBMIT_DECISION,
        description=(
            "Submit the final decision for the ticket. Call this exactly once, when done. "
            "reply needs message (and article_ids it uses); refund needs invoice_id and "
            "amount_eur; escalate and close need reason."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [c.value for c in Category]},
                "priority": {"type": "string", "enum": [p.value for p in Priority]},
                "action": {"type": "string", "enum": [a.value for a in ActionType]},
                "message": {"type": "string"},
                "article_ids": {"type": "array", "items": {"type": "string"}},
                "invoice_id": {"type": "string"},
                "amount_eur": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["category", "priority", "action"],
            "additionalProperties": False,
        },
    ),
]


class ToolExecutor:
    """Runs lookup tools for one ticket. Results are JSON text for the model."""

    def __init__(self, store: DataStore, ticket: Ticket) -> None:
        self._store = store
        self._customer_id = ticket.customer_id

    def run(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return _error(f"arguments for {name} are not valid JSON")
        if not isinstance(args, dict):
            return _error(f"arguments for {name} must be a JSON object")

        match name:
            case "get_customer":
                customer = self._store.get_customer(self._customer_id)
                if customer is None:
                    return _error("customer not found")
                return customer.model_dump_json()
            case "get_invoices":
                invoices = self._store.get_invoices(self._customer_id)
                return json.dumps([inv.model_dump(mode="json") for inv in invoices])
            case "search_kb":
                query = args.get("query")
                if not isinstance(query, str) or not query.strip():
                    return _error("search_kb needs a non-empty 'query' string")
                articles = self._store.search_kb(query)
                return json.dumps([a.model_dump(mode="json") for a in articles])
            case _:
                return _error(f"unknown tool {name!r}")


def format_validation_error(exc: ValidationError) -> str:
    return _error(f"decision is invalid: {validation_problems(exc)}. Call {SUBMIT_DECISION} again.")


def validation_problems(exc: ValidationError) -> str:
    """Which fields are wrong and why, without the values: unlike `str(exc)`, nothing the model wrote is repeated."""
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False))


def _error(message: str) -> str:
    return json.dumps({"error": message})
