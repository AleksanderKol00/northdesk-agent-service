import pytest

from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk


@pytest.fixture
def helpdesk() -> FakeHelpdesk:
    return FakeHelpdesk()
