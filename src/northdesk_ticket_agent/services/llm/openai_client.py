from typing import Any

import openai

from northdesk_ticket_agent.services.llm.base import (
    LLMPermanentError,
    LLMResponse,
    LLMTransientError,
    Message,
    ToolCall,
    ToolSpec,
)

# 408 timeout, 409 conflict, 429 rate limit, 5xx server errors: worth retrying.
_TRANSIENT_STATUS = {408, 409, 429}


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        # The SDK's own retries are off: retries belong to the caller, so there is one
        # retry policy with one limit instead of two stacked ones.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key, timeout=timeout_s, max_retries=0
        )
        self._model = model

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=[to_openai_message(m) for m in messages],
                tools=[to_openai_tool(t) for t in tools],
                tool_choice="required",
            )
        except openai.APIConnectionError as exc:  # includes timeouts
            raise LLMTransientError(f"OpenAI connection failed: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code in _TRANSIENT_STATUS or exc.status_code >= 500:
                raise LLMTransientError(f"OpenAI returned {exc.status_code}") from exc
            raise LLMPermanentError(f"OpenAI returned {exc.status_code}: {exc}") from exc
        except openai.OpenAIError as exc:  # e.g. a response body that doesn't match the schema
            raise LLMTransientError(f"OpenAI call failed: {exc}") from exc

        if not completion.choices:
            raise LLMTransientError("OpenAI returned no choices")
        return from_openai_message(completion.choices[0].message)


def to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_openai_message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    out: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return out


def from_openai_message(message: Any) -> LLMResponse:
    calls = [
        ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
        for call in message.tool_calls or []
        if getattr(call, "type", "function") == "function"
    ]
    return LLMResponse(content=message.content, tool_calls=calls)
