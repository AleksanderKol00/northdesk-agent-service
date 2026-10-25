"""What the API needs from Temporal: start a ticket's workflow, read its state, answer its refund approval.

The workflow ID is the ticket ID, so a ticket sent twice finds the workflow already there instead of starting a second
one; the ID is never reused, even after the first workflow has finished.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from temporalio.client import (
    Client,
    RPCTimeoutOrCancelledError,
    WorkflowHandle,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from northdesk_ticket_agent.domain.models import Ticket
from northdesk_ticket_agent.orchestration.errors import (
    NothingToApproveError,
    TicketNotFoundError,
    TicketWorkflowsUnavailableError,
)
from northdesk_ticket_agent.orchestration.workflow import Approval, TicketState, TicketWorkflow

# How long a call waits for Temporal, and for the worker when it has to answer (a query, an update).
RPC_TIMEOUT = timedelta(seconds=10)


@dataclass(frozen=True)
class StartedTicket:
    workflow_id: str
    created: bool  # False when the ticket was sent before and its existing workflow is returned


class TicketWorkflows(Protocol):
    async def start(self, ticket: Ticket) -> StartedTicket: ...

    async def status(self, ticket_id: str) -> TicketState: ...

    async def approve(self, ticket_id: str, approval: Approval) -> TicketState: ...


class TemporalTicketWorkflows:
    """`TicketWorkflows` on a Temporal client. The workflow runs on a worker; this only talks to the server.

    Reading the state and approving are answered by the worker, so they wait for it, but only up to `rpc_timeout`:
    if Temporal or the worker does not answer in time, `TicketWorkflowsUnavailableError` is raised.
    """

    def __init__(self, client: Client, task_queue: str, rpc_timeout: timedelta = RPC_TIMEOUT) -> None:
        self._client = client
        self._task_queue = task_queue
        self._rpc_timeout = rpc_timeout

    async def start(self, ticket: Ticket) -> StartedTicket:
        try:
            handle = await self._client.start_workflow(
                TicketWorkflow.run,
                ticket,
                id=ticket.ticket_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=self._rpc_timeout,
            )
        except WorkflowAlreadyStartedError as err:
            return StartedTicket(workflow_id=err.workflow_id, created=False)
        except RPCError as err:
            raise TicketWorkflowsUnavailableError() from err
        return StartedTicket(workflow_id=handle.id, created=True)

    async def status(self, ticket_id: str) -> TicketState:
        try:
            return await self._handle(ticket_id).query(TicketWorkflow.status, rpc_timeout=self._rpc_timeout)
        except RPCError as err:
            raise _translate(err, ticket_id) from err
        except (WorkflowQueryFailedError, WorkflowQueryRejectedError) as err:  # the worker answered, but with an error
            raise TicketWorkflowsUnavailableError from err

    async def approve(self, ticket_id: str, approval: Approval) -> TicketState:
        handle = self._handle(ticket_id)
        try:
            return await handle.execute_update(TicketWorkflow.approve, approval, rpc_timeout=self._rpc_timeout)
        except WorkflowUpdateFailedError as err:  # refused by the validator: the workflow is not waiting
            raise NothingToApproveError(ticket_id) from err
        except RPCTimeoutOrCancelledError as err:  # an update times out with its own error, not an RPCError
            raise TicketWorkflowsUnavailableError from err
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise _translate(err, ticket_id) from err
            # Either there is no such workflow, or it has finished and takes no more updates.
            try:
                await handle.describe(rpc_timeout=self._rpc_timeout)
            except RPCError as describe_err:
                raise _translate(describe_err, ticket_id) from err
            raise NothingToApproveError(ticket_id) from err

    def _handle(self, ticket_id: str) -> WorkflowHandle[TicketWorkflow, TicketState]:
        return self._client.get_workflow_handle_for(TicketWorkflow.run, ticket_id)


def _translate(err: RPCError, ticket_id: str) -> Exception:
    if err.status == RPCStatusCode.NOT_FOUND:
        return TicketNotFoundError(ticket_id)
    return TicketWorkflowsUnavailableError()
