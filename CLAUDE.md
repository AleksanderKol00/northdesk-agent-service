# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend service (FastAPI, Python 3.14, managed with `uv`) for Northdesk, a fictional software company whose support team receives tickets (password problems, billing questions, outages, spam). Each ticket is processed in several steps with the help of an AI agent, and the service must keep working when the AI or other downstream services fail temporarily — resilience to transient failures is a core design goal, not an afterthought.

Built so far: the decision agent and the decision checks. The workflow engine (Temporal), the ticket API endpoints, the Helpdesk integration and Docker Compose are not built yet; `main.py` only has `/health`.

## Architecture

Packages under `src/northdesk_ticket_agent/`, kept separate on purpose:

- `domain/` — pure business code with no dependency on FastAPI, Temporal or the LLM provider.
  - `models.py`: `Ticket`, `Customer`, `Invoice`, `KbArticle`; `ProposedDecision` (loose, raw model output: plain strings) and `Decision` (checked: `StrEnum`s + an action union of reply/refund/escalate/close).
  - `rules.py`: `check_decision()` — allowed values, required fields per action, and refunds (invoice must belong to the ticket's customer, be `paid`, and the amount must be finite, > 0 and ≤ the invoice). Any violation turns the decision into an escalation that lists the violations.
- `agent/` — `TicketAgent.decide(ticket) -> ProposedDecision`: a tool-calling loop. The model returns its decision by calling the `submit_decision` tool. Unreadable decisions are sent back to the model, up to `max_decision_attempts`; the loop is capped at `max_steps`. An unknown customer raises before any LLM call. Errors in `agent/errors.py` are all permanent.
- `services/` — outside dependencies.
  - `datastore.py`: read-only JSON seed data (`data/*.json`, shipped as package data) with keyword-ranked `search_kb`.
  - `llm/base.py`: provider-neutral types (`Message`, `ToolCall`, `LLMResponse`, `ToolSpec`), the `LLMClient` protocol, and `LLMTransientError` (retryable) / `LLMPermanentError`.
  - `llm/openai_client.py`: the real provider. SDK retries are off, so there is a single retry policy, owned by the caller.
  - `llm/fake_client.py`: a deterministic rule-based stand-in (no key, no network). It is deliberately gullible, so the rules are what block bad decisions.
  - `llm/factory.py`: chooses the client by `LLM_MODE`.
- `config.py` — `Settings` (pydantic-settings, env vars / `.env`; see `.env.example`). Use `TicketAgent.from_settings(settings, store)` so the env limits apply.

Invariants to keep when changing the agent:
- Ticket text is untrusted. `render_ticket` sends it as JSON inside `<ticket>` tags with `<`/`>` escaped, so it can't close the tag.
- `get_customer` / `get_invoices` take no arguments and always use the ticket's `customer_id`.
- Refund ownership is checked against `ticket.customer_id`, never anything the model says.
- The agent returns a *proposed* decision; `check_decision` must always run before anything acts on it.

## Commands

```sh
uv sync                                                   # install deps (incl. dev group)
uv run fastapi dev src/northdesk_ticket_agent/main.py     # dev server with reload on :8000
uv run pytest                                             # all tests
uv run pytest tests/unit                                  # unit tests only
uv run pytest tests/integration                           # integration tests only
uv run pytest tests/unit/test_health.py::test_health_returns_ok   # single test
uv run ruff check . && uv run ruff format .               # lint / format
uv run mypy                                                # type check (strict)
uv run pre-commit install --install-hooks                # once: git hooks
uv run pre-commit run --all-files                        # all hooks by hand
```

Ruff: line length 100; rules `E, F, I, UP, B` plus `S` (bandit), `BLE`, `ASYNC`, `SIM`, `C4`, `PIE`, `RET`, `RUF`, `PT`. Don't use `assert` for checks in `src/` (stripped under `-O`); use explicit guards. mypy runs in `strict` mode with the pydantic plugin over `src` and `tests`.

Pre-commit (`.pre-commit-config.yaml`): file hygiene, `uv lock --check`, ruff, mypy on commit; pytest on push. Hooks run through `uv run`, so tool versions come from `uv.lock`. The FastAPI entrypoint is configured in `pyproject.toml` as `northdesk_ticket_agent.main:app`.

## Git

- Work on a separate branch per task, never directly on `main`. Branch from an up-to-date `main` and name it `<type>/<short-kebab-description>`, with type one of `feat`, `fix`, `refactor`, `test`, `docs`, `chore` (e.g. `feat/temporal-workflow`, `fix/refund-nan-check`).
- Merge back into `main` through a pull request once tests and ruff pass, then delete the branch.
- Make granular commits: one logical change per commit (e.g. dependencies/config, seed data, domain rules, a service adapter, the agent, docs), each with its own tests, not one big commit.
- Each commit should leave the repo working: tests, ruff and mypy pass at every commit.

## Layout and test conventions

- Source uses the `src/` layout (`src/northdesk_ticket_agent/`), built with `uv_build`.
- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — exercise the app over HTTP via `TestClient` and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area: `client.py` (`client` TestClient), `data.py` (`datastore`, `example_tickets` keyed by ticket id), `llm.py` (`scripted_llm` factory replaying canned `LLMResponse`s; the `tool_call()` helper builds them). New fixture modules must be registered in `tests/conftest.py` via `pytest_plugins`.
- Async tests need no marker: `asyncio_mode = "auto"` (pytest-asyncio).
- Tests never need network or an API key; use the fake LLM or `scripted_llm`.
- Pytest runs with `--import-mode=importlib` and `pythonpath = ["."]`, so test directories have no package-name collisions and fixtures are imported as `tests.fixtures.<module>`.
