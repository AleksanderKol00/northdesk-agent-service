from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from northdesk_ticket_agent.config import Settings


async def connect(settings: Settings) -> Client:
    """A Temporal client for the API and the worker, with the pydantic data converter the workflow's models need."""
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
