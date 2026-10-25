# Northdesk Ticket Agent

[![CI](https://github.com/AleksanderKol00/northdesk-agent-service/actions/workflows/ci.yml/badge.svg)](https://github.com/AleksanderKol00/northdesk-agent-service/actions/workflows/ci.yml)

A support-ticket triage service for Northdesk, a fictional software company. An LLM agent reads each
ticket, looks up the customer, their invoices and the knowledge base through tool calls, and proposes
a reply, a refund, an escalation or closing the ticket. Code checks every proposal, refunds wait for a
person's approval, and a Temporal workflow takes the ticket from decision to action, retrying the LLM
and the Helpdesk when they fail for a short time.

## Running with Docker

```sh
cp .env.example .env        # optional: the defaults run with LLM_MODE=fake and no API key
docker compose up --build
```

This starts the Temporal dev server, the API and the worker. The API is on http://localhost:8000 (docs at `/docs`)
and the Temporal web UI on http://localhost:8233. Temporal keeps its state in a volume, so
`docker compose down` keeps the workflow history and `docker compose down -v` wipes it.

Run a single worker (don't `--scale` it, and don't also start `uv run northdesk-worker` against the same server):
see [Known limitations](#known-limitations).

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
- **Duplicates are caught only while Temporal remembers the ticket.** The workflow ID is the ticket ID and is not reused,
  but Temporal deletes a closed workflow once the namespace's retention period has passed. After that, sending the same
  ticket ID starts a new workflow, and `GET /tickets/{id}` for the old one returns 404. Keeping submitted ticket IDs in a
  database of our own would make this permanent.
- **A repeated ticket ID wins over its new content.** A second `POST /tickets` with an ID that was sent before returns
  the existing workflow, even if the subject, body or customer differ; the new content is not processed or compared.
- **A failed ticket is not retried by sending it again.** If the action fails for good, the workflow fails and a new
  `POST` returns that failed workflow. This is on purpose: the failed action may have been carried out (a lost
  response), so running the ticket again could refund twice. Once someone has checked the Helpdesk, the workflow can be
  reset or restarted from the Temporal UI (:8233).
- **The agent loop is one activity.** A temporary LLM error late in the loop retries `ticket.decide` from the first
  step, repeating the earlier LLM calls. Running each LLM call as its own activity would retry only the failed call.

## Tests

- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — tests that exercise the app through HTTP (`TestClient`) and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area, registered in `tests/conftest.py` via `pytest_plugins`.
