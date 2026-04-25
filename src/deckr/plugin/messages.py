"""Plugin host protocol: HostMessage and message type constants."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from pydantic import Field, field_serializer, field_validator

from deckr.contracts.models import DeckrModel, freeze_json, thaw_json


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
    if address.startswith("controller:"):
        controller_id = address.split(":", 1)[1]
        return controller_id or None
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
    """Parse canonical controller-scoped context IDs."""
    parts: dict[str, str | None] = {
        "controller_id": None,
        "device_id": None,
        "slot_id": None,
    }
    for item in context_id.split("|"):
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"Invalid contextId {context_id!r}")
        decoded = _decode_context_value(value)
        if not decoded:
            raise ValueError(f"Invalid contextId {context_id!r}")
        if key == "controller":
            parts["controller_id"] = decoded
        elif key == "device":
            parts["device_id"] = decoded
        elif key == "slot":
            parts["slot_id"] = decoded
        else:
            raise ValueError(f"Invalid contextId {context_id!r}")
    if None in parts.values():
        raise ValueError(f"Invalid contextId {context_id!r}")
    return parts


class HostMessage(DeckrModel):
    """Message envelope for plugin host protocol. All messages carry from/to for routing."""

    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    type: str
    payload: Mapping[str, Any]
    message_id: str = Field(default_factory=_new_message_id, alias="messageId")
    in_reply_to: str | None = Field(default=None, alias="inReplyTo")

    @field_validator("payload", mode="after")
    @classmethod
    def _freeze_payload(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("payload")
    def _serialize_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def for_host(self, host_id: str) -> bool:
        """True if this message is intended for the given host."""
        return self.to_id in {host_address(host_id), "all_hosts"}

    def for_controller(self, controller_id: str | None = None) -> bool:
        """True if this message is intended for the given controller or all controllers."""
        if self.to_id == "all_controllers":
            return True
        if controller_id is None:
            return parse_controller_address(self.to_id) is not None
        return self.to_id == controller_address(controller_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON (e.g. MQTT, WebSocket)."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HostMessage:
        """Deserialize from JSON."""
        data = dict(d)
        if "messageId" not in data:
            raise ValueError("messageId is required")
        return cls.model_validate(data)

    @classmethod
    def schema_dict(cls) -> dict[str, Any]:
        return cls.model_json_schema(by_alias=True)


class ActionDescriptor(DeckrModel):
    """Action identity advertised by a plugin host."""

    uuid: str
    name: str | None = None
    plugin_uuid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for action registration payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class TitleOptions(DeckrModel):
    """Font and styling options for controller-rendered titles."""

    font_family: str | None = None
    font_size: int | str | None = None
    font_style: str | None = None
    title_color: str | None = None
    title_alignment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class SlotBinding(DeckrModel):
    """One slot bound to an action for static or dynamic pages."""

    slot_id: str
    action_uuid: str
    settings: Mapping[str, Any]
    title_options: TitleOptions | None = None

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class DynamicPageDescriptor(DeckrModel):
    """Plugin-generated page descriptor carried by openPage commands."""

    page_id: str
    slots: tuple[SlotBinding, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def make_dynamic_page_id() -> str:
    """Generate a unique page ID for dynamic pages."""
    return str(uuid.uuid4())


# Message type constants
ACTIONS_REGISTERED = "actionsRegistered"
REQUEST_ACTIONS = "requestActions"
ALL_HOSTS = "all_hosts"
ALL_CONTROLLERS = "all_controllers"
ACTIONS_UNREGISTERED = "actionsUnregistered"
HOST_ONLINE = "hostOnline"
HOST_OFFLINE = "hostOffline"
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
SHOW_ALERT = "showAlert"
SHOW_OK = "showOk"
REQUEST_SETTINGS = "requestSettings"
HERE_ARE_SETTINGS = "hereAreSettings"
SET_SETTINGS = "setSettings"
SET_PAGE = "setPage"
OPEN_PAGE = "openPage"
CLOSE_PAGE = "closePage"
SLEEP_SCREEN = "sleepScreen"
WAKE_SCREEN = "wakeScreen"


@dataclass(frozen=True)
class ActionsChangedEvent:
    """Emitted by ActionRegistry when actions are registered/unregistered."""

    registered: list[str]  # action UUIDs now available
    unregistered: list[str]  # action UUIDs no longer available


def extract_device_id(context_id: str) -> str:
    """Extract device_id from contextId."""
    return parse_context_id(context_id)["device_id"] or ""


def extract_slot_id(context_id: str) -> str:
    """Extract slot_id from contextId."""
    return parse_context_id(context_id)["slot_id"] or ""


def extract_controller_id(context_id: str) -> str | None:
    """Extract controller_id from contextId when present."""
    return parse_context_id(context_id)["controller_id"]


# Host -> controller commands a controller-lite should implement.
CORE_COMMAND_MESSAGE_TYPES = frozenset(
    {
        SET_TITLE,
        SET_IMAGE,
        SHOW_ALERT,
        SHOW_OK,
        REQUEST_SETTINGS,
        SET_SETTINGS,
    }
)

# Deckr-specific controller extensions beyond the core command set.
DECKR_EXTENSION_COMMAND_MESSAGE_TYPES = frozenset(
    {
        SET_PAGE,
        OPEN_PAGE,
        CLOSE_PAGE,
        SLEEP_SCREEN,
        WAKE_SCREEN,
    }
)

# Types that are commands/requests from host to controller (need contextId routing)
COMMAND_MESSAGE_TYPES = (
    CORE_COMMAND_MESSAGE_TYPES | DECKR_EXTENSION_COMMAND_MESSAGE_TYPES
)
