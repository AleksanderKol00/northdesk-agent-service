from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Ticket(BaseModel):
    ticket_id: str
    customer_id: str
    subject: str
    body: str


class Customer(BaseModel):
    customer_id: str
    name: str
    plan: str
    status: str


class Invoice(BaseModel):
    invoice_id: str
    customer_id: str
    month: str
    amount_eur: float
    status: str


class KbArticle(BaseModel):
    article_id: str
    title: str
    text: str


class Category(StrEnum):
    ACCOUNT = "account"
    BILLING = "billing"
    TECHNICAL = "technical"
    OTHER = "other"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ActionType(StrEnum):
    REPLY = "reply"
    REFUND = "refund"
    ESCALATE = "escalate"
    CLOSE = "close"


class ProposedDecision(BaseModel):
    """What the model proposed, before any checks.

    Fields are deliberately loose (plain strings, everything optional except the three
    core fields) so that a wrong value is caught and reported by the rules instead of
    being lost in a parse error.
    """

    category: str
    priority: str
    action: str
    message: str | None = None
    article_ids: list[str] = Field(default_factory=list)
    invoice_id: str | None = None
    amount_eur: float | None = None
    reason: str | None = None


class ReplyAction(BaseModel):
    type: Literal[ActionType.REPLY] = ActionType.REPLY
    message: str
    article_ids: list[str] = Field(default_factory=list)


class RefundAction(BaseModel):
    type: Literal[ActionType.REFUND] = ActionType.REFUND
    invoice_id: str
    amount_eur: float


class EscalateAction(BaseModel):
    type: Literal[ActionType.ESCALATE] = ActionType.ESCALATE
    reason: str


class CloseAction(BaseModel):
    type: Literal[ActionType.CLOSE] = ActionType.CLOSE
    reason: str


Action = Annotated[
    ReplyAction | RefundAction | EscalateAction | CloseAction,
    Field(discriminator="type"),
]


class Decision(BaseModel):
    """A decision that passed the checks, or the escalation that replaced it."""

    category: Category
    priority: Priority
    action: Action
    violations: list[str] = Field(default_factory=list)
