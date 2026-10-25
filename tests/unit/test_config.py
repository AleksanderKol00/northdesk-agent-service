import pytest

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.agent.errors import AgentStepLimitError
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.llm.factory import build_llm_client
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_fake_mode_is_the_default() -> None:
    assert isinstance(build_llm_client(settings()), FakeLLMClient)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_openai_mode_without_a_key_fails_at_startup(key: str | None) -> None:
    config = settings(llm_mode="openai", openai_api_key=key)
    assert config.openai_api_key is None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_llm_client(config)


async def test_agent_uses_limits_from_settings(
    datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    # The fake needs two steps (lookups, then a decision), so a limit of one must stop it.
    agent = TicketAgent.from_settings(settings(agent_max_steps=1), datastore)
    with pytest.raises(AgentStepLimitError):
        await agent.decide(example_tickets["T-1"])
