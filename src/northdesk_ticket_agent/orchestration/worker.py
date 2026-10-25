"""The Temporal worker process: polls the task queue and runs the ticket workflow and its activities.

Start it with `uv run northdesk-worker` (needs a Temporal server at `TEMPORAL_ADDRESS`).
"""

import asyncio
import logging
import signal
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.logs import LogLevel, configure_logging
from northdesk_ticket_agent.orchestration.activities import TicketActivities
from northdesk_ticket_agent.orchestration.client import connect
from northdesk_ticket_agent.orchestration.workflow import TicketWorkflow
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk

# How long a stopping worker lets running activities finish before cancelling them. Kept under Docker's default 10 s
# stop timeout, so a `docker compose stop` ends in a clean shutdown and not a kill.
GRACEFUL_SHUTDOWN = timedelta(seconds=8)
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)

logger = logging.getLogger(__name__)


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


def configure_worker_logging(level: LogLevel) -> None:
    """JSON logs, as in the API. Temporal's details become fields of the line, so they are not appended to the
    message as well (the SDK's default)."""
    configure_logging(level)
    activity.logger.activity_info_on_message = False
    workflow.logger.workflow_info_on_message = False


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
        logger.info("Worker started", extra={"task_queue": settings.temporal_task_queue})
        await stop.wait()
        logger.info("Stopping the worker")


def main() -> None:
    settings = Settings()
    configure_worker_logging(settings.log_level)
    asyncio.run(run_worker(settings))
