import pytest
from pydantic import ValidationError

from northdesk_ticket_agent.domain.models import Invoice, Ticket


def ticket(ticket_id: str) -> Ticket:
    return Ticket(ticket_id=ticket_id, customer_id="C-100", subject="Hi", body="Hello")


@pytest.mark.parametrize("ticket_id", ["T-1", "t_2.retry", "1", "A" * 64])
def test_ticket_id_accepts_url_safe_ids(ticket_id: str) -> None:
    assert ticket(ticket_id).ticket_id == ticket_id


@pytest.mark.parametrize("ticket_id", ["", "A/B", "T 1", "T:1", "?", ".", "..", ".T-1", "-T-1", "A" * 65])
def test_ticket_id_rejects_ids_that_are_not_url_safe(ticket_id: str) -> None:
    with pytest.raises(ValidationError):
        ticket(ticket_id)


@pytest.mark.parametrize(("subject", "body"), [("S" * 201, "Hello"), ("Hi", "B" * 10_001)])
def test_ticket_text_is_capped(subject: str, body: str) -> None:
    with pytest.raises(ValidationError):
        Ticket(ticket_id="T-1", customer_id="C-100", subject=subject, body=body)


def test_invoice_rejects_an_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Invoice.model_validate(
            {"invoice_id": "INV-1", "customer_id": "C-100", "month": "2026-09", "amount_eur": 1, "status": "refunded"}
        )
