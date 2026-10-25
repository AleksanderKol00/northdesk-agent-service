from typing import Any

import pytest

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.agent.errors import AgentStepLimitError
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.llm.factory import build_llm_client
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient
from northdesk_ticket_agent.services.llm.openai_client import OpenAIClient


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_fake_mode_is_the_default() -> None:
    assert isinstance(build_llm_client(settings()), FakeLLMClient)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_openai_mode_without_a_key_fails_at_startup(key: str | None) -> None:
    config = settings(llm_mode="openai", openai_api_key=key)
    assert config.openai_api_key is None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_llm_client(config)


async def test_agent_uses_limits_from_settings(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    # The fake needs two steps (lookups, then a decision), so a limit of one must stop it.
    agent = TicketAgent.from_settings(settings(agent_max_steps=1), datastore)
    with pytest.raises(AgentStepLimitError):
        await agent.decide(example_tickets["T-1"])


@pytest.mark.parametrize("rate", [-0.1, 1.5])
def test_helpdesk_failure_rate_must_be_between_zero_and_one(rate: float) -> None:
    with pytest.raises(ValueError, match="helpdesk_failure_rate"):
        settings(helpdesk_failure_rate=rate)


def test_openai_mode_with_a_key_builds_the_openai_client() -> None:
    client = build_llm_client(settings(llm_mode="openai", openai_api_key="sk-test"))
    assert isinstance(client, OpenAIClient)
