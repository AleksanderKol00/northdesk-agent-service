import pytest

from northdesk_ticket_agent.services.fake_helpdesk import FakeHelpdesk


@pytest.fixture
def helpdesk() -> FakeHelpdesk:
    return FakeHelpdesk()
