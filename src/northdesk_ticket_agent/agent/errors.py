class AgentError(Exception):
    """Base class for errors raised while deciding on a ticket. None are worth retrying."""


class UnknownCustomerError(AgentError):
    """The ticket names a customer we have no record of."""


class DecisionParseError(AgentError):
    """The model did not return a readable decision after several attempts."""


class AgentStepLimitError(AgentError):
    """The model kept calling tools without ever returning a decision."""
