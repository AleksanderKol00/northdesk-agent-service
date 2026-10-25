"""One log line per request, with the ticket's IDs when the request is about a ticket.

It replaces uvicorn's access log, which only knows the path. Each request runs in a `log_context` of its own; the
endpoints bind the ticket's IDs to it, so this line and every line logged while handling the request carry them.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, status

from northdesk_ticket_agent.logs import log_context

logger = logging.getLogger(__name__)

# Docker's health check calls this every few seconds: logged at DEBUG, so it doesn't drown out the tickets.
QUIET_PATHS = frozenset({"/health"})


def add_access_log(app: FastAPI) -> None:
    app.middleware("http")(_log_request)


async def _log_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    started = time.perf_counter()
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR  # what the client gets if the app raises
    with log_context():
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            logger.log(
                logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO,
                "%s %s %d",
                request.method,
                request.url.path,
                status_code,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
