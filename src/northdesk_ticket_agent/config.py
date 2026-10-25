from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_timeout_s: float = 30.0

    agent_max_steps: int = 8
    agent_max_decision_attempts: int = 3

    helpdesk_failure_rate: float = Field(default=0.2, ge=0.0, le=1.0)

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_missing(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value
