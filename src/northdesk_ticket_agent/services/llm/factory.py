from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.services.llm.base import LLMClient
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient
from northdesk_ticket_agent.services.llm.openai_client import OpenAIClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_mode == "fake":
        return FakeLLMClient()
    if settings.openai_api_key is None:
        raise ValueError("LLM_MODE=openai needs OPENAI_API_KEY; set it or use LLM_MODE=fake")
    return OpenAIClient(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model,
        timeout_s=settings.openai_timeout_s,
    )
