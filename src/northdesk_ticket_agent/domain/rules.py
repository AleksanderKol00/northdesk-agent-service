"""Checks applied to every proposed decision before anything is done.

Pure functions: no I/O and no dependency on the API, the workflow engine or the LLM
provider. The facts they rely on (the ticket's customer, that customer's invoices, the
known articles) come from our own data, never from the model or the ticket text.
"""

import math
import re
from collections.abc import Collection, Iterable
from decimal import Decimal

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

# Words that make a ticket about money. A close of such a ticket goes to a person, whatever category the model chose.
_MONEY_WORDS = re.compile(
    r"\b(?:refund\w*|charge[ds]?|invoices?|pay|paid|payments?|billing|billed|money|eur|euros?)\b|€",
    re.IGNORECASE,
)


def check_decision(
    proposed: ProposedDecision,
    ticket: Ticket,
    invoices: Iterable[Invoice],
    kb_article_ids: Collection[str],
) -> Decision:
    """Validate a proposed decision; any broken rule turns it into an escalation that lists every broken rule."""
    violations: list[str] = []
    category = _parse_enum(Category, proposed.category, "category", violations)
    priority = _parse_enum(Priority, proposed.priority, "priority", violations)
    action_type = _parse_enum(ActionType, proposed.action, "action", violations)

    # Each check returns the action, or the rules it breaks.
    action: Action | list[str] = []
    match action_type:
        case ActionType.REPLY:
            action = _check_reply(proposed, kb_article_ids)
        case ActionType.REFUND:
            action = _check_refund(proposed, ticket.customer_id, invoices)
        case ActionType.ESCALATE:
            action = _check_escalate(proposed)
        case ActionType.CLOSE:
            action = _check_close(proposed, ticket, category)
    if isinstance(action, list):
        violations += action

    if violations or isinstance(action, list) or category is None or priority is None:
        return Decision(
            category=category or Category.OTHER,
            priority=priority or Priority.NORMAL,
            action=EscalateAction(reason="Decision failed checks: " + "; ".join(violations)),
            violations=violations,
        )
    return Decision(category=category, priority=priority, action=action)


def _check_reply(proposed: ProposedDecision, kb_article_ids: Collection[str]) -> ReplyAction | list[str]:
    violations = []
    message = _non_blank(proposed.message)
    if message is None:
        violations.append("reply needs a message")
    unknown = [a for a in proposed.article_ids if a not in kb_article_ids]
    if unknown:
        violations.append(f"reply cites unknown articles: {', '.join(unknown)}")
    if violations or message is None:
        return violations
    return ReplyAction(message=message, article_ids=list(proposed.article_ids))


def _check_refund(
    proposed: ProposedDecision, customer_id: str, invoices: Iterable[Invoice]
) -> RefundAction | list[str]:
    violations = []
    amount = _to_money(proposed.amount_eur)
    if amount is None:
        violations.append("refund needs an amount greater than 0, in whole cents")
    invoice_id = proposed.invoice_id
    if not invoice_id:
        violations.append("refund needs an invoice")
    else:
        # Ownership is checked against the ticket's customer, never a customer the model names.
        invoice = next((i for i in invoices if i.invoice_id == invoice_id and i.customer_id == customer_id), None)
        if invoice is None:
            violations.append(f"invoice {invoice_id} does not belong to customer {customer_id}")
        else:
            if invoice.status != "paid":
                violations.append(f"invoice {invoice_id} is {invoice.status}, only paid invoices")
            if amount is not None and amount > invoice.amount_eur:
                violations.append(f"refund {amount} EUR is larger than invoice {invoice_id} ({invoice.amount_eur} EUR)")
    if violations or not invoice_id or amount is None:
        return violations
    return RefundAction(invoice_id=invoice_id, amount_eur=amount)


def _check_escalate(proposed: ProposedDecision) -> EscalateAction | list[str]:
    reason = _non_blank(proposed.reason)
    return EscalateAction(reason=reason) if reason else ["escalate needs a reason"]


def _check_close(proposed: ProposedDecision, ticket: Ticket, category: Category | None) -> CloseAction | list[str]:
    violations = []
    reason = _non_blank(proposed.reason)
    if reason is None:
        violations.append("close needs a reason")
    # Closing ends a ticket unseen, so it is only for tickets not about our product (spam, off-topic). The category is
    # the model's own, so this catches an inconsistent decision. The money words are the ticket's, so a refund demand
    # the model was talked into calling spam still reaches a person.
    if category is not None and category != Category.OTHER:
        violations.append(f"close is only for category {Category.OTHER}, not {category}")
    if _MONEY_WORDS.search(f"{ticket.subject}\n{ticket.body}"):
        violations.append("close is not allowed for a ticket about money")
    if violations or reason is None:
        return violations
    return CloseAction(reason=reason)


def _to_money(value: float | None) -> Decimal | None:
    """The amount in exact euros, or None unless it is finite, greater than 0 and in whole cents."""
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    amount = Decimal(str(value))  # str: the shortest decimal for the float (49.99), not its binary expansion
    # The exponent, not quantize(): quantizing 1e26 to the cent needs more digits than the context has, and raises.
    exponent = amount.as_tuple().exponent
    return amount if isinstance(exponent, int) and exponent >= -2 else None


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
