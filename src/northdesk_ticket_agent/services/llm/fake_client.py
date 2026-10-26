"""A rule-based stand-in for a real LLM: no network, no API key, deterministic.

It speaks the same protocol as the real client. On the first turn it asks for tool
calls; once the tool results are in the conversation it calls submit_decision.

It is deliberately naive and does what the ticket asks (e.g. it proposes any refund
amount a customer names), so the checks in code are what keep it in line.
"""

import json
import re
from typing import Any

from northdesk_ticket_agent.services.llm.base import LLMResponse, Message, ToolCall, ToolSpec

_TICKET = re.compile(r"<ticket>\s*(.*?)\s*</ticket>", re.DOTALL)
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:eur|euro|€)", re.IGNORECASE)

# Matched as whole words or phrases, so e.g. "download" does not count as "down".
_REFUND_WORDS = ("refund", "refunds", "refunded")
_BILLING_WORDS = (
    *_REFUND_WORDS,
    *("charge", "charged", "charges", "invoice", "invoices", "bill", "billed", "billing"),
    *("payment", "payments", "paid"),
)
_DOUBLE_CHARGE_WORDS = ("twice", "double", "duplicate")
_OUTAGE_WORDS = ("down", "outage", "outages", "error", "errors", "not working", "unavailable")
_LOGIN_WORDS = ("password", "log in", "login", "sign in", "locked out")
_URGENT_WORDS = ("urgent", "right now", "asap", "immediately")


class FakeLLMClient:
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        ticket = _find_ticket(messages)
        text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".strip()
        results = _tool_results(messages)
        next_id = sum(1 for m in messages if m.role == "tool")

        if not results:
            calls = [
                ("get_customer", {}),
                ("search_kb", {"query": text}),
            ]
            if _has_any(text, _BILLING_WORDS):
                calls.append(("get_invoices", {}))
            return LLMResponse(
                tool_calls=[
                    ToolCall(id=f"call_{next_id + i}", name=name, arguments=json.dumps(args))
                    for i, (name, args) in enumerate(calls)
                ]
            )

        decision = _decide(text, results)
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id=f"call_{next_id}",
                    name="submit_decision",
                    arguments=json.dumps(decision),
                )
            ]
        )


def _decide(text: str, results: dict[str, Any]) -> dict[str, Any]:
    # A failed tool returns {"error": ...}; treat that the same as "nothing found".
    customer = _as_dict(results.get("get_customer"))
    invoices = _as_list(results.get("get_invoices"))
    articles = _as_list(results.get("search_kb"))
    urgent = _has_any(text, _URGENT_WORDS)
    double_charge = _has_any(text, _DOUBLE_CHARGE_WORDS)

    if double_charge or _has_any(text, _REFUND_WORDS):
        if not invoices:
            return _escalate("billing", "normal", "Refund requested but no invoices found.")
        invoice = (_duplicate_invoice(invoices) if double_charge else None) or invoices[0]
        amount = _mentioned_amount(text)
        if amount is None:
            amount = float(invoice["amount_eur"])  # sent as a string, to keep it exact
        return {
            "category": "billing",
            "priority": "high" if urgent else "normal",
            "action": "refund",
            "invoice_id": invoice["invoice_id"],
            "amount_eur": amount,
        }

    if _has_any(text, _OUTAGE_WORDS):
        enterprise = customer.get("plan") == "enterprise"
        return _escalate(
            "technical",
            "urgent" if urgent or enterprise else "high",
            f"Possible outage reported (customer plan: {customer.get('plan', 'unknown')}).",
        )

    if customer.get("status") == "suspended":
        article = {"article_id": "KB-4"}
        return _reply("account", "high", article, articles)

    if _has_any(text, _LOGIN_WORDS) and articles:
        return _reply("account", "normal", articles[0], articles)

    if articles:
        return _reply("other", "normal", articles[0], articles)

    return {
        "category": "other",
        "priority": "low",
        "action": "close",
        "reason": "Not a support request about Northdesk.",
    }


def _reply(category: str, priority: str, article: dict[str, Any], found: list[dict[str, Any]]) -> dict[str, Any]:
    full = next((a for a in found if a["article_id"] == article["article_id"]), article)
    body = full.get("text", "Please see the linked article.")
    return {
        "category": category,
        "priority": priority,
        "action": "reply",
        "message": f"Hello, thanks for reaching out. {body}",
        "article_ids": [full["article_id"]],
    }


def _escalate(category: str, priority: str, reason: str) -> dict[str, Any]:
    return {"category": category, "priority": priority, "action": "escalate", "reason": reason}


def _duplicate_invoice(invoices: list[dict[str, Any]]) -> dict[str, Any] | None:
    seen: set[tuple[Any, Any]] = set()
    for invoice in invoices:
        key = (invoice["month"], invoice["amount_eur"])
        if key in seen:
            return invoice
        seen.add(key)
    return None


def _mentioned_amount(text: str) -> float | None:
    match = _AMOUNT.search(text)
    return float(match.group(1).replace(",", ".")) if match else None


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) and "error" not in value else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _find_ticket(messages: list[Message]) -> dict[str, Any]:
    for message in messages:
        if message.role == "user" and message.content and (match := _TICKET.search(message.content)):
            try:
                ticket = json.loads(match.group(1))
            except json.JSONDecodeError:
                return {}
            return ticket if isinstance(ticket, dict) else {}
    return {}


def _tool_results(messages: list[Message]) -> dict[str, Any]:
    """Map tool name -> parsed result, using the assistant messages to resolve call ids."""
    names = {call.id: call.name for m in messages for call in m.tool_calls}
    results: dict[str, Any] = {}
    for message in messages:
        if message.role == "tool" and message.tool_call_id in names:
            try:
                results[names[message.tool_call_id]] = json.loads(message.content or "null")
            except json.JSONDecodeError:
                continue
    return results
