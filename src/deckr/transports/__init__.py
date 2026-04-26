"""First-class message transport implementations."""

from deckr.transports.bus import (
    EventBus,
    attach_event_handlers,
    event_handler,
)
from deckr.transports.routes import (
    EndpointRoute,
    RouteClient,
    RouteEvent,
    RouteTable,
)

__all__ = [
    "EndpointRoute",
    "EventBus",
    "RouteClient",
    "RouteEvent",
    "RouteTable",
    "attach_event_handlers",
    "event_handler",
]
