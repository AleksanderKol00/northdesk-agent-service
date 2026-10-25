import pytest

from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError
from northdesk_ticket_agent.services.helpdesk import HelpdeskConflictError
from northdesk_ticket_agent.services.llm.base import LLMPermanentError, LLMTransientError


@pytest.mark.parametrize("error", [LLMTransientError])
def test_temporary_errors_share_one_base(error: type[ServiceError]) -> None:
    assert issubclass(error, TemporaryServiceError)


@pytest.mark.parametrize("error", [LLMPermanentError, HelpdeskConflictError])
def test_permanent_errors_are_not_temporary(error: type[ServiceError]) -> None:
    assert issubclass(error, ServiceError)
    assert not issubclass(error, TemporaryServiceError)
