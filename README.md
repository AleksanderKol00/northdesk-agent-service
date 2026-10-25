# Northdesk Ticket Agent

[![CI](https://github.com/AleksanderKol00/northdesk-agent-service/actions/workflows/ci.yml/badge.svg)](https://github.com/AleksanderKol00/northdesk-agent-service/actions/workflows/ci.yml)

Backend service for Northdesk, a small (fictional) software company whose support team
receives many tickets every day: password problems, billing questions, outages, and some spam.

The service processes each ticket in several steps with the help of an AI agent, and is designed
to keep working when the AI or other downstream services fail temporarily.

## Development

GitHub Actions (`.github/workflows/ci.yml`) runs on every push: the pre-commit hooks plus a secret scan of the full history, and the tests with the 90% coverage threshold.

```sh
uv sync                                                   # install deps
uv run fastapi dev src/northdesk_ticket_agent/main.py     # run with reload on :8000
uv run northdesk-worker                                   # Temporal worker (needs a server at TEMPORAL_ADDRESS)
uv run pytest                                             # all tests
uv run pytest tests/unit                                  # unit tests only
uv run pytest tests/integration                           # integration tests only
uv run pytest --cov                                       # with coverage (fails under 90%)
uv run ruff check . && uv run ruff format .               # lint / format
uv run mypy                                               # type check (strict)
uv run lint-imports                                       # package boundaries
uv run deptry src                                         # declared vs. imported deps
uv run pre-commit install --install-hooks                 # once: git hooks
uv run pre-commit run --all-files                         # all hooks by hand
```

## Configuration

Settings come from environment variables (or a local `.env`); see `.env.example`.

- `LLM_MODE=fake` (default) uses a rule-based stand-in: no API key, no network, deterministic.
- `LLM_MODE=openai` uses OpenAI tool calling and needs `OPENAI_API_KEY`.
- `HELPDESK_FAILURE_RATE` (0 to 1, default 0.2) makes the fake Helpdesk fail at random with temporary errors, to exercise retries.
- `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` tell the worker where to find Temporal and which queue to poll.

## Known limitations

- **One worker with the fake Helpdesk.** Its idempotency keys live in the worker's memory. Temporal may send a retry
  to another worker, and a restart forgets the keys, so with several workers a refund whose response was lost could be
  issued twice. A real Helpdesk keeps the keys on its side, which removes this limit.
- **One action of each type per ticket.** The Helpdesk idempotency key is `<ticket_id>:<action type>`, so processing a
  ticket again cannot send a second, different reply (it fails as a conflict). This keeps a workflow reset from issuing a
  refund twice; a ticket is processed once, as a repeated submission returns the existing workflow.
- **The agent loop is one activity.** A temporary LLM error late in the loop retries `ticket.decide` from the first
  step, repeating the earlier LLM calls. Running each LLM call as its own activity would retry only the failed call.

## Tests

- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — tests that exercise the app through HTTP (`TestClient`) and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area, registered in `tests/conftest.py` via `pytest_plugins`.
