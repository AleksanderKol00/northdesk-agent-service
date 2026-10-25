import json
from collections.abc import Callable
from typing import Any

import pytest

from northdesk_ticket_agent.services.llm.base import LLMResponse, Message, ToolCall, ToolSpec


class ScriptedLLM:
    """Replays canned responses and records every conversation it was sent."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of responses")
        return self._responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any] | str | None = None, call_id: str = "c1") -> LLMResponse:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


ScriptedLLMFactory = Callable[..., ScriptedLLM]


@pytest.fixture
def scripted_llm() -> ScriptedLLMFactory:
    return lambda *responses: ScriptedLLM(list(responses))
