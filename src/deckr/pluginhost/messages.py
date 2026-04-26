"""Body contracts for the core ``plugin_messages`` lane."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote

from pydantic import Field, field_serializer, field_validator

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
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json

_RESERVED_EXTENSION_DATA_FIELDS = frozenset({"hostId", "contextId", "actionUuid"})


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
    """Base class for typed ``plugin_messages`` lane bodies."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class EmptyPluginBody(PluginMessageBody):
    """Body for messages whose meaning lives entirely in the envelope/subject."""


class PluginExtensionBody(PluginMessageBody):
    """Explicit extension body for plugin lane extension traffic."""

    extension_type: str
    extension_schema_id: str
    data: JsonObject = Field(default_factory=dict)

    @field_validator("data", mode="after")
    @classmethod
    def _freeze_data(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        reserved = _RESERVED_EXTENSION_DATA_FIELDS & set(value)
        if reserved:
            fields = ", ".join(sorted(reserved))
            raise ValueError(f"extension data must not contain routing fields: {fields}")
        return freeze_json(value)

    @field_serializer("data")
    def _serialize_data(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class ActionsRegisteredBody(PluginMessageBody):
    action_uuids: tuple[str, ...] = Field(alias="actionUuids")
    actions: tuple[ActionDescriptor, ...]


class ActionsUnregisteredBody(PluginMessageBody):
    action_uuids: tuple[str, ...] = Field(alias="actionUuids")


class SettingsBody(PluginMessageBody):
    settings: JsonObject = Field(default_factory=dict)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class TitleOptionsBody(PluginMessageBody):
    text: str = ""
    title_options: TitleOptions | None = None


class ImageBody(PluginMessageBody):
    image: str = ""


class PageSelectBody(PluginMessageBody):
    profile: str = "default"
    page: int = 0


class OpenPageBody(PluginMessageBody):
    descriptor: DynamicPageDescriptor


class SlotCoordinates(DeckrModel):
    column: int
    row: int


class SlotImageFormat(DeckrModel):
    width: int
    height: int
    format: str
    rotation: int | None = None


class SlotInfo(DeckrModel):
    slot_id: str
    slot_type: str
    coordinates: SlotCoordinates | None = None
    gestures: tuple[str, ...] = Field(default_factory=tuple)
    image_format: SlotImageFormat | None = None


class WillAppearEvent(DeckrModel):
    event: str = "willAppear"
    slot: SlotInfo


class WillDisappearEvent(DeckrModel):
    event: str = "willDisappear"
    slot_id: str


class KeyEvent(DeckrModel):
    event: str
    slot_id: str


class DialRotateEvent(DeckrModel):
    event: str
    slot_id: str
    direction: str


class TouchSwipeEvent(DeckrModel):
    event: str
    slot_id: str
    direction: str


class PageAppearEvent(DeckrModel):
    event: str = "pageAppear"
    page_id: str
    timeout_ms: int | None = None


class PageDisappearEvent(DeckrModel):
    event: str = "pageDisappear"
    page_id: str
    reason: str | None = None


class ControllerEventBody(PluginMessageBody):
    event: Any
    settings: JsonObject = Field(default_factory=dict)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_event_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_event_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class WillAppearBody(ControllerEventBody):
    event: WillAppearEvent


class WillDisappearBody(ControllerEventBody):
    event: WillDisappearEvent


class KeyEventBody(ControllerEventBody):
    event: KeyEvent


class DialRotateBody(ControllerEventBody):
    event: DialRotateEvent


class TouchSwipeBody(ControllerEventBody):
    event: TouchSwipeEvent


class PageAppearBody(ControllerEventBody):
    event: PageAppearEvent


class PageDisappearBody(ControllerEventBody):
    event: PageDisappearEvent


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
    body: PluginMessageBody | Mapping[str, Any] | None = None,
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    parsed_body = plugin_body_for_type(message_type, body or {})
    return DeckrMessage(
        lane=PLUGIN_MESSAGES_LANE,
        messageType=message_type,
        sender=sender,
        recipient=_target(recipient),
        subject=subject,
        body=parsed_body.to_dict(),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def plugin_body_for_type(
    message_type: str,
    body: PluginMessageBody | Mapping[str, Any],
) -> PluginMessageBody:
    body_type = PLUGIN_BODY_BY_MESSAGE_TYPE.get(message_type)
    if body_type is None:
        raise ValueError(f"Unsupported plugin message type {message_type!r}")
    if isinstance(body, PluginMessageBody):
        if not isinstance(body, body_type):
            raise TypeError(
                f"{message_type!r} requires body type {body_type.__name__}, "
                f"got {type(body).__name__}"
            )
        return body
    return body_type.model_validate(body)


def plugin_body(message: DeckrMessage) -> PluginMessageBody:
    return plugin_body_for_type(message.message_type, message.body)


def plugin_body_dict(message: DeckrMessage) -> Mapping[str, Any]:
    return plugin_body(message).to_dict()


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


def context_subject(
    context_id: str,
    *,
    action_uuid: str | None = None,
) -> EntitySubject:
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
    if action_uuid is not None:
        identifiers["actionUuid"] = action_uuid
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


def subject_action_uuid(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("actionUuid")
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
    controllers: tuple[str, ...] | None = None
    property_inspector_path: str | None = None
    manifest_defaults: JsonObject | None = None

    @field_validator("manifest_defaults", mode="after")
    @classmethod
    def _freeze_manifest_defaults(
        cls,
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("manifest_defaults")
    def _serialize_manifest_defaults(
        self,
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None

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
PLUGIN_EXTENSION = "pluginExtension"


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


PLUGIN_BODY_BY_MESSAGE_TYPE: dict[str, type[PluginMessageBody]] = {
    ACTIONS_REGISTERED: ActionsRegisteredBody,
    ACTIONS_UNREGISTERED: ActionsUnregisteredBody,
    REQUEST_ACTIONS: EmptyPluginBody,
    HOST_ONLINE: EmptyPluginBody,
    HOST_OFFLINE: EmptyPluginBody,
    WILL_APPEAR: WillAppearBody,
    WILL_DISAPPEAR: WillDisappearBody,
    KEY_UP: KeyEventBody,
    KEY_DOWN: KeyEventBody,
    DIAL_ROTATE: DialRotateBody,
    TOUCH_TAP: KeyEventBody,
    TOUCH_SWIPE: TouchSwipeBody,
    PAGE_APPEAR: PageAppearBody,
    PAGE_DISAPPEAR: PageDisappearBody,
    SET_TITLE: TitleOptionsBody,
    SET_IMAGE: ImageBody,
    SHOW_ALERT: EmptyPluginBody,
    SHOW_OK: EmptyPluginBody,
    REQUEST_SETTINGS: EmptyPluginBody,
    HERE_ARE_SETTINGS: SettingsBody,
    SET_SETTINGS: SettingsBody,
    SET_PAGE: PageSelectBody,
    OPEN_PAGE: OpenPageBody,
    CLOSE_PAGE: EmptyPluginBody,
    SLEEP_SCREEN: EmptyPluginBody,
    WAKE_SCREEN: EmptyPluginBody,
    PLUGIN_EXTENSION: PluginExtensionBody,
}

ActionsRegisteredBody.model_rebuild()
TitleOptionsBody.model_rebuild()
OpenPageBody.model_rebuild()
