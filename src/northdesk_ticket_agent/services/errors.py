"""Errors shared by the outside services (LLM provider, Helpdesk).

Callers decide whether to retry with one check, ``isinstance(exc, TemporaryServiceError)``, whichever service failed.
"""


class ServiceError(Exception):
    """A call to an outside service failed. Not worth retrying unless it is also a TemporaryServiceError."""


class TemporaryServiceError(ServiceError):
    """The service failed for a moment; the same call may succeed if retried."""
