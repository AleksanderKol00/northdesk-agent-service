from fastapi.testclient import TestClient

from northdesk_ticket_agent.domain.models import (
    Category,
    Decision,
    Priority,
    RefundAction,
    Ticket,
)
from northdesk_ticket_agent.orchestration.workflow import Approval, Step
from tests.fixtures.client import StubTicketWorkflows
from tests.fixtures.logs import LogLines

REFUND = RefundAction(invoice_id="INV-1002", amount_eur=49.0)


def await_refund_approval(workflows: StubTicketWorkflows, ticket_id: str) -> None:
    state = workflows.states[ticket_id]
    state.step = Step.AWAITING_APPROVAL
    state.decision = Decision(category=Category.BILLING, priority=Priority.NORMAL, action=REFUND)
    state.action = REFUND


def test_post_ticket_starts_its_workflow(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    ticket = example_tickets["T-1"]

    response = client.post("/tickets", json=ticket.model_dump())

    assert response.status_code == 202
    assert response.json() == {"ticket_id": "T-1", "workflow_id": "T-1", "created": True}
    assert workflows.started == [ticket]


def test_the_same_ticket_sent_twice_returns_the_existing_workflow(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    ticket = example_tickets["T-1"].model_dump()
    client.post("/tickets", json=ticket)

    response = client.post("/tickets", json=ticket)

    assert response.status_code == 200
    assert response.json() == {"ticket_id": "T-1", "workflow_id": "T-1", "created": False}
    assert len(workflows.started) == 1


def test_post_ticket_rejects_an_incomplete_ticket(client: TestClient, workflows: StubTicketWorkflows) -> None:
    response = client.post("/tickets", json={"ticket_id": "T-9", "subject": "Hi"})

    assert response.status_code == 422
    assert workflows.started == []


def test_post_ticket_rejects_an_id_that_is_not_url_safe(client: TestClient, workflows: StubTicketWorkflows) -> None:
    response = client.post("/tickets", json={"ticket_id": "A/B", "customer_id": "C-100", "subject": "s", "body": "b"})

    assert response.status_code == 422
    assert workflows.started == []


def test_get_ticket_returns_its_state(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    client.post("/tickets", json=example_tickets["T-2"].model_dump())
    await_refund_approval(workflows, "T-2")

    response = client.get("/tickets/T-2")

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == body["workflow_id"] == "T-2"
    assert body["step"] == "awaiting_approval"
    assert body["decision"]["action"] == {"type": "refund", "invoice_id": "INV-1002", "amount_eur": 49.0}
    assert body["receipt"] is None


def test_get_unknown_ticket_is_404(client: TestClient) -> None:
    response = client.get("/tickets/T-404")

    assert response.status_code == 404
    assert response.json() == {"detail": "ticket T-404 not found"}


def test_approval_answers_a_waiting_refund(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    client.post("/tickets", json=example_tickets["T-2"].model_dump())
    await_refund_approval(workflows, "T-2")

    response = client.post("/tickets/T-2/approval", json={"approved": False, "comment": "Not a double charge"})

    assert response.status_code == 200
    assert response.json()["approval"] == {"approved": False, "comment": "Not a double charge"}
    assert workflows.states["T-2"].approval == Approval(approved=False, comment="Not a double charge")


def test_approval_when_nothing_is_waiting_is_409(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    client.post("/tickets", json=example_tickets["T-1"].model_dump())

    response = client.post("/tickets/T-1/approval", json={"approved": True})

    assert response.status_code == 409
    assert response.json() == {"detail": "ticket T-1 has no refund waiting for approval"}


def test_a_second_approval_says_the_refund_was_already_answered(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    client.post("/tickets", json=example_tickets["T-2"].model_dump())
    await_refund_approval(workflows, "T-2")
    client.post("/tickets/T-2/approval", json={"approved": True})

    response = client.post("/tickets/T-2/approval", json={"approved": False})

    assert response.status_code == 409
    assert response.json() == {"detail": "the refund for ticket T-2 was already approved"}
    assert workflows.states["T-2"].approval == Approval(approved=True)


def test_approval_for_unknown_ticket_is_404(client: TestClient) -> None:
    response = client.post("/tickets/T-404/approval", json={"approved": True})

    assert response.status_code == 404


def test_approval_needs_an_answer(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    client.post("/tickets", json=example_tickets["T-2"].model_dump())
    await_refund_approval(workflows, "T-2")

    response = client.post("/tickets/T-2/approval", json={"comment": "yes?"})

    assert response.status_code == 422
    assert workflows.states["T-2"].approval is None


def test_unavailable_temporal_is_503(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket]
) -> None:
    workflows.down = True

    responses = [
        client.post("/tickets", json=example_tickets["T-1"].model_dump()),
        client.get("/tickets/T-1"),
        client.post("/tickets/T-1/approval", json={"approved": True}),
    ]

    assert [response.status_code for response in responses] == [503, 503, 503]
    assert responses[0].json() == {"detail": "tickets cannot be processed right now; try again later"}


def test_every_request_about_a_ticket_logs_its_ids(
    client: TestClient, workflows: StubTicketWorkflows, example_tickets: dict[str, Ticket], json_logs: LogLines
) -> None:
    client.post("/tickets", json=example_tickets["T-2"].model_dump())
    await_refund_approval(workflows, "T-2")
    client.post("/tickets/T-2/approval", json={"approved": True})
    client.get("/tickets/T-2")

    lines = [line for line in json_logs.all() if line["logger"].startswith("northdesk_ticket_agent.")]  # no httpx
    assert lines, "nothing was logged"
    assert all(line.get("ticket_id") == "T-2" and line.get("workflow_id") == "T-2" for line in lines)
    access = json_logs.from_logger("northdesk_ticket_agent.api.access_log")
    assert [(line["method"], line["path"], line["status_code"]) for line in access] == [
        ("POST", "/tickets", 202),
        ("POST", "/tickets/T-2/approval", 200),
        ("GET", "/tickets/T-2", 200),
    ]
    assert [line["message"] for line in json_logs.from_logger("northdesk_ticket_agent.api.tickets")] == [
        "Ticket accepted; workflow started",
        "Refund approved",
    ]


def test_a_repeated_ticket_is_logged_as_such(
    client: TestClient, example_tickets: dict[str, Ticket], json_logs: LogLines
) -> None:
    client.post("/tickets", json=example_tickets["T-1"].model_dump())
    client.post("/tickets", json=example_tickets["T-1"].model_dump())

    messages = [line["message"] for line in json_logs.from_logger("northdesk_ticket_agent.api.tickets")]
    assert messages == ["Ticket accepted; workflow started", "Ticket was sent before; returning its workflow"]


def test_an_error_answer_is_logged_with_the_ticket(client: TestClient, json_logs: LogLines) -> None:
    client.get("/tickets/T-404")

    [error] = json_logs.from_logger("northdesk_ticket_agent.api.errors")
    assert error["level"] == "WARNING"
    assert error["error"] == "TicketNotFoundError"
    assert error["ticket_id"] == "T-404"
    assert "exception" not in error  # a 4xx is the client's problem: no traceback


def test_unavailable_temporal_is_logged_with_its_cause(
    client: TestClient, workflows: StubTicketWorkflows, json_logs: LogLines
) -> None:
    workflows.down = True

    client.get("/tickets/T-1")

    [error] = json_logs.from_logger("northdesk_ticket_agent.api.errors")
    assert error["error"] == "TicketWorkflowsUnavailableError"
    assert "Traceback" in error["exception"]


def test_health_checks_stay_out_of_the_info_log(client: TestClient, json_logs: LogLines) -> None:
    client.get("/health")

    assert json_logs.from_logger("northdesk_ticket_agent.api.access_log") == []
