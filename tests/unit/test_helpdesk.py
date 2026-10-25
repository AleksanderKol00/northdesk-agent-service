import pytest
from pydantic import ValidationError

from northdesk_ticket_agent.domain.models import Action, CloseAction, EscalateAction, RefundAction, ReplyAction
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk, HelpdeskConflictError

REFUND = RefundAction(invoice_id="INV-1002", amount_eur=49.0)


@pytest.mark.parametrize(
    ("action", "prefix"),
    [
        (ReplyAction(message="Hi", article_ids=["KB-1"]), "RE-"),
        (REFUND, "RF-"),
        (EscalateAction(reason="outage"), "ES-"),
        (CloseAction(reason="spam"), "CL-"),
    ],
)
async def test_performs_each_action(helpdesk: FakeHelpdesk, action: Action, prefix: str) -> None:
    receipt = await helpdesk.perform("T-1", action, idempotency_key="T-1:act")

    assert receipt.reference.startswith(prefix)
    assert receipt.ticket_id == "T-1"
    assert receipt.action == action
    assert helpdesk.performed == [receipt]


async def test_repeated_call_with_the_same_key_refunds_once(helpdesk: FakeHelpdesk) -> None:
    first = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
    second = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")

    assert second == first
    assert len(helpdesk.performed) == 1


async def test_different_keys_are_different_requests(helpdesk: FakeHelpdesk) -> None:
    first = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
    second = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act-again")

    assert first.reference != second.reference
    assert len(helpdesk.performed) == 2


@pytest.mark.parametrize(
    ("ticket_id", "action"),
    [
        ("T-2", RefundAction(invoice_id="INV-1002", amount_eur=1.0)),
        ("T-9", REFUND),
    ],
)
async def test_reusing_a_key_for_another_request_is_a_conflict(
    helpdesk: FakeHelpdesk, ticket_id: str, action: Action
) -> None:
    await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")

    with pytest.raises(HelpdeskConflictError):
        await helpdesk.perform(ticket_id, action, idempotency_key="T-2:act")
    assert len(helpdesk.performed) == 1


async def test_changing_a_returned_receipt_or_the_sent_action_does_not_change_the_record(
    helpdesk: FakeHelpdesk,
) -> None:
    action = RefundAction(invoice_id="INV-1002", amount_eur=49.0)
    receipt = await helpdesk.perform("T-2", action, idempotency_key="T-2:act")

    action.amount_eur = 5000.0
    assert isinstance(receipt.action, RefundAction)
    receipt.action.amount_eur = 5000.0
    with pytest.raises(ValidationError):
        receipt.reference = "RF-9999"  # type: ignore[misc]

    again = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
    assert again.action == REFUND
    assert helpdesk.performed[0].action == REFUND
