from northdesk_ticket_agent.api.health import health


def test_health_returns_ok() -> None:
    assert health() == {"status": "ok"}
