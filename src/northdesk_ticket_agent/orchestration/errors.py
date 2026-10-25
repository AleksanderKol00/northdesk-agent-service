class TicketNotFoundError(Exception):
    """No workflow was ever started for this ticket ID."""

    def __init__(self, ticket_id: str) -> None:
        super().__init__(f"ticket {ticket_id} not found")


class NothingToApproveError(Exception):
    """The ticket exists, but no refund is waiting for approval: it is at another step, or already finished.

    `approved` is the answer the refund already got, if any, so a client that sends its answer again (say, after a
    timeout) learns that the first one counted.
    """

    def __init__(self, ticket_id: str, approved: bool | None = None) -> None:
        if approved is None:
            message = f"ticket {ticket_id} has no refund waiting for approval"
        else:
            message = f"the refund for ticket {ticket_id} was already {'approved' if approved else 'rejected'}"
        super().__init__(message)
        self.approved = approved


class TicketWorkflowsUnavailableError(Exception):
    """Temporal, or the worker that answers for the workflow, did not respond in time. Worth trying again later."""

    def __init__(self, message: str = "tickets cannot be processed right now; try again later") -> None:
        super().__init__(message)
