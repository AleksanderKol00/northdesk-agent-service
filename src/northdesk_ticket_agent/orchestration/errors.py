class TicketNotFoundError(Exception):
    """No workflow was ever started for this ticket ID."""

    def __init__(self, ticket_id: str) -> None:
        super().__init__(f"ticket {ticket_id} not found")


class NothingToApproveError(Exception):
    """The ticket exists, but no refund is waiting for approval: it is at another step, or already finished."""

    def __init__(self, ticket_id: str) -> None:
        super().__init__(f"ticket {ticket_id} has no refund waiting for approval")


class TicketWorkflowsUnavailableError(Exception):
    """Temporal, or the worker that answers for the workflow, did not respond in time. Worth trying again later."""

    def __init__(self) -> None:
        super().__init__("tickets cannot be processed right now; try again later")
