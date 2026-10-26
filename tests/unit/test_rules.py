from typing import Any

import pytest

from northdesk_ticket_agent.domain.models import (
    ActionType,
    Category,
    Decision,
    Invoice,
    Priority,
    ProposedDecision,
    Ticket,
)
from northdesk_ticket_agent.domain.rules import check_decision

TICKET = Ticket(ticket_id="T-X", customer_id="C-100", subject="s", body="b")
INVOICES = [
    Invoice(invoice_id="INV-1", customer_id="C-100", month="2026-09", amount_eur=49.0, status="paid"),
    Invoice(invoice_id="INV-9", customer_id="C-900", month="2026-09", amount_eur=990.0, status="paid"),
    Invoice(invoice_id="INV-4", customer_id="C-100", month="2026-08", amount_eur=49.0, status="overdue"),
]
KB_IDS = {"KB-1", "KB-2"}


def check(**fields: Any) -> Decision:
    proposed = ProposedDecision(**{"category": "billing", "priority": "normal", **fields})
    return check_decision(proposed, TICKET, INVOICES, KB_IDS)


@pytest.mark.parametrize(
    ("fields", "expected_type"),
    [
        ({"action": "reply", "message": "Hi", "article_ids": ["KB-1"]}, ActionType.REPLY),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": 49.0}, ActionType.REFUND),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": 10.0}, ActionType.REFUND),
        ({"action": "escalate", "reason": "needs a human"}, ActionType.ESCALATE),
    ],
)
def test_valid_decisions_pass_unchanged(fields: dict[str, Any], expected_type: ActionType) -> None:
    decision = check(**fields)
    assert decision.violations == []
    assert decision.action.type == expected_type
    assert decision.category == Category.BILLING
    assert decision.priority == Priority.NORMAL


def test_off_topic_ticket_can_be_closed() -> None:
    decision = check(category="other", priority="low", action="close", reason="spam")
    assert decision.violations == []
    assert decision.action.type == ActionType.CLOSE
    assert decision.category == Category.OTHER


@pytest.mark.parametrize(
    ("fields", "violation"),
    [
        ({"category": "sales", "action": "close", "reason": "x"}, "category 'sales'"),
        ({"priority": "critical", "action": "close", "reason": "x"}, "priority 'critical'"),
        ({"action": "delete_account"}, "action 'delete_account'"),
        ({"action": "reply"}, "reply needs a message"),
        ({"action": "reply", "message": "   "}, "reply needs a message"),
        ({"action": "reply", "message": "Hi", "article_ids": ["KB-99"]}, "unknown articles: KB-99"),
        ({"action": "refund", "amount_eur": 10.0}, "refund needs an invoice"),
        ({"action": "refund", "invoice_id": "INV-1"}, "amount greater than 0"),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": -5}, "amount greater than 0"),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": "NaN"}, "amount greater than 0"),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": "inf"}, "amount greater than 0"),
        ({"action": "refund", "invoice_id": "INV-4", "amount_eur": 10.0}, "is overdue"),
        ({"action": "refund", "invoice_id": "INV-9", "amount_eur": 10.0}, "does not belong"),
        ({"action": "refund", "invoice_id": "INV-404", "amount_eur": 10.0}, "does not belong"),
        ({"action": "refund", "invoice_id": "INV-1", "amount_eur": 5000.0}, "larger than invoice"),
        ({"action": "escalate"}, "escalate needs a reason"),
        ({"category": "other", "action": "close", "reason": ""}, "close needs a reason"),
        ({"category": "account", "action": "close", "reason": "resolved"}, "close is only for category other"),
        ({"category": "billing", "action": "close", "reason": "resolved"}, "close is only for category other"),
        ({"category": "technical", "action": "close", "reason": "resolved"}, "close is only for category other"),
    ],
)
def test_broken_rule_becomes_escalation(fields: dict[str, Any], violation: str) -> None:
    decision = check(**fields)
    assert decision.action.type == ActionType.ESCALATE
    assert any(violation in v for v in decision.violations), decision.violations
    assert violation in decision.action.reason


def test_escalation_keeps_valid_category_and_priority() -> None:
    decision = check(priority="urgent", action="refund", invoice_id="INV-1", amount_eur=500.0)
    assert decision.category == Category.BILLING
    assert decision.priority == Priority.URGENT


def test_escalation_falls_back_when_category_and_priority_are_invalid() -> None:
    decision = check(category="??", priority="??", action="close", reason="x")
    assert decision.category == Category.OTHER
    assert decision.priority == Priority.NORMAL
    assert len(decision.violations) == 2


def test_nan_refund_from_model_json_is_rejected() -> None:
    proposed = ProposedDecision.model_validate_json(
        '{"category": "billing", "priority": "normal", "action": "refund", "invoice_id": "INV-1", "amount_eur": NaN}'
    )
    decision = check_decision(proposed, TICKET, INVOICES, KB_IDS)
    assert decision.action.type == ActionType.ESCALATE
