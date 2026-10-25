"""How the errors from `TicketWorkflows` reach the client: as HTTP statuses with the error message as `detail`."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from northdesk_ticket_agent.orchestration.errors import (
    NothingToApproveError,
    TicketNotFoundError,
    TicketWorkflowsUnavailableError,
)

logger = logging.getLogger(__name__)

STATUS_FOR: dict[type[Exception], int] = {
    TicketNotFoundError: status.HTTP_404_NOT_FOUND,
    NothingToApproveError: status.HTTP_409_CONFLICT,
    TicketWorkflowsUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def add_error_handlers(app: FastAPI) -> None:
    for error in STATUS_FOR:
        app.add_exception_handler(error, _error_response)


async def _error_response(request: Request, exc: Exception) -> JSONResponse:
    status_code = STATUS_FOR[type(exc)]
    # A 503 hides a Temporal error the client never sees: log it, with its cause, for whoever runs the service.
    logger.warning("%s", exc, extra={"error": type(exc).__name__}, exc_info=exc if status_code >= 500 else None)
    return JSONResponse({"detail": str(exc)}, status_code=status_code)
