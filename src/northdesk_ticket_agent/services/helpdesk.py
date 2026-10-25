"""A stand-in for the Helpdesk API that carries out the final action on a ticket.

It fails at random with a temporary error, at a configurable rate. Half of those failures happen after the action was
done (the response is lost), which is exactly the case a retry must not repeat.

Every call takes an idempotency key. Repeating a call with the same key returns the first result instead of doing the
action again, so a retried refund is issued once. State lives in memory, so the guarantee holds within one process.
"""

import random

from pydantic import BaseModel, ConfigDict

from northdesk_ticket_agent.domain.models import Action, ActionType
from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError

_REFERENCE_PREFIX = {
    ActionType.REPLY: "RE",
    ActionType.REFUND: "RF",
    ActionType.ESCALATE: "ES",
    ActionType.CLOSE: "CL",
}


class HelpdeskError(ServiceError):
    """Base class for Helpdesk failures."""


class HelpdeskTemporaryError(HelpdeskError, TemporaryServiceError):
    """The Helpdesk failed for a moment; the same call may succeed if retried."""


class HelpdeskConflictError(HelpdeskError):
    """An idempotency key was reused for a different request. A retry cannot fix this."""


class HelpdeskReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    ticket_id: str
    action: Action


class FakeHelpdesk:
    def __init__(self, failure_rate: float = 0.0, rng: random.Random | None = None) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(f"failure_rate must be between 0 and 1, got {failure_rate}")
        self._failure_rate = failure_rate
        self._rng = rng or random.Random()  # noqa: S311 - simulated failures, not security
        self._by_key: dict[str, HelpdeskReceipt] = {}
        self._performed: list[HelpdeskReceipt] = []

    @property
    def performed(self) -> list[HelpdeskReceipt]:
        """Actions actually carried out, in order. Repeats of an idempotent call are not listed again."""
        return [receipt.model_copy(deep=True) for receipt in self._performed]

    async def perform(self, ticket_id: str, action: Action, idempotency_key: str) -> HelpdeskReceipt:
        # A conflict is checked first: it is permanent, so it must never be hidden behind a temporary failure.
        earlier = self._by_key.get(idempotency_key)
        if earlier is not None and (earlier.ticket_id != ticket_id or earlier.action != action):
            raise HelpdeskConflictError(f"idempotency key {idempotency_key!r} was used for a different request")

        fails = self._rng.random() < self._failure_rate
        response_lost = fails and self._rng.random() < 0.5
        if fails and not response_lost:
            raise HelpdeskTemporaryError("Helpdesk is temporarily unavailable")

        receipt = earlier if earlier is not None else self._record(ticket_id, action, idempotency_key)
        if response_lost:
            raise HelpdeskTemporaryError("Helpdesk response was lost")
        # Callers get copies, so changing a receipt or its action cannot change what is stored.
        return receipt.model_copy(deep=True)

    def _record(self, ticket_id: str, action: Action, idempotency_key: str) -> HelpdeskReceipt:
        number = len(self._performed) + 1
        receipt = HelpdeskReceipt(
            reference=f"{_REFERENCE_PREFIX[action.type]}-{number:04d}",
            ticket_id=ticket_id,
            action=action.model_copy(deep=True),
        )
        self._by_key[idempotency_key] = receipt
        self._performed.append(receipt)
        return receipt
