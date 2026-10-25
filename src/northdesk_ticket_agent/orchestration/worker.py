"""The Temporal worker process: polls the task queue and runs the ticket workflow and its activities.

Start it with `uv run northdesk-worker` (needs a Temporal server at `TEMPORAL_ADDRESS`).
"""

import asyncio
import signal
from datetime import timedelta

from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.orchestration.activities import TicketActivities
from northdesk_ticket_agent.orchestration.client import connect
from northdesk_ticket_agent.orchestration.workflow import TicketWorkflow
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk

# How long a stopping worker lets running activities finish before cancelling them. Kept under Docker's default 10 s
# stop timeout, so a `docker compose stop` ends in a clean shutdown and not a kill.
GRACEFUL_SHUTDOWN = timedelta(seconds=8)
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


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


def stop_on_signals() -> asyncio.Event:
    """Return an event that SIGTERM or SIGINT sets, on the running loop.

    In a container the worker is PID 1, and the kernel ignores SIGTERM for PID 1 unless the process handles it: without
    this, `docker stop` would wait out its timeout and kill the worker in the middle of an activity.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    return stop


async def run_worker(settings: Settings) -> None:
    """Run the worker until SIGTERM or SIGINT, then shut it down gracefully."""
    stop = stop_on_signals()
    activities = build_activities(settings)
    client = await connect(settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[TicketWorkflow],
        activities=activities.all(),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
    )
    async with worker:
        await stop.wait()


def main() -> None:
    asyncio.run(run_worker(Settings()))
