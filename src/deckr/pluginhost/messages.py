"""Body contracts for the core ``plugin_messages`` lane."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote

from pydantic import field_serializer, field_validator

from deckr.contracts.messages import (
    PLUGIN_MESSAGES_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    EntitySubject,
    MessageTarget,
    controller_address,
    endpoint_target,
    entity_subject,
    host_address,
    message_targets_endpoint,
)
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json


def _encode_context_value(value: str) -> str:
    return quote(value, safe="")


def _decode_context_value(value: str) -> str:
    return unquote(value)


def build_context_id(controller_id: str, config_id: str, slot_id: str) -> str:
    """Canonical controller-scoped context ID."""
    return "|".join(
        [
            f"controller={_encode_context_value(controller_id)}",
            f"config={_encode_context_value(config_id)}",
            f"slot={_encode_context_value(slot_id)}",
        ]
    )


def _parse_context_id(context_id: str) -> dict[str, str | None]:
    """Parse canonical controller-scoped context IDs."""
    parts: dict[str, str | None] = {
        "controller_id": None,
        "config_id": None,
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
        elif key == "config":
            parts["config_id"] = decoded
        elif key == "slot":
            parts["slot_id"] = decoded
        else:
            raise ValueError(f"Invalid contextId {context_id!r}")
    if None in parts.values():
        raise ValueError(f"Invalid contextId {context_id!r}")
    return parts


class PluginMessageBody(DeckrModel):
    """Generic Phase 1 body for plugin lane messages."""

    payload: Mapping[str, Any]

    @field_validator("payload", mode="after")
    @classmethod
    def _freeze_payload(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("payload")
    def _serialize_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def _target(
    recipient: str | EndpointAddress | MessageTarget,
) -> MessageTarget:
    if isinstance(recipient, EndpointTarget | BroadcastTarget):
        return recipient
    return endpoint_target(recipient)


def plugin_message(
    *,
    sender: str | EndpointAddress,
    recipient: str | EndpointAddress | MessageTarget,
    message_type: str,
    payload: Mapping[str, Any],
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    body = PluginMessageBody(payload=payload)
    return DeckrMessage(
        lane=PLUGIN_MESSAGES_LANE,
        messageType=message_type,
        sender=sender,
        recipient=_target(recipient),
        subject=subject,
        body=body.to_dict(),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def plugin_payload(message: DeckrMessage) -> Mapping[str, Any]:
    body = PluginMessageBody.model_validate(message.body)
    return body.payload


def plugin_message_for_host(message: DeckrMessage, host_id: str) -> bool:
    return message_targets_endpoint(message, host_address(host_id))


def plugin_message_for_controller(
    message: DeckrMessage,
    controller_id: str | None = None,
) -> bool:
    if controller_id is None:
        return message.recipient.endpoint_family == "controller" if isinstance(
            message.recipient, BroadcastTarget
        ) else message.recipient.endpoint.family == "controller"
    return message_targets_endpoint(message, controller_address(controller_id))


def context_subject(context_id: str) -> EntitySubject:
    identifiers: dict[str, str] = {"contextId": context_id}
    try:
        parsed = _parse_context_id(context_id)
    except ValueError:
        parsed = {}
    controller_id = parsed.get("controller_id")
    config_id = parsed.get("config_id")
    slot_id = parsed.get("slot_id")
    if controller_id is not None:
        identifiers["controllerId"] = controller_id
    if config_id is not None:
        identifiers["configId"] = config_id
    if slot_id is not None:
        identifiers["slotId"] = slot_id
    return entity_subject("context", **identifiers)


def subject_context_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("contextId")
    return str(value) if value is not None else None


def subject_controller_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("controllerId")
    return str(value) if value is not None else None


def subject_config_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("configId")
    return str(value) if value is not None else None


def subject_slot_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("slotId")
    return str(value) if value is not None else None


def plugin_host_subject(host_id: str) -> EntitySubject:
    return entity_subject("plugin_host", hostId=host_id)


def plugin_actions_subject(host_id: str | None = None) -> EntitySubject:
    if host_id is None:
        return entity_subject("plugin_actions")
    return entity_subject("plugin_actions", hostId=host_id)


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
