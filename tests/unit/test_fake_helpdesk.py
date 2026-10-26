import random

import pytest
from pydantic import ValidationError

from northdesk_ticket_agent.domain.models import Action, CloseAction, EscalateAction, RefundAction, ReplyAction
from northdesk_ticket_agent.services.fake_helpdesk import FakeHelpdesk
from northdesk_ticket_agent.services.helpdesk import HelpdeskConflictError, HelpdeskTemporaryError

REFUND = RefundAction(invoice_id="INV-1002", amount_eur=49.0)


class ScriptedRandom(random.Random):
    """Returns the given rolls in order: the first decides failure, the second whether the response is lost."""

    def __init__(self, *rolls: float) -> None:
        super().__init__()
        self._rolls = list(rolls)

    def random(self) -> float:
        return self._rolls.pop(0)


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


async def test_failure_before_the_action_does_nothing() -> None:
    helpdesk = FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.1, 0.9))

    with pytest.raises(HelpdeskTemporaryError, match="unavailable"):
        await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
    assert helpdesk.performed == []


async def test_retry_after_a_lost_response_does_not_refund_twice() -> None:
    # First call: fails (0.1 < 0.5) after the refund was made (0.1 < 0.5). Retry: succeeds (0.9).
    helpdesk = FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.1, 0.1, 0.9))

    with pytest.raises(HelpdeskTemporaryError, match="lost"):
        await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
    receipt = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")

    assert helpdesk.performed == [receipt]


async def test_always_fails_at_rate_one() -> None:
    helpdesk = FakeHelpdesk(failure_rate=1.0, rng=random.Random(7))  # noqa: S311
    for _ in range(20):
        with pytest.raises(HelpdeskTemporaryError):
            await helpdesk.perform("T-1", CloseAction(reason="spam"), idempotency_key="T-1:act")


async def test_retries_eventually_succeed_and_act_once() -> None:
    helpdesk = FakeHelpdesk(failure_rate=0.5, rng=random.Random(42))  # noqa: S311
    receipt = None
    for _ in range(50):
        try:
            receipt = await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")
            break
        except HelpdeskTemporaryError:
            continue

    assert receipt is not None
    assert helpdesk.performed == [receipt]


@pytest.mark.parametrize("rate", [-0.1, 1.5])
def test_rejects_a_failure_rate_outside_zero_to_one(rate: float) -> None:
    with pytest.raises(ValueError, match="failure_rate"):
        FakeHelpdesk(failure_rate=rate)


async def test_conflict_is_reported_even_when_the_call_would_fail() -> None:
    # First call succeeds (0.9). The second roll (0.1) would fail it, but the conflict is found before any roll.
    helpdesk = FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.9, 0.1, 0.9))
    await helpdesk.perform("T-2", REFUND, idempotency_key="T-2:act")

    with pytest.raises(HelpdeskConflictError):
        await helpdesk.perform("T-2", RefundAction(invoice_id="INV-1002", amount_eur=1.0), idempotency_key="T-2:act")


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
