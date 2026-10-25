# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend service (FastAPI, Python 3.14, managed with `uv`) for Northdesk, a fictional software company whose support team receives tickets (password problems, billing questions, outages, spam). Each ticket is processed in several steps with the help of an AI agent, and the service must keep working when the AI or other downstream services fail temporarily — resilience to transient failures is a core design goal, not an afterthought.

The repo is currently an early scaffold: only `src/northdesk_ticket_agent/main.py` (the `app` with a `/health` endpoint) exists so far.

## Commands

```sh
uv sync                                                   # install deps (incl. dev group)
uv run fastapi dev src/northdesk_ticket_agent/main.py     # dev server with reload on :8000
uv run pytest                                             # all tests
uv run pytest tests/unit                                  # unit tests only
uv run pytest tests/integration                           # integration tests only
uv run pytest tests/unit/test_health.py::test_health_returns_ok   # single test
uv run ruff check . && uv run ruff format .               # lint / format
```

Ruff: line length 100, rules `E, F, I, UP, B`. The FastAPI entrypoint is configured in `pyproject.toml` as `northdesk_ticket_agent.main:app`.

## Layout and test conventions

- Source uses the `src/` layout (`src/northdesk_ticket_agent/`), built with `uv_build`.
- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — exercise the app over HTTP via `TestClient` and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area (e.g. `client.py` provides the `client` `TestClient` fixture). New fixture modules must be registered in `tests/conftest.py` via `pytest_plugins`.
- Pytest runs with `--import-mode=importlib` and `pythonpath = ["."]`, so test directories have no package-name collisions and fixtures are imported as `tests.fixtures.<module>`.
