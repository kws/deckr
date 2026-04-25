"""First-class message transport implementations."""

from deckr.transports.bus import (
    TRANSPORT_ID_HEADER,
    TRANSPORT_KIND_HEADER,
    TRANSPORT_LANE_HEADER,
    TRANSPORT_REMOTE_ID_HEADER,
    EventBus,
    TransportEnvelope,
    attach_event_handlers,
    event_handler,
)

__all__ = [
    "EventBus",
    "TRANSPORT_ID_HEADER",
    "TRANSPORT_KIND_HEADER",
    "TRANSPORT_LANE_HEADER",
    "TRANSPORT_REMOTE_ID_HEADER",
    "TransportEnvelope",
    "attach_event_handlers",
    "event_handler",
]
