"""A stand-in for the Helpdesk API that carries out the final action on a ticket.

Every call takes an idempotency key. Repeating a call with the same key returns the first result instead of doing the
action again, so a retried refund is issued once. State lives in memory, so the guarantee holds within one process.
"""

from pydantic import BaseModel, ConfigDict

from northdesk_ticket_agent.domain.models import Action, ActionType
from northdesk_ticket_agent.services.errors import ServiceError

_REFERENCE_PREFIX = {
    ActionType.REPLY: "RE",
    ActionType.REFUND: "RF",
    ActionType.ESCALATE: "ES",
    ActionType.CLOSE: "CL",
}


class HelpdeskError(ServiceError):
    """Base class for Helpdesk failures."""


class HelpdeskConflictError(HelpdeskError):
    """An idempotency key was reused for a different request. A retry cannot fix this."""


class HelpdeskReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    ticket_id: str
    action: Action


class FakeHelpdesk:
    def __init__(self) -> None:
        self._by_key: dict[str, HelpdeskReceipt] = {}
        self._performed: list[HelpdeskReceipt] = []

    @property
    def performed(self) -> list[HelpdeskReceipt]:
        """Actions actually carried out, in order. Repeats of an idempotent call are not listed again."""
        return [receipt.model_copy(deep=True) for receipt in self._performed]

    async def perform(self, ticket_id: str, action: Action, idempotency_key: str) -> HelpdeskReceipt:
        earlier = self._by_key.get(idempotency_key)
        if earlier is not None and (earlier.ticket_id != ticket_id or earlier.action != action):
            raise HelpdeskConflictError(f"idempotency key {idempotency_key!r} was used for a different request")

        receipt = earlier if earlier is not None else self._record(ticket_id, action, idempotency_key)
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
