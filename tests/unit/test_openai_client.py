from types import SimpleNamespace
from typing import Any, cast

import httpx2
import openai
import pytest

from northdesk_ticket_agent.services.llm.base import (
    LLMPermanentError,
    LLMTransientError,
    Message,
    ToolCall,
    ToolSpec,
)
from northdesk_ticket_agent.services.llm.openai_client import OpenAIClient

TOOLS = [
    ToolSpec(
        name="get_customer",
        description="Get the customer.",
        parameters={"type": "object", "properties": {}},
    )
]
REQUEST = httpx2.Request("POST", "https://api.openai.test/v1/chat/completions")


class StubCompletions:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(result: Any) -> tuple[OpenAIClient, StubCompletions]:
    completions = StubCompletions(result)
    stub = cast(openai.AsyncOpenAI, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    return OpenAIClient(api_key="unused", model="test-model", client=stub), completions


def completion(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def status_error(status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return openai.APIStatusError("boom", response=response, body=None)


async def test_maps_tool_calls_and_sends_neutral_messages() -> None:
    call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="search_kb", arguments='{"query": "x"}'),
    )
    client, completions = make_client(completion(tool_calls=[call]))
    messages = [
        Message(role="system", content="sys"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_0", name="get_customer", arguments="{}")],
        ),
        Message(role="tool", tool_call_id="call_0", content="{}"),
    ]

    response = await client.complete(messages, TOOLS)

    assert response.tool_calls == [ToolCall(id="call_1", name="search_kb", arguments='{"query": "x"}')]
    sent = completions.kwargs
    assert sent["model"] == "test-model"
    assert sent["tool_choice"] == "required"
    assert sent["tools"][0]["function"]["name"] == "get_customer"
    assert sent["messages"][1]["tool_calls"][0]["function"]["name"] == "get_customer"
    assert sent["messages"][2] == {"role": "tool", "tool_call_id": "call_0", "content": "{}"}


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        status_error(429),
        status_error(500),
        status_error(503),
        openai.APIResponseValidationError(response=httpx2.Response(200, request=REQUEST), body={}),
    ],
)
async def test_temporary_failures_are_transient(error: Exception) -> None:
    client, _ = make_client(error)
    with pytest.raises(LLMTransientError):
        await client.complete([Message(role="user", content="hi")], TOOLS)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_request_errors_are_permanent(status: int) -> None:
    client, _ = make_client(status_error(status))
    with pytest.raises(LLMPermanentError):
        await client.complete([Message(role="user", content="hi")], TOOLS)


async def test_a_response_without_choices_is_transient() -> None:
    client, _ = make_client(SimpleNamespace(choices=[]))
    with pytest.raises(LLMTransientError, match="no choices"):
        await client.complete([Message(role="user", content="hi")], TOOLS)
