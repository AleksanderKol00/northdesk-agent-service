"""Checks applied to every proposed decision before anything is done.

Pure functions: no I/O and no dependency on the API, the workflow engine or the LLM
provider. The facts they rely on (the ticket's customer, that customer's invoices, the
known articles) come from our own data, never from the model or the ticket text.
"""

import math
from collections.abc import Collection, Iterable

from northdesk_ticket_agent.domain.models import (
    Action,
    ActionType,
    Category,
    CloseAction,
    Decision,
    EscalateAction,
    Invoice,
    Priority,
    ProposedDecision,
    RefundAction,
    ReplyAction,
    Ticket,
)


def check_decision(
    proposed: ProposedDecision,
    ticket: Ticket,
    invoices: Iterable[Invoice],
    kb_article_ids: Collection[str],
) -> Decision:
    """Validate a proposed decision; any broken rule turns it into an escalation."""
    violations: list[str] = []

    category = _parse_enum(Category, proposed.category, "category", violations)
    priority = _parse_enum(Priority, proposed.priority, "priority", violations)
    action_type = _parse_enum(ActionType, proposed.action, "action", violations)

    action: Action | None = None
    match action_type:
        case ActionType.REPLY:
            action = _check_reply(proposed, kb_article_ids, violations)
        case ActionType.REFUND:
            action = _check_refund(proposed, ticket, invoices, violations)
        case ActionType.ESCALATE:
            if reason := _non_blank(proposed.reason):
                action = EscalateAction(reason=reason)
            else:
                violations.append("escalate needs a reason")
        case ActionType.CLOSE:
            if reason := _non_blank(proposed.reason):
                action = CloseAction(reason=reason)
            else:
                violations.append("close needs a reason")

    if violations or action is None or category is None or priority is None:
        return Decision(
            category=category or Category.OTHER,
            priority=priority or Priority.NORMAL,
            action=EscalateAction(reason="Decision failed checks: " + "; ".join(violations)),
            violations=violations,
        )
    return Decision(category=category, priority=priority, action=action)


def _check_reply(
    proposed: ProposedDecision,
    kb_article_ids: Collection[str],
    violations: list[str],
) -> ReplyAction | None:
    message = _non_blank(proposed.message)
    if not message:
        violations.append("reply needs a message")
    unknown = [a for a in proposed.article_ids if a not in kb_article_ids]
    if unknown:
        violations.append(f"reply cites unknown articles: {', '.join(unknown)}")
    if violations or not message:
        return None
    return ReplyAction(message=message, article_ids=list(proposed.article_ids))


def _check_refund(
    proposed: ProposedDecision,
    ticket: Ticket,
    invoices: Iterable[Invoice],
    violations: list[str],
) -> RefundAction | None:
    invoice_id = proposed.invoice_id
    if not invoice_id:
        violations.append("refund needs an invoice")
    amount = proposed.amount_eur
    if amount is None or not math.isfinite(amount) or amount <= 0:
        violations.append("refund needs an amount greater than 0")
    if violations or not invoice_id or amount is None:
        return None

    # Ownership is checked against the ticket's customer, never a customer the model names.
    invoice = next(
        (inv for inv in invoices if inv.invoice_id == invoice_id and inv.customer_id == ticket.customer_id),
        None,
    )
    if invoice is None:
        violations.append(f"invoice {invoice_id} does not belong to customer {ticket.customer_id}")
        return None
    if invoice.status != "paid":
        violations.append(f"invoice {invoice.invoice_id} is {invoice.status}, only paid invoices")
        return None
    if amount > invoice.amount_eur:
        violations.append(
            f"refund {amount:.2f} EUR is larger than invoice {invoice.invoice_id} ({invoice.amount_eur:.2f} EUR)"
        )
        return None
    return RefundAction(invoice_id=invoice.invoice_id, amount_eur=amount)


def _parse_enum[E: (Category, Priority, ActionType)](
    enum: type[E], value: str, field: str, violations: list[str]
) -> E | None:
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum)
        violations.append(f"{field} {value!r} is not allowed (allowed: {allowed})")
        return None


def _non_blank(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None
