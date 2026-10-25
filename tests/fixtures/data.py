import pytest

from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.services.datastore import DataStore, load_example_tickets


@pytest.fixture
def datastore() -> DataStore:
    return DataStore.from_package_data()


@pytest.fixture
def example_tickets() -> dict[str, Ticket]:
    return {t.ticket_id: t for t in load_example_tickets()}
