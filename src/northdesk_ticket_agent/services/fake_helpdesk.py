"""An in-memory stand-in for the Helpdesk.

It fails at random with a temporary error, at a configurable rate. Half of those failures happen after the action was
done (the response is lost), which is exactly the case a retry must not repeat. Idempotency keys live in memory, so
they are honoured within one process only.
"""

import logging
import random

from northdesk_ticket_agent.domain.models import Action, ActionType
from northdesk_ticket_agent.services.helpdesk import HelpdeskConflictError, HelpdeskReceipt, HelpdeskTemporaryError

logger = logging.getLogger(__name__)

_REFERENCE_PREFIX = {
    ActionType.REPLY: "RE",
    ActionType.REFUND: "RF",
    ActionType.ESCALATE: "ES",
    ActionType.CLOSE: "CL",
}


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
            logger.warning("Simulated failure before acting", extra={"idempotency_key": idempotency_key})
            raise HelpdeskTemporaryError("Helpdesk is temporarily unavailable")

        if earlier is not None:
            receipt = earlier
            logger.info(
                "Repeated request; returning the first receipt",
                extra={"idempotency_key": idempotency_key, "reference": receipt.reference},
            )
        else:
            receipt = self._record(ticket_id, action, idempotency_key)
            logger.info(
                "Carried out %s",
                action.type,
                extra={"idempotency_key": idempotency_key, "reference": receipt.reference},
            )
        if response_lost:
            logger.warning("Simulated failure after acting: response lost", extra={"idempotency_key": idempotency_key})
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
