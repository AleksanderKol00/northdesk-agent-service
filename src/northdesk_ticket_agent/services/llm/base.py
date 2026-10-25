"""Provider-neutral types for talking to an LLM with tool calling."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from northdesk_ticket_agent.services.errors import ServiceError, TemporaryServiceError


class LLMError(ServiceError):
    """Base class for failures of the LLM provider."""


class LLMTransientError(LLMError, TemporaryServiceError):
    """The call failed for a reason that may go away (timeout, rate limit, 5xx)."""


class LLMPermanentError(LLMError):
    """The call failed in a way a retry cannot fix (bad key, invalid request)."""


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant messages
    tool_call_id: str | None = None  # tool messages


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=self.tool_calls)


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


class LLMClient(Protocol):
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...
