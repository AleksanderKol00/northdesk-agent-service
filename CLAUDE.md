# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend service (FastAPI, Python 3.14, managed with `uv`) that triages support tickets for Northdesk, a fictional software company. An LLM agent looks up the customer, their invoices and the knowledge base through tool calls and proposes what to do; code checks the proposal, refunds wait for a person, and a Temporal workflow carries each ticket from decision to action. The LLM and the Helpdesk can both fail transiently, so retries, idempotent actions and escalation with a clear reason are part of the design, not add-ons.

Built so far: the decision agent, the decision checks, the fake Helpdesk client, the Temporal ticket workflow, activities and worker, the ticket API, and structured (JSON) logs. Docker Compose runs Temporal, the API and the worker. The full README is still to come.

## Architecture

Packages under `src/northdesk_ticket_agent/`, kept separate on purpose:

- `main.py` — builds the FastAPI app: `create_app(workflows=None)`; `app = create_app()` is the entry point. Without `workflows` the lifespan calls `configure_logging`, connects to Temporal from `Settings` and puts a `TemporalTicketWorkflows` on `app.state`; tests pass a stub, and logging is left alone.
- `logs.py` — JSON lines, one per record: `time` (UTC), `level`, `logger`, `message`, then fields from Temporal's details (`temporal_workflow` / `temporal_activity`: their `workflow_id` is lifted to the top, and also as `ticket_id` for `TicketWorkflow`), from `log_context(**fields)` (a contextvar; `bind_log_context` adds to the innermost one), and from `extra`, later ones winning. `configure_logging(level, stream)` replaces the root handlers and routes uvicorn's loggers through them (its access log is off; the API has its own). Every line about a ticket must carry `ticket_id` and `workflow_id`. Log IDs, types and counts, never ticket text, replies or the model's tool arguments. Outside the layers, like `config.py`, which imports its `LogLevel`: so it imports nothing from the package and no Temporal.
- `api/` — the HTTP layer. `access_log.py`: middleware that runs each request in a `log_context` and logs one line per request (method, path, status, duration; `/health` at DEBUG); the endpoints bind the ticket's IDs to it. `tickets.py` (router): `POST /tickets` starts the workflow and returns at once (202, `created: true`; 200, `created: false` when the ticket ID was sent before), `GET /tickets/{id}` returns the `TicketState` plus `workflow_id`, `POST /tickets/{id}/approval` takes an `Approval`. `health.py`: `/health`. `schemas.py`: response models. `dependencies.py`: `Workflows`, the `TicketWorkflows` from `app.state`. `errors.py`: exception handlers, `TicketNotFoundError` → 404, `NothingToApproveError` → 409, `TicketWorkflowsUnavailableError` → 503, so endpoints don't catch them; each is logged as a warning, a 503 with its cause's traceback.
- `domain/` — pure business code with no dependency on FastAPI, Temporal or the LLM provider.
  - `models.py`: `Ticket` (`ticket_id` is limited to `[A-Za-z0-9_.-]{1,64}` and starts with a letter or digit, so it is never `.` or `..`: it is also the workflow ID, an API path segment and part of the idempotency key), `Customer`, `Invoice`, `KbArticle`; `ProposedDecision` (loose, raw model output: plain strings) and `Decision` (checked: `StrEnum`s + an action union of reply/refund/escalate/close).
  - `rules.py`: `check_decision()` — allowed values, required fields per action, and refunds (invoice must belong to the ticket's customer, be `paid`, and the amount must be finite, > 0 and ≤ the invoice). Any violation turns the decision into an escalation that lists the violations.
- `agent/` — `TicketAgent.decide(ticket) -> ProposedDecision`: a tool-calling loop. The model returns its decision by calling the `submit_decision` tool. Unreadable decisions are sent back to the model, up to `max_decision_attempts`; the loop is capped at `max_steps`. An unknown customer raises before any LLM call. Errors in `agent/errors.py` are all permanent.
- `services/` — outside dependencies.
  - `errors.py`: `ServiceError` and `TemporaryServiceError`. Every service error derives from `ServiceError`; temporary ones (`LLMTransientError`, `HelpdeskTemporaryError`) also derive from `TemporaryServiceError`, so retry decisions are one `isinstance` check.
  - `datastore.py`: read-only JSON seed data (`data/*.json`, shipped as package data) with keyword-ranked `search_kb`.
  - `llm/base.py`: provider-neutral types (`Message`, `ToolCall`, `LLMResponse`, `ToolSpec`), the `LLMClient` protocol, and `LLMTransientError` (temporary) / `LLMPermanentError`.
  - `llm/openai_client.py`: the real provider. SDK retries are off, so there is a single retry policy, owned by the caller.
  - `llm/fake_client.py`: a deterministic rule-based stand-in (no key, no network). It is deliberately gullible, so the rules are what block bad decisions.
  - `llm/factory.py`: chooses the client by `LLM_MODE`.
  - `helpdesk.py`: `FakeHelpdesk.perform(ticket_id, action, idempotency_key)` carries out the final action and returns a `HelpdeskReceipt`. It fails at random (`HELPDESK_FAILURE_RATE`) with `HelpdeskTemporaryError`; half of those failures happen after the action was done (lost response). A repeated key returns the first receipt, so a retry never acts twice; reusing a key for a different request raises `HelpdeskConflictError` (permanent), checked before any random failure. Receipts are frozen and stored/returned as copies. State is in memory, per process.
- `orchestration/` — Temporal.
  - `activities.py`: `TicketActivities` with `decide` (runs the agent), `check` (wraps `check_decision`, loading the ticket customer's invoices and the article ids) and `act` (calls the Helpdesk with idempotency key `<ticket_id>:<action type>`). Activity names are `ticket.decide`, `ticket.check`, `ticket.act`. Each runs in a `log_context` with the ticket's IDs (so the agent's and the Helpdesk's lines carry them) and logs its outcome through `activity.logger`; a failed attempt is logged by the SDK ("Completing activity as failed"). Known errors become an `ApplicationError` whose `type` is the original class name: a `TemporaryServiceError` is retryable; any other `ServiceError` and every `AgentError` are non-retryable. Unexpected errors are left to the workflow's retry policy.
  - `workflow.py`: `TicketWorkflow` (`TicketWorkflow`, input `Ticket`, result `TicketState`): decide → check → wait for approval (refunds only) → act, calling the activities by name. It is the only place that guarantees `check` runs before `act`. Any failure while deciding or checking (non-retryable error, or retries used up) becomes an escalation whose reason is `Could not decide automatically: <error type>: <message>` (a Temporal timeout has no type, only its message); a refund that is rejected or gets no answer within `APPROVAL_TIMEOUT` (24 h) is escalated too, and an approval accepted by the update in the same activation as the timer still counts. If `act` fails for good, the workflow fails (`ActionFailed`) with step `failed` and `error` set; after temporary errors or timeouts the error says the action may have been carried out (a lost response), after a non-retryable one that it was not. Timeouts and retry policies are constants at the top: attempts are capped, but long enough to ride out a short outage (about 1.5 min of backoff for `decide`, 15 min for `act`). Query `status` returns `TicketState` (step, checked decision, approval, the action carried out, Helpdesk receipt, error); update `approve` takes an `Approval`, is rejected by its validator unless a refund is waiting ("already approved/rejected" if it was answered; an update to a finished workflow fails with an `RPCError` instead), and returns only once the workflow has moved past the approval, so its result shows the action that follows. It is declared with a bare `@workflow.update` (named after the method): with `name=`, the SDK's types lose the async result. State is built in `@workflow.init`, so the query works from the start. Imports of our models go through `workflow.unsafe.imports_passed_through()` for the sandbox. It logs only through `workflow.logger` (silent during replay), with no IDs of its own: the JSON formatter lifts them from the logger's `temporal_workflow` details.
  - `tickets.py`: what the API needs from Temporal, as the `TicketWorkflows` protocol (`start`, `status`, `approve`), implemented by `TemporalTicketWorkflows`. Workflow ID = ticket ID with `WorkflowIDReusePolicy.REJECT_DUPLICATE`, so a ticket is not processed twice, even after its workflow finished, as long as Temporal keeps the closed workflow (the namespace's retention period); a duplicate start returns the existing workflow (`StartedTicket.created` is False), whatever the second request's body, and a failed workflow is not restarted by sending the ticket again. Every call has `rpc_timeout` (`RPC_TIMEOUT`, 10 s): the query and the update need a worker to answer, and without one they would wait out Temporal's own deadline.
  - `errors.py`: what `TemporalTicketWorkflows` turns Temporal's errors into: `TicketNotFoundError` (API 404) and `NothingToApproveError` (API 409: the update validator refused, or the workflow has finished; a `status` query then tells a finished workflow from a missing one and names the earlier answer, if any) and `TicketWorkflowsUnavailableError` (API 503: Temporal or the worker did not answer in time, or a query failed in the worker; an update timeout is Temporal's `RPCTimeoutOrCancelledError`, not an `RPCError`, and since the update may still have been accepted, its message says to read the ticket before retrying).
  - `client.py`: `connect(settings)`, the Temporal client with the pydantic data converter, used by the API and the worker alike.
  - `worker.py`: `build_activities(settings)` builds the data store, agent and Helpdesk once per process. The Helpdesk's idempotency keys are in memory, so a retry is deduplicated only if it reaches the same process: run one worker, since Temporal may send retries to any worker and a restart forgets the keys. `run_worker` connects with the pydantic data converter and runs the workflow and activities on `TEMPORAL_TASK_QUEUE` until SIGTERM/SIGINT (`stop_on_signals`), then shuts down gracefully, giving running activities `GRACEFUL_SHUTDOWN` (8 s, under Docker's 10 s stop timeout). The handler matters in a container, where the worker is PID 1 and an unhandled SIGTERM is ignored. `configure_worker_logging` sets up JSON logs and stops the SDK from also appending its details to each message. Entry point: `uv run northdesk-worker`.
- `Dockerfile` / `docker-compose.yml` (repo root) — one single-stage image (uv, non-editable install, runs as non-root) runs both the API (`fastapi run`) and the worker (`northdesk-worker`). Compose adds Temporal's dev server (`temporalio/temporal`, UI on 8233, SQLite state on a volume) and points both at it with `TEMPORAL_ADDRESS=temporal:7233`; `.env` is optional. Keep one worker (see `worker.py`). Image tags are pinned; bump them deliberately.
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
uv run northdesk-worker                                   # Temporal worker (needs a server at TEMPORAL_ADDRESS)
docker compose up --build                                 # whole stack: Temporal (UI :8233), API (:8000), worker
uv run pytest                                             # all tests
uv run pytest tests/unit                                  # unit tests only
uv run pytest tests/integration                           # integration tests only
uv run pytest --cov                                       # with coverage (fails under 90%)
uv run pytest tests/unit/test_health.py::test_health_returns_ok   # single test
uv run ruff check . && uv run ruff format .               # lint / format
uv run mypy                                               # type check (strict)
uv run lint-imports                                       # package boundaries
uv run deptry src                                         # declared vs. imported deps
uv run pre-commit install --install-hooks                 # once: git hooks
uv run pre-commit run --all-files                         # all hooks by hand
uv run pre-commit run --hook-stage manual --all-files     # secret scan of the whole git history
```

The FastAPI entrypoint is configured in `pyproject.toml` as `northdesk_ticket_agent.main:app`.

Ruff: line length 120; rules `E, F, I, UP, B` plus `S` (bandit), `BLE`, `ASYNC`, `SIM`, `C4`, `PIE`, `RET`, `RUF`, `PT`, `TID` (relative imports are banned; import sorting is `I`, formatting is `ruff format`). Don't use `assert` for checks in `src/` (stripped under `-O`); use explicit guards. mypy runs in `strict` mode with the pydantic plugin over `src` and `tests`.

import-linter (`[tool.importlinter]` in `pyproject.toml`) enforces the package boundaries above: layers `main → api → orchestration → agent → services → domain`, `domain` imports no FastAPI/OpenAI/config, only `services/llm/openai_client.py` imports `openai` directly, and `temporalio` stays out of `agent`, `services` and `domain`.

Pre-commit (`.pre-commit-config.yaml`): file hygiene, no commits on `main`, gitleaks (secrets), typos, `uv lock --check`, ruff, mypy, import-linter, deptry (every third-party import in `src/` must be a declared dependency) on commit; pytest with coverage on push (branch coverage, `fail_under = 90` in `[tool.coverage.report]`). Keep coverage up when adding code instead of lowering the threshold. Hooks run through `uv run`, so tool versions come from `uv.lock`.

CI (`.github/workflows/ci.yml`): on every push, two parallel jobs. **Lint** runs `uv run pre-commit run --all-files` (skipping `no-commit-to-branch` and the staged-only `gitleaks` hook) and then the `gitleaks-history` hook; **Tests** runs `pytest --cov`. `.pre-commit-config.yaml` is the single list of checks: add a check there and CI picks it up.

Secrets: the `gitleaks` hook scans staged changes on commit; the `gitleaks-history` hook (same tool, `stages: [pre-push, manual]`) scans the whole git history on push and in CI. `pre-commit run --all-files` alone does not scan for secrets; run `uv run pre-commit run --hook-stage manual --all-files` for a full scan.

## Git

- Work on a separate branch per task, never directly on `main`. Branch from an up-to-date `main` and name it `<type>/<short-kebab-description>`, with type one of `feat`, `fix`, `refactor`, `test`, `docs`, `chore` (e.g. `feat/temporal-workflow`, `fix/refund-nan-check`).
- Merge into `main` only when the branch's CI run is green: all tests pass and branch coverage is at least 90% (`uv run pytest --cov`), along with ruff, mypy, lint-imports and deptry. Never merge a red or still-running branch, and never lower `fail_under` to get a merge through. Merge through a pull request, then delete the branch.
- Review before commit: finish the code (tests and all checks passing), leave it uncommitted for the user to review, and commit only after the user has reviewed it and says to.
- Make granular commits: one logical change per commit (e.g. dependencies/config, seed data, domain rules, a service adapter, the agent, docs), each with its own tests, not one big commit.
- Each commit should leave the repo working: tests, ruff and mypy pass at every commit.

## Layout and test conventions

- Source uses the `src/` layout (`src/northdesk_ticket_agent/`), built with `uv_build`.
- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — exercise the app over HTTP via `TestClient` and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area: `client.py` (`client`, a TestClient on `create_app(workflows)` with `workflows`, a `StubTicketWorkflows` held in memory), `data.py` (`datastore`, `example_tickets` keyed by ticket id), `helpdesk.py` (`helpdesk`, a `FakeHelpdesk` that never fails), `logs.py` (`json_logs`, a `LogLines` with every INFO+ line of the test parsed from JSON, with Temporal's loggers set up as in the worker; `restore_logging`, for a test that calls `configure_logging`), `llm.py` (`scripted_llm` factory replaying canned `LLMResponse`s; the `tool_call()` helper builds them), `temporal.py` (`workflow_env`, a session-wide time-skipping `WorkflowEnvironment` with the pydantic converter; `isolated_workflow_env`, a test server of its own for a test that leaves a query or update unanswered, which would keep time skipping off on the shared one; `temporal_workflows`, a `TemporalTicketWorkflows` on `workflow_env` with a worker running the real activities and the fake LLM; tests using them need `pytestmark = pytest.mark.asyncio(loop_scope="session")`). Fixtures go here, not in test modules, with names that don't clash. New fixture modules must be registered in `tests/conftest.py` via `pytest_plugins`.
- Async tests need no marker: `asyncio_mode = "auto"` (pytest-asyncio).
- Tests never need network or an API key; use the fake LLM or `scripted_llm`. Activities are tested with Temporal's `ActivityEnvironment`, which needs no server. The workflow is tested in `tests/integration/test_workflow.py` against the time-skipping test server, with fake activities registered under the real names, plus one run of every example ticket through the real activities. `tests/integration/test_ticket_workflows.py` runs `TemporalTicketWorkflows` and the API against the same server. A test on that shared server must not leave a workflow running when its worker stops: a pending task stops time skipping for every later test. The SDK downloads the test server binary once and caches it in the temp directory.
- No `__init__.py` files anywhere under `tests/`. Pytest runs with `--import-mode=importlib` and `pythonpath = ["."]`, so test directories have no package-name collisions and fixtures are imported as `tests.fixtures.<module>` (namespace packages); mypy uses `explicit_package_bases` with `mypy_path = ["src", "."]` for the same reason.
