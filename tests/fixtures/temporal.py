from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    """Temporal's time-skipping test server, started once per session: waiting 24 h takes no real time.

    The SDK downloads the test server binary on first use and caches it in the temp directory; after that no network
    is needed. Tests using it run on the session event loop (`pytest.mark.asyncio(loop_scope="session")`).
    """
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        yield env
