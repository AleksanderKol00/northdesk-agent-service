"""The Temporal worker process: polls the task queue and runs the ticket activities.

Start it with `uv run northdesk-worker` (needs a Temporal server at `TEMPORAL_ADDRESS`).
"""

import asyncio

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.orchestration.activities import TicketActivities
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk


def build_activities(settings: Settings) -> TicketActivities:
    """Build the data store and clients once; every activity run in this process shares them.

    Sharing matters for the Helpdesk: its idempotency keys live in memory, so a retry is deduplicated only when it
    reaches this same instance. Temporal may send a retry to any worker on the queue, and a restart forgets the keys,
    so run a single worker with the fake Helpdesk. A real Helpdesk keeps the keys on its side, which lifts this limit.
    """
    store = DataStore.from_package_data()
    return TicketActivities(
        agent=TicketAgent.from_settings(settings, store),
        store=store,
        helpdesk=FakeHelpdesk(failure_rate=settings.helpdesk_failure_rate),
    )


async def run_worker(settings: Settings) -> None:
    activities = build_activities(settings)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    worker = Worker(client, task_queue=settings.temporal_task_queue, activities=activities.all())
    await worker.run()


def main() -> None:
    asyncio.run(run_worker(Settings()))
