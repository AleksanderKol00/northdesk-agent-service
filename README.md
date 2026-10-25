# Northdesk Ticket Agent

Backend service for Northdesk, a small (fictional) software company whose support team
receives many tickets every day: password problems, billing questions, outages, and some spam.

The service processes each ticket in several steps with the help of an AI agent, and is designed
to keep working when the AI or other downstream services fail temporarily.

## Development

```sh
uv sync                                                   # install deps
uv run fastapi dev src/northdesk_ticket_agent/main.py     # run with reload on :8000
uv run pytest                                             # all tests
uv run pytest tests/unit                                  # unit tests only
uv run pytest tests/integration                           # integration tests only
uv run ruff check . && uv run ruff format .               # lint / format
```

## Tests

- `tests/unit/` — fast, isolated tests of single functions/classes; no I/O or app startup.
- `tests/integration/` — tests that exercise the app through HTTP (`TestClient`) and real wiring.
- `tests/fixtures/` — shared pytest fixtures, one module per area, registered in `tests/conftest.py` via `pytest_plugins`.
