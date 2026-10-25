from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from northdesk_ticket_agent.api import health, tickets
from northdesk_ticket_agent.api.errors import add_error_handlers
from northdesk_ticket_agent.config import Settings
from northdesk_ticket_agent.orchestration.client import connect
from northdesk_ticket_agent.orchestration.tickets import TemporalTicketWorkflows, TicketWorkflows


def create_app(workflows: TicketWorkflows | None = None) -> FastAPI:
    """The API. Without `workflows`, it connects to Temporal on startup, using the settings from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if workflows is None:
            settings = Settings()
            app.state.workflows = TemporalTicketWorkflows(await connect(settings), settings.temporal_task_queue)
        else:
            app.state.workflows = workflows
        yield

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
    return app


app = create_app()
