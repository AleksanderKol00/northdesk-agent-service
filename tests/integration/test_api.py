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
