from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from northdesk_ticket_agent.api import health, tickets
from northdesk_ticket_agent.api.access_log import add_access_log
from northdesk_ticket_agent.api.errors import add_error_handlers
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.logs import configure_logging
from northdesk_ticket_agent.orchestration.client import connect
from northdesk_ticket_agent.orchestration.tickets import TemporalTicketWorkflows, TicketWorkflows
from northdesk_ticket_agent.tracing import API_SERVICE, configure_tracing

# Docker's health check calls this every few seconds; a trace for each would bury the tickets'. Matched against the
# whole URL (scheme, host and path, no query), so anchored: `/tickets/health` is a ticket.
UNTRACED_URLS = r"^https?://[^/]+/health$"


def create_app(workflows: TicketWorkflows | None = None) -> FastAPI:
    """The API. Without `workflows`, it sets up JSON logging and tracing and connects to Temporal on startup, using
    the settings from the environment; with them (in tests), logging and tracing are left as they are."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if workflows is not None:
            app.state.workflows = workflows
            yield
            return
        settings = Settings()
        # Here rather than at import: uvicorn sets up its own logging first, and this replaces it.
        configure_logging(settings.log_level)
        stop_tracing = configure_tracing(API_SERVICE)
        try:
            app.state.workflows = TemporalTicketWorkflows(await connect(settings), settings.temporal_task_queue)
            yield
        finally:
            stop_tracing()  # sends the spans still waiting in the batch

    app = FastAPI(
        title="Northdesk Ticket Agent",
        description=(
            "Triages Northdesk support tickets: an LLM agent proposes an action, code checks it, "
            "and a Temporal workflow carries it out, retrying when a service fails for a short time."
        ),
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(tickets.router)
    add_error_handlers(app)
    add_access_log(app)
    # Outermost, so the access line and everything the request logs carry its trace ID. The spans for each ASGI
    # message the request reads or sends add nothing here. Until a tracer provider is set, the spans are no-ops.
    FastAPIInstrumentor.instrument_app(app, excluded_urls=UNTRACED_URLS, exclude_spans=["receive", "send"])
    return app


app = create_app()
