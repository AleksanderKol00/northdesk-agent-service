import asyncio
import os
import random
import signal
from collections.abc import Awaitable, Callable

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from northdesk_ticket_agent.agent.agent import TicketAgent
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.domain.models import (
    ActionType,
    EscalateAction,
    ProposedDecision,
    RefundAction,
    Ticket,
)
from northdesk_ticket_agent.orchestration.activities import ActInput, CheckInput, TicketActivities
from northdesk_ticket_agent.orchestration.worker import STOP_SIGNALS, build_activities, stop_on_signals
from northdesk_ticket_agent.services.datastore import DataStore
from northdesk_ticket_agent.services.helpdesk import FakeHelpdesk
from northdesk_ticket_agent.services.llm.base import (
    LLMClient,
    LLMError,
    LLMPermanentError,
    LLMResponse,
    LLMTransientError,
    Message,
    ToolSpec,
)
from northdesk_ticket_agent.services.llm.fake_client import FakeLLMClient
from tests.fixtures.llm import ScriptedLLMFactory, tool_call

REFUND = RefundAction(invoice_id="INV-1002", amount_eur=49.0)


class FailingLLM:
    def __init__(self, error: LLMError) -> None:
        self._error = error

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        raise self._error


class ScriptedRandom(random.Random):
    """Returns the given rolls in order. Per Helpdesk call: the first decides failure, the second whether it happens
    after the action was done (response lost)."""

    def __init__(self, *rolls: float) -> None:
        super().__init__()
        self._rolls = list(rolls)

    def random(self) -> float:
        return self._rolls.pop(0)


def activities(
    datastore: DataStore, llm: LLMClient | None = None, helpdesk: FakeHelpdesk | None = None
) -> TicketActivities:
    return TicketActivities(
        agent=TicketAgent(llm or FakeLLMClient(), datastore),
        store=datastore,
        helpdesk=helpdesk or FakeHelpdesk(),
    )


async def run_failing(fn: Callable[..., Awaitable[object]], *args: object) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(fn, *args)
    return caught.value


# decide


async def test_decide_returns_the_agents_proposal(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    proposed = await ActivityEnvironment().run(activities(datastore).decide, example_tickets["T-2"])

    assert proposed.action == "refund"
    assert proposed.invoice_id == "INV-1002"


async def test_decide_unknown_customer_is_not_retried(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    error = await run_failing(activities(datastore).decide, example_tickets["T-7"])

    assert error.type == "UnknownCustomerError"
    assert error.non_retryable


async def test_decide_unreadable_decision_is_not_retried(
    datastore: DataStore, example_tickets: dict[str, Ticket], scripted_llm: ScriptedLLMFactory
) -> None:
    llm = scripted_llm(*[tool_call("submit_decision", "not json")] * 3)

    error = await run_failing(activities(datastore, llm).decide, example_tickets["T-1"])

    assert error.type == "DecisionParseError"
    assert error.non_retryable


async def test_decide_step_limit_is_not_retried(
    datastore: DataStore, example_tickets: dict[str, Ticket], scripted_llm: ScriptedLLMFactory
) -> None:
    llm = scripted_llm(*[tool_call("get_customer")] * 8)

    error = await run_failing(activities(datastore, llm).decide, example_tickets["T-1"])

    assert error.type == "AgentStepLimitError"
    assert error.non_retryable


@pytest.mark.parametrize(
    ("llm_error", "retryable"),
    [
        (LLMTransientError("rate limited"), True),
        (LLMPermanentError("bad API key"), False),
    ],
)
async def test_decide_llm_errors_keep_their_retryability(
    datastore: DataStore, example_tickets: dict[str, Ticket], llm_error: LLMError, retryable: bool
) -> None:
    error = await run_failing(activities(datastore, FailingLLM(llm_error)).decide, example_tickets["T-1"])

    assert error.type == type(llm_error).__name__
    assert error.non_retryable is not retryable
    assert error.message == str(llm_error)


# check


async def test_check_accepts_a_valid_refund(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    proposed = ProposedDecision(
        category="billing", priority="normal", action="refund", invoice_id="INV-1002", amount_eur=49.0
    )

    decision = await ActivityEnvironment().run(
        activities(datastore).check, CheckInput(ticket=example_tickets["T-2"], proposed=proposed)
    )

    assert decision.action == REFUND
    assert decision.violations == []


async def test_check_uses_the_tickets_customer_for_ownership(
    datastore: DataStore, example_tickets: dict[str, Ticket]
) -> None:
    # INV-1002 belongs to C-100, but T-1 comes from C-200.
    proposed = ProposedDecision(
        category="billing", priority="normal", action="refund", invoice_id="INV-1002", amount_eur=49.0
    )

    decision = await ActivityEnvironment().run(
        activities(datastore).check, CheckInput(ticket=example_tickets["T-1"], proposed=proposed)
    )

    assert isinstance(decision.action, EscalateAction)
    assert decision.violations == ["invoice INV-1002 does not belong to customer C-200"]


async def test_check_rejects_unknown_articles(datastore: DataStore, example_tickets: dict[str, Ticket]) -> None:
    proposed = ProposedDecision(
        category="account", priority="normal", action="reply", message="Hi", article_ids=["KB-404"]
    )

    decision = await ActivityEnvironment().run(
        activities(datastore).check, CheckInput(ticket=example_tickets["T-1"], proposed=proposed)
    )

    assert decision.action.type == ActionType.ESCALATE
    assert decision.violations == ["reply cites unknown articles: KB-404"]


# act


async def test_act_performs_the_action(datastore: DataStore, helpdesk: FakeHelpdesk) -> None:
    receipt = await ActivityEnvironment().run(
        activities(datastore, helpdesk=helpdesk).act, ActInput(ticket_id="T-2", action=REFUND)
    )

    assert receipt.ticket_id == "T-2"
    assert receipt.action == REFUND
    assert helpdesk.performed == [receipt]


async def test_act_retried_refund_is_issued_once(datastore: DataStore, helpdesk: FakeHelpdesk) -> None:
    act = activities(datastore, helpdesk=helpdesk).act
    request = ActInput(ticket_id="T-2", action=REFUND)

    first = await ActivityEnvironment().run(act, request)
    second = await ActivityEnvironment().run(act, request)

    assert second == first
    assert len(helpdesk.performed) == 1


async def test_act_retry_after_a_lost_response_returns_the_first_receipt(datastore: DataStore) -> None:
    # Call 1: fails (0.0) after acting (0.0), so the response is lost. Call 2, Temporal's retry: succeeds (0.99).
    helpdesk = FakeHelpdesk(failure_rate=0.5, rng=ScriptedRandom(0.0, 0.0, 0.99))
    act = activities(datastore, helpdesk=helpdesk).act
    request = ActInput(ticket_id="T-2", action=REFUND)

    error = await run_failing(act, request)
    assert error.type == "HelpdeskTemporaryError"
    assert not error.non_retryable

    receipt = await ActivityEnvironment().run(act, request)
    assert helpdesk.performed == [receipt]


async def test_act_temporary_helpdesk_failure_is_retried(datastore: DataStore) -> None:
    # Fails (0.99 < 1.0) before acting (0.99 >= 0.5).
    helpdesk = FakeHelpdesk(failure_rate=1.0, rng=ScriptedRandom(0.99, 0.99))

    error = await run_failing(activities(datastore, helpdesk=helpdesk).act, ActInput(ticket_id="T-2", action=REFUND))

    assert error.type == "HelpdeskTemporaryError"
    assert not error.non_retryable
    assert helpdesk.performed == []


async def test_act_a_different_refund_on_the_same_ticket_is_not_retried(
    datastore: DataStore, helpdesk: FakeHelpdesk
) -> None:
    act = activities(datastore, helpdesk=helpdesk).act
    await ActivityEnvironment().run(act, ActInput(ticket_id="T-2", action=REFUND))

    other = RefundAction(invoice_id="INV-1002", amount_eur=10.0)
    error = await run_failing(act, ActInput(ticket_id="T-2", action=other))

    assert error.type == "HelpdeskConflictError"
    assert error.non_retryable
    assert len(helpdesk.performed) == 1


# worker wiring


async def test_worker_builds_activities_from_settings(example_tickets: dict[str, Ticket]) -> None:
    built = build_activities(Settings(_env_file=None, llm_mode="fake", helpdesk_failure_rate=0.0))

    assert [fn.__name__ for fn in built.all()] == ["decide", "check", "act"]
    receipt = await ActivityEnvironment().run(
        built.act, ActInput(ticket_id="T-3", action=EscalateAction(reason="outage"))
    )
    assert receipt.reference.startswith("ES-")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_worker_stops_on_signal(sig: signal.Signals) -> None:
    loop = asyncio.get_running_loop()
    stop = stop_on_signals()
    try:
        assert not stop.is_set()
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        for handled in STOP_SIGNALS:
            loop.remove_signal_handler(handled)
