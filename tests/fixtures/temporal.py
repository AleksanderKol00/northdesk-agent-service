import os
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.orchestration.activities import TicketActivities
from northdesk_ticket_agent.orchestration.tickets import TemporalTicketWorkflows
from northdesk_ticket_agent.orchestration.workflow import TicketWorkflow
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.fake_helpdesk import FakeHelpdesk
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient

# A test server binary already on disk, for running the tests with no network at all. Without it the SDK downloads the
# binary on first use and caches it in the temp directory.
TEST_SERVER_PATH_ENV = "TEMPORAL_TEST_SERVER_PATH"


async def start_test_server() -> WorkflowEnvironment:
    """Temporal's time-skipping test server, from `TEMPORAL_TEST_SERVER_PATH` if set."""
    return await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
        test_server_existing_path=os.environ.get(TEST_SERVER_PATH_ENV) or None,
    )


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    """Temporal's time-skipping test server, started once per session: waiting 24 h takes no real time.

    See `start_test_server` for where the binary comes from. Tests using it run on the session event loop
    (`pytest.mark.asyncio(loop_scope="session")`).
    """
    async with await start_test_server() as env:
        yield env


@pytest_asyncio.fixture(loop_scope="session")
async def isolated_workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    """A test server of its own, for a test that leaves calls unanswered: on the shared server, a query or update that
    timed out keeps time skipping off for every later test, even once its workflow is terminated."""
    async with await start_test_server() as env:
        yield env


@pytest_asyncio.fixture(loop_scope="session")
async def temporal_workflows(
    workflow_env: WorkflowEnvironment, datastore: DataStore, helpdesk: FakeHelpdesk
) -> AsyncIterator[TemporalTicketWorkflows]:
    """`TemporalTicketWorkflows` on the test server, with a worker running the real activities and the fake LLM.

    Its client traces like `connect`'s, so the spans reach the `spans` fixture once a test asks for it.
    """
    task_queue = f"test-{uuid.uuid4()}"
    config = workflow_env.client.config()
    config["interceptors"] = [TracingInterceptor()]
    client = Client(**config)
    activities = TicketActivities(TicketAgent(FakeLLMClient(), datastore), datastore, helpdesk)
    async with Worker(client, task_queue=task_queue, workflows=[TicketWorkflow], activities=activities.all()):
        yield TemporalTicketWorkflows(client, task_queue)
