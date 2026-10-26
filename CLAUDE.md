# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend service (FastAPI, Python 3.14, managed with `uv`) that triages support tickets for Northdesk, a fictional software company. An LLM agent looks up the customer, their invoices and the knowledge base through tool calls and proposes what to do; code checks the proposal, refunds wait for a person, and a Temporal workflow carries each ticket from decision to action. The LLM and the Helpdesk can both fail transiently, so retries, idempotent actions and escalation with a clear reason are part of the design, not add-ons.

Built so far: the decision agent, the decision checks, the fake Helpdesk client, the Temporal ticket workflow, activities and worker, the ticket API, structured (JSON) logs, OpenTelemetry traces and the README. Docker Compose runs Temporal, the API, the
worker and Jaeger.

## Architecture

Packages under `src/northdesk_ticket_agent/`, kept separate on purpose:

- `main.py` — `create_app(workflows=None)`; `app = create_app()` is the entry point. Without `workflows` the lifespan sets up JSON logging and connects to Temporal; tests pass a stub and logging is left alone.
- `logs.py` — JSON lines. Fields come from Temporal's logger details (`workflow_id`, copied as `ticket_id` too: every workflow here is a ticket's), from `log_context(**fields)` / `bind_log_context` (a contextvar), and from `extra`. `configure_logging` routes every logger, uvicorn's included, through one handler and keeps `LIBRARY_LOGGERS` at INFO under `DEBUG`, since the SDK logs activity results (the model's output) at DEBUG. Every line about a ticket carries `ticket_id` and `workflow_id`. Log IDs, types and counts, never ticket text, replies, a reviewer's comment, or anything the model wrote (tool arguments, an unchecked decision, rule violations, `str(ValidationError)`). Outside the layers: imports nothing from the package and no Temporal.
- `tracing.py` — OpenTelemetry. `configure_tracing(service)` sets an OTLP/HTTP exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` (or `..._TRACES_ENDPOINT`) is set and `OTEL_SDK_DISABLED` isn't (else spans are no-ops). The `OTEL_*` variables are the SDK's, read from the process environment: not in `Settings`, not from `.env` (`uv run --env-file .env`). It returns a stop function that flushes for at most `FLUSH_TIMEOUT` (1 s), so a dead collector can't push the worker past Docker's 10 s stop. FastAPI's instrumentation is applied in `create_app` (not `/health`; `UNTRACED_URLS` matches the whole URL, so it is anchored); `connect()` adds Temporal's `TracingInterceptor`, which carries the trace from the API's start into the workflow and activities (workflow spans are zero-length by design). The agent adds `chat` / `execute_tool <name>` spans (a name `ToolExecutor.knows` doesn't run becomes `unknown`, in the log line too); a failed one records only the error's type, not its message or stack trace. Same rule as logs: span attributes are IDs, types and counts. Log lines inside a span carry `trace_id` / `span_id`.
- `api/` — the HTTP layer. `tickets.py`: `POST /tickets` (202, or 200 with `created: false` for a ticket sent before), `GET /tickets/{id}`, `POST /tickets/{id}/approval`. `schemas.py`: the API's own response models; `TicketStatus.from_state` maps the workflow's `TicketState`, so the workflow's state can change without changing the API. `access_log.py`: one line per request, inside a `log_context`; also logs unhandled errors. `dependencies.py`: `Workflows` and `TicketId` (binds the ID to the log context; use it on every `{ticket_id}` route). `errors.py`: maps the orchestration errors to 404/409/503, so endpoints don't catch them.
- `domain/` — pure business code with no dependency on FastAPI, Temporal or the LLM provider.
  - `models.py`: `Ticket` (`ticket_id` is URL-safe and never `.`/`..`: it is also the workflow ID, a path segment and part of the idempotency key; subject and body are capped, since they go to the LLM), `Customer`, `Invoice`, `KbArticle`; `Money` (`Decimal`, whole cents, a string in JSON); `ProposedDecision` (loose, raw model output: plain strings, float amount) and `Decision` (checked: `StrEnum`s + an action union of reply/refund/escalate/close).
  - `rules.py`: `check_decision()`. Each `_check_*` returns its action or the rules it breaks, and every broken rule is listed in the escalation that replaces the decision. Refunds: a `paid` invoice of the ticket's customer, an amount > 0 in whole cents and ≤ the invoice. Closes: only for category `other`, and never for a ticket whose own words match `_MONEY_WORDS`.
- `agent/` — `TicketAgent.decide(ticket) -> ProposedDecision`: a tool-calling loop that ends when the model calls `submit_decision`. Unreadable decisions go back to the model up to `max_decision_attempts`; the loop is capped at `max_steps`. Errors in `agent/errors.py` are all permanent; a `DecisionParseError` names the wrong fields, never their values.
- `services/` — outside dependencies.
  - `errors.py`: every service error derives from `ServiceError`; temporary ones also from `TemporaryServiceError`, so retry decisions are one `isinstance` check.
  - `datastore.py`: read-only JSON seed data (`data/*.json`, package data) with keyword-ranked `search_kb`. `data/tickets.json` holds the README's example tickets, loaded by the `example_tickets` fixture.
  - `llm/base.py` (types, `LLMClient` protocol, errors), `llm/openai_client.py` (SDK retries off: one retry policy, the caller's; only connection errors, 408/409/429 and 5xx are temporary; error messages name the status, OpenAI's error code and request ID, never OpenAI's message, which may repeat the request), `llm/fake_client.py` (deterministic and deliberately gullible, so the rules are what block bad decisions), `llm/factory.py` (by `LLM_MODE`).
  - `helpdesk.py`: the `Helpdesk` protocol, `HelpdeskReceipt` and errors. A repeated idempotency key returns the first receipt; a key reused for a different request is a `HelpdeskConflictError`. `fake_helpdesk.py`: `FakeHelpdesk`, in memory, failing at random (`HELPDESK_FAILURE_RATE`), half the time after acting (a lost response).
- `orchestration/` — Temporal.
  - `models.py`: what the workflow, activities and callers exchange (activity names, `CheckInput`, `ActInput`, `Step`, `Approval`, `TicketState`). It goes through Temporal history, so changes must stay readable by running workflows.
  - `activities.py`: `TicketActivities.decide` / `check` / `act`, each in a `log_context` with the ticket's IDs. Known errors become an `ApplicationError` typed with the original class name, retryable only for a `TemporaryServiceError`, raised `from None`: Temporal stores (and the SDK logs) the whole cause chain with its messages, and what an error was raised from may quote the model or the ticket. So a service or agent error's own message must be safe. `act` uses idempotency key `<ticket_id>:<action type>`.
  - `workflow.py`: `TicketWorkflow`: decide → check → approval (refunds only) → act. It is the only place that guarantees `check` runs before `act`. A failure before acting becomes an escalation with the reason; a rejected or unanswered refund too. If `act` fails for good the workflow fails (`ActionFailed`), saying whether the action may still have happened. Query `status`, update `approve` (validator refuses unless a refund is waiting; returns once the workflow moved on). Gotchas: `@workflow.update` stays bare (with `name=` the SDK's types lose the async result); imports go through `imports_passed_through()`; log only through `workflow.logger`.
  - `tickets.py`: the `TicketWorkflows` protocol the API uses, implemented by `TemporalTicketWorkflows`. Workflow ID = ticket ID with `REJECT_DUPLICATE`, so a ticket is processed once (within the namespace's retention). Every call has an `rpc_timeout`, since queries and updates wait for a worker. An update timeout is `RPCTimeoutOrCancelledError`, not an `RPCError`, and may still have been accepted.
  - `errors.py`: `TicketNotFoundError` (404), `NothingToApproveError` (409), `TicketWorkflowsUnavailableError` (503).
  - `client.py`: `connect(settings)` with the pydantic data converter; its `Runtime` forwards Temporal Core's logs to Python logging.
  - `worker.py`: builds the activities once per process and runs until SIGTERM/SIGINT (handled explicitly: the worker is PID 1 in a container). Run one worker: the fake Helpdesk's idempotency keys are in memory. Entry point: `uv run northdesk-worker`.
- `Dockerfile` / `docker-compose.yml` — one image runs the API and the worker; Compose adds Temporal's dev server (UI on 8233) and Jaeger (UI on 16686, OTLP/HTTP on 4318). Keep one worker. Image tags are pinned; bump them deliberately.
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
- `tests/fixtures/` — shared pytest fixtures, one module per area: `client.py` (`client`, a TestClient on `create_app(workflows)` with `workflows`, a `StubTicketWorkflows` held in memory), `data.py` (`datastore`; `example_tickets`, from `data/tickets.json`, keyed by ticket id), `helpdesk.py` (`helpdesk`, a `FakeHelpdesk` that never fails), `logs.py` (`json_logs`, a `LogLines` with every INFO+ line of the test parsed from JSON, with Temporal's loggers set up as in the worker; `restore_logging`, for a test that calls `configure_logging`), `llm.py` (`scripted_llm` factory replaying canned `LLMResponse`s; the `tool_call()` helper builds them), `temporal.py` (`workflow_env`, a session-wide time-skipping `WorkflowEnvironment` with the pydantic converter; `isolated_workflow_env`, a test server of its own for a test that leaves a query or update unanswered, which would keep time skipping off on the shared one; `temporal_workflows`, a `TemporalTicketWorkflows` on `workflow_env` with a worker running the real activities and the fake LLM, its client tracing like `connect`'s; tests using them need `pytestmark = pytest.mark.asyncio(loop_scope="session")`), `tracing.py` (`spans`, a `Spans` with every span finished during the test; the first use sets a global in-memory tracer provider for the rest of the session). Fixtures go here, not in test modules, with names that don't clash. New fixture modules must be registered in `tests/conftest.py` via `pytest_plugins`.
- Async tests need no marker: `asyncio_mode = "auto"` (pytest-asyncio).
- Tests never need network or an API key; use the fake LLM or `scripted_llm`. Activities are tested with Temporal's `ActivityEnvironment`, which needs no server. The workflow is tested in `tests/integration/test_workflow.py` against the time-skipping test server, with fake activities registered under the real names, plus one run of every example ticket through the real activities. `tests/integration/test_ticket_workflows.py` runs `TemporalTicketWorkflows` and the API against the same server. A test on that shared server must not leave a workflow running when its worker stops: a pending task stops time skipping for every later test. The SDK downloads the test server binary once and caches it in the temp directory; `TEMPORAL_TEST_SERVER_PATH` (read by `start_test_server` in `tests/fixtures/temporal.py`) points at a binary on disk instead, for runs with no network.
- No `__init__.py` files anywhere under `tests/`. Pytest runs with `--import-mode=importlib` and `pythonpath = ["."]`, so test directories have no package-name collisions and fixtures are imported as `tests.fixtures.<module>` (namespace packages); mypy uses `explicit_package_bases` with `mypy_path = ["src", "."]` for the same reason.
