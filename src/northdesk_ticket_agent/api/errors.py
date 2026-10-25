"""How the errors from `TicketWorkflows` reach the client: as HTTP statuses with the error message as `detail`."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from northdesk_ticket_agent.orchestration.errors import (
    NothingToApproveError,
    TicketNotFoundError,
    TicketWorkflowsUnavailableError,
)

STATUS_FOR: dict[type[Exception], int] = {
    TicketNotFoundError: status.HTTP_404_NOT_FOUND,
    NothingToApproveError: status.HTTP_409_CONFLICT,
    TicketWorkflowsUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def add_error_handlers(app: FastAPI) -> None:
    for error in STATUS_FOR:
        app.add_exception_handler(error, _error_response)


async def _error_response(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=STATUS_FOR[type(exc)])
