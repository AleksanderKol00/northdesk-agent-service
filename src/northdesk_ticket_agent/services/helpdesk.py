"""The Helpdesk, which carries out the final action on a ticket: what the rest of the service relies on.

Every call takes an idempotency key: repeating a call with the same key returns the first receipt instead of acting
again, so a retry after a lost response never refunds twice. Reusing a key for a different request is a conflict.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from northdesk_ticket_agent.domain.models import Action
from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError


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


class Helpdesk(Protocol):
    async def perform(self, ticket_id: str, action: Action, idempotency_key: str) -> HelpdeskReceipt: ...
