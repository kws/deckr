"""Plugin host protocol: HostMessage and message type constants."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote


def _new_message_id() -> str:
    return str(uuid.uuid4())


def controller_address(controller_id: str) -> str:
    """Canonical logical address for a controller endpoint."""
    return f"controller:{controller_id}"


def host_address(host_id: str) -> str:
    """Canonical logical address for a plugin host endpoint."""
    return f"host:{host_id}"


def parse_controller_address(address: str) -> str | None:
    """Return controller_id when address is a controller endpoint."""
    if address == "controller":
        return ""
    if address.startswith("controller:"):
        return address.split(":", 1)[1]
    return None


def parse_host_address(address: str) -> str | None:
    """Return host_id when address is a host endpoint."""
    if address.startswith("host:"):
        return address.split(":", 1)[1]
    return None


def _encode_context_value(value: str) -> str:
    return quote(value, safe="")


def _decode_context_value(value: str) -> str:
    return unquote(value)


def build_context_id(controller_id: str, device_id: str, slot_id: str) -> str:
    """Canonical controller-scoped context ID."""
    return "|".join(
        [
            f"controller={_encode_context_value(controller_id)}",
            f"device={_encode_context_value(device_id)}",
            f"slot={_encode_context_value(slot_id)}",
        ]
    )


def parse_context_id(context_id: str) -> dict[str, str | None]:
    """Parse controller-scoped or legacy context IDs."""
    if context_id.startswith("controller="):
        parts: dict[str, str | None] = {
            "controller_id": None,
            "device_id": None,
            "slot_id": None,
        }
        for item in context_id.split("|"):
            key, sep, value = item.partition("=")
            if not sep:
                continue
            decoded = _decode_context_value(value)
            if key == "controller":
                parts["controller_id"] = decoded
            elif key == "device":
                parts["device_id"] = decoded
            elif key == "slot":
                parts["slot_id"] = decoded
        return parts
    if "." in context_id:
        device_id, slot_id = context_id.split(".", 1)
        return {
            "controller_id": None,
            "device_id": device_id,
            "slot_id": slot_id,
        }
    return {
        "controller_id": None,
        "device_id": context_id or None,
        "slot_id": None,
    }


@dataclass(frozen=True)
class HostMessage:
    """Message envelope for plugin host protocol. All messages carry from/to for routing."""

    from_id: str
    to_id: str
    type: str
    payload: dict[str, Any]
    message_id: str = field(default_factory=_new_message_id)
    in_reply_to: str | None = None
    internal_metadata: dict[str, Any] | None = None  # In-memory only; not serialized

    def for_host(self, host_id: str) -> bool:
        """True if this message is intended for the given host."""
        return self.to_id in {host_address(host_id), host_id, "all_hosts"}

    def for_controller(self, controller_id: str | None = None) -> bool:
        """True if this message is intended for the given controller or all controllers."""
        if self.to_id == "all_controllers":
            return True
        if controller_id is None:
            return self.to_id == "controller" or self.to_id.startswith("controller:")
        return self.to_id in {controller_address(controller_id), "controller"}

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON (e.g. MQTT, WebSocket)."""
        payload = {
            "messageId": self.message_id,
            "from": self.from_id,
            "to": self.to_id,
            "type": self.type,
            "payload": self.payload,
        }
        if self.in_reply_to is not None:
            payload["inReplyTo"] = self.in_reply_to
        return payload

    @classmethod
    def from_dict(
        cls, d: dict[str, Any], *, internal_metadata: dict[str, Any] | None = None
    ) -> HostMessage:
        """Deserialize from JSON. internal_metadata is only set via kwarg when receiver knows source."""
        return cls(
            message_id=d.get("messageId", _new_message_id()),
            from_id=d["from"],
            to_id=d["to"],
            type=d["type"],
            payload=d["payload"],
            in_reply_to=d.get("inReplyTo"),
            internal_metadata=internal_metadata,
        )


# Message type constants
HOST_READY = "hostReady"  # Legacy alias for actionsRegistered
ACTIONS_REGISTERED = "actionsRegistered"
REQUEST_ACTIONS = "requestActions"
ALL_HOSTS = "all_hosts"
ALL_CONTROLLERS = "all_controllers"
ACTIONS_UNREGISTERED = "actionsUnregistered"
HOST_ONLINE = "hostOnline"
HOST_OFFLINE = "hostOffline"
CONTROLLER_CAPABILITIES = "controllerCapabilities"
WILL_APPEAR = "willAppear"
WILL_DISAPPEAR = "willDisappear"
KEY_UP = "keyUp"
KEY_DOWN = "keyDown"
DIAL_ROTATE = "dialRotate"
TOUCH_TAP = "touchTap"
TOUCH_SWIPE = "touchSwipe"
PAGE_APPEAR = "pageAppear"
PAGE_DISAPPEAR = "pageDisappear"
SET_TITLE = "setTitle"
SET_IMAGE = "setImage"
SET_KEY_IMAGE = "setKeyImage"
SET_STATE = "setState"
SHOW_ALERT = "showAlert"
SHOW_OK = "showOk"
REQUEST_SETTINGS = "requestSettings"
HERE_ARE_SETTINGS = "hereAreSettings"
SET_SETTINGS = "setSettings"
SET_PAGE = "setPage"
OPEN_PAGE = "openPage"
CLOSE_PAGE = "closePage"
OPEN_URL = "openUrl"
SLEEP_SCREEN = "sleepScreen"
WAKE_SCREEN = "wakeScreen"


@dataclass(frozen=True)
class ActionsChangedEvent:
    """Emitted by ActionRegistry when actions are registered/unregistered."""

    registered: list[str]  # action UUIDs now available
    unregistered: list[str]  # action UUIDs no longer available


def extract_device_id(context_id: str) -> str:
    """Extract device_id from contextId."""
    return parse_context_id(context_id).get("device_id") or ""


def extract_slot_id(context_id: str) -> str:
    """Extract slot_id from contextId."""
    parsed = parse_context_id(context_id)
    slot_id = parsed.get("slot_id")
    if slot_id:
        return slot_id
    if "." in context_id:
        return context_id.split(".", 1)[1]
    return context_id


def extract_controller_id(context_id: str) -> str | None:
    """Extract controller_id from contextId when present."""
    return parse_context_id(context_id).get("controller_id")


# Types that are commands/requests from host to controller (need contextId routing)
COMMAND_MESSAGE_TYPES = frozenset(
    {
        SET_TITLE,
        SET_IMAGE,
        SET_KEY_IMAGE,
        SET_STATE,
        SHOW_ALERT,
        SHOW_OK,
        REQUEST_SETTINGS,
        SET_SETTINGS,
        SET_PAGE,
        OPEN_PAGE,
        CLOSE_PAGE,
        OPEN_URL,
        SLEEP_SCREEN,
        WAKE_SCREEN,
    }
)
