import json

import pytest

from northdesk_ticket_agent.agent.tools import ToolExecutor
from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.services.datastore import DataStore


def run(datastore: DataStore, ticket: Ticket, name: str, arguments: str) -> object:
    return json.loads(ToolExecutor(datastore, ticket).run(name, arguments))


@pytest.mark.parametrize(
    ("name", "arguments", "error"),
    [
        ("search_kb", "{not json", "not valid JSON"),
        ("search_kb", '["password"]', "must be a JSON object"),
        ("search_kb", "{}", "non-empty 'query'"),
        ("search_kb", '{"query": "   "}', "non-empty 'query'"),
        ("delete_customer", "{}", "unknown tool 'delete_customer'"),
    ],
)
def test_bad_tool_calls_get_an_error_the_model_can_read(
    datastore: DataStore, example_tickets: dict[str, Ticket], name: str, arguments: str, error: str
) -> None:
    result = run(datastore, example_tickets["T-1"], name, arguments)

    assert isinstance(result, dict)
    assert error in result["error"]


def test_get_customer_reports_an_unknown_customer(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    assert run(datastore, example_tickets["T-7"], "get_customer", "") == {"error": "customer not found"}
