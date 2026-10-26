import json
from importlib import resources

import pytest

from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.services.datastore import DataStore


@pytest.fixture
def datastore() -> DataStore:
    return DataStore.from_package_data()


@pytest.fixture
def example_tickets() -> dict[str, Ticket]:
    """The README's example tickets, which ship with the seed data so they can be sent to a running stack."""
    rows = json.loads(resources.files("northdesk_ticket_agent.data").joinpath("tickets.json").read_text("utf-8"))
    return {row["ticket_id"]: Ticket(**row) for row in rows}
