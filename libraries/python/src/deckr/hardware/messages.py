from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, JsonValue, field_serializer, field_validator

from deckr.contracts.messages import (
    HARDWARE_MESSAGES_LANE,
    HARDWARE_MESSAGES_SCHEMA_ID,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    MessageTarget,
    controllers_broadcast,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
)
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.hardware.descriptors import (
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
    DeviceSourceReference,
)

DEVICE_AVAILABLE = "deviceAvailable"
DEVICE_DESCRIPTOR_CHANGED = "deviceDescriptorChanged"
DEVICE_UNAVAILABLE = "deviceUnavailable"
CONTROL_INPUT = "controlInput"
CONTROL_COMMAND = "controlCommand"

# Reserved wire names for capability state and command reply messages (bodies TBD).
CAPABILITY_STATE_CHANGED = "capabilityStateChanged"
CAPABILITY_STATE_REQUEST = "capabilityStateRequest"
CAPABILITY_STATE_REPLY = "capabilityStateReply"
COMMAND_ACCEPTED = "commandAccepted"
COMMAND_REJECTED = "commandRejected"
COMMAND_REPLY = "commandReply"

CommandRejectionReason = Literal[
    "malformed",
    "unsupported",
    "expired",
    "unauthorized",
    "stale",
    "rejected",
]
CapabilityStateStatus = Literal["ok", "unavailable", "unsupported", "rejected"]


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _require_non_empty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_optional_non_empty(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_empty(value, field_name=field_name)


def _require_non_negative(value: int | None, *, field_name: str) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


class DeviceAvailableMessage(DeckrModel):
    descriptor: DeviceDescriptor


class DeviceDescriptorChangedMessage(DeckrModel):
    descriptor: DeviceDescriptor


class DeviceUnavailableMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="reason")


class ControlInputMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str = Field(alias="controlId")
    capability_id: str = Field(alias="capabilityId")
    event_type: str = Field(alias="eventType")
    value: JsonValue | None = None
    sequence: int | None = None
    occurred_at: datetime = Field(default_factory=_now_utc, alias="occurredAt")
    sources: tuple[DeviceSourceReference, ...] = Field(default_factory=tuple)

    @field_validator("control_id", "capability_id", "event_type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value, field_name="control input target")

    @field_validator("sequence")
    @classmethod
    def _validate_sequence(cls, value: int | None) -> int | None:
        return _require_non_negative(value, field_name="sequence")

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: JsonValue | None) -> Any:
        if value is None:
            return None
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ControlCommandMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str | None = Field(default=None, alias="controlId")
    capability_id: str = Field(alias="capabilityId")
    command_type: str = Field(alias="commandType")
    params: JsonObject = Field(default_factory=dict)

    @field_validator("capability_id", "command_type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value, field_name="control command target")

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="control command target")

    @field_validator("params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class CapabilityStateChangedMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    capability_id: str = Field(alias="capabilityId")
    value: JsonValue | None = None
    control_id: str | None = Field(default=None, alias="controlId")
    state_type: str | None = Field(default=None, alias="stateType")
    sequence: int | None = None
    occurred_at: datetime = Field(default_factory=_now_utc, alias="occurredAt")

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _require_non_empty(value, field_name="capability state target")

    @field_validator("control_id", "state_type")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="capability state target")

    @field_validator("sequence")
    @classmethod
    def _validate_sequence(cls, value: int | None) -> int | None:
        return _require_non_negative(value, field_name="sequence")

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: JsonValue | None) -> Any:
        if value is None:
            return None
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CapabilityStateRequestMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    capability_id: str = Field(alias="capabilityId")
    control_id: str | None = Field(default=None, alias="controlId")
    state_type: str | None = Field(default=None, alias="stateType")
    params: JsonObject = Field(default_factory=dict)

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _require_non_empty(value, field_name="capability state target")

    @field_validator("control_id", "state_type")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="capability state target")

    @field_validator("params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class CapabilityStateReplyMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    capability_id: str = Field(alias="capabilityId")
    status: CapabilityStateStatus = "ok"
    value: JsonValue | None = None
    control_id: str | None = Field(default=None, alias="controlId")
    state_type: str | None = Field(default=None, alias="stateType")
    error: str | None = None

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _require_non_empty(value, field_name="capability state target")

    @field_validator("control_id", "state_type", "error")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="capability state reply")

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: JsonValue | None) -> Any:
        if value is None:
            return None
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)


class CommandAcceptedMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str | None = Field(default=None, alias="controlId")
    capability_id: str = Field(alias="capabilityId")
    command_type: str = Field(alias="commandType")
    accepted_at: datetime = Field(default_factory=_now_utc, alias="acceptedAt")

    @field_validator("capability_id", "command_type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value, field_name="command acknowledgement")

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="command acknowledgement")

    @field_serializer("accepted_at")
    def _serialize_accepted_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CommandRejectedMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str | None = Field(default=None, alias="controlId")
    capability_id: str = Field(alias="capabilityId")
    command_type: str = Field(alias="commandType")
    reason: CommandRejectionReason
    message: str | None = None

    @field_validator("capability_id", "command_type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value, field_name="command rejection")

    @field_validator("control_id", "message")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="command rejection")


class CommandReplyMessage(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str | None = Field(default=None, alias="controlId")
    capability_id: str = Field(alias="capabilityId")
    command_type: str = Field(alias="commandType")
    result: JsonValue | None = None

    @field_validator("capability_id", "command_type")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_non_empty(value, field_name="command reply")

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str | None) -> str | None:
        return _require_optional_non_empty(value, field_name="command reply")

    @field_validator("result", mode="after")
    @classmethod
    def _freeze_result(cls, value: JsonValue | None) -> Any:
        if value is None:
            return None
        return freeze_json(value)

    @field_serializer("result")
    def _serialize_result(self, value: Any) -> Any:
        return thaw_json(value)

HardwareMessageBody = (
    DeviceAvailableMessage
    | DeviceDescriptorChangedMessage
    | DeviceUnavailableMessage
    | ControlInputMessage
    | ControlCommandMessage
    | CapabilityStateChangedMessage
    | CapabilityStateRequestMessage
    | CapabilityStateReplyMessage
    | CommandAcceptedMessage
    | CommandRejectedMessage
    | CommandReplyMessage
)

HARDWARE_BODY_BY_MESSAGE_TYPE: dict[str, type[HardwareMessageBody]] = {
    DEVICE_AVAILABLE: DeviceAvailableMessage,
    DEVICE_DESCRIPTOR_CHANGED: DeviceDescriptorChangedMessage,
    DEVICE_UNAVAILABLE: DeviceUnavailableMessage,
    CONTROL_INPUT: ControlInputMessage,
    CONTROL_COMMAND: ControlCommandMessage,
    CAPABILITY_STATE_CHANGED: CapabilityStateChangedMessage,
    CAPABILITY_STATE_REQUEST: CapabilityStateRequestMessage,
    CAPABILITY_STATE_REPLY: CapabilityStateReplyMessage,
    COMMAND_ACCEPTED: CommandAcceptedMessage,
    COMMAND_REJECTED: CommandRejectedMessage,
    COMMAND_REPLY: CommandReplyMessage,
}
HARDWARE_MESSAGE_TYPE_BY_BODY = {
    body_type: message_type
    for message_type, body_type in HARDWARE_BODY_BY_MESSAGE_TYPE.items()
}
_HARDWARE_BODY_TYPES = tuple(HARDWARE_BODY_BY_MESSAGE_TYPE.values())


def hardware_subject_for_device(ref: DeviceRef) -> EntitySubject:
    return entity_subject(
        "hardware_device",
        managerId=ref.manager_id,
        deviceId=ref.device_id,
    )


def hardware_subject_for_capability(ref: CapabilityRef) -> EntitySubject:
    device = ref.device_ref
    if device is None:
        raise ValueError("capability subject requires deviceRef")
    identifiers = {
        "managerId": device.manager_id,
        "deviceId": device.device_id,
        "capabilityId": ref.capability_id,
    }
    if ref.control_id is not None:
        identifiers["controlId"] = ref.control_id
    return entity_subject(
        "hardware_capability",
        **identifiers,
    )


def hardware_capability_ref_from_subject(subject: EntitySubject) -> CapabilityRef | None:
    if subject.kind != "hardware_capability":
        return None
    ids = subject.identifiers
    manager_id = ids.get("managerId")
    device_id = ids.get("deviceId")
    control_id = ids.get("controlId")
    capability_id = ids.get("capabilityId")
    if manager_id is None or device_id is None or capability_id is None:
        return None
    return CapabilityRef(
        deviceRef=DeviceRef(managerId=manager_id, deviceId=device_id),
        controlId=control_id,
        capabilityId=capability_id,
    )


def hardware_body_for_type(
    message_type: str,
    body: HardwareMessageBody | Mapping[str, Any],
) -> HardwareMessageBody:
    body_type = HARDWARE_BODY_BY_MESSAGE_TYPE.get(message_type)
    if body_type is None:
        raise ValueError(f"Unsupported hardware message type {message_type!r}")
    if isinstance(body, _HARDWARE_BODY_TYPES):
        if not isinstance(body, body_type):
            raise TypeError(
                f"{message_type!r} requires body type {body_type.__name__}, "
                f"got {type(body).__name__}"
            )
        return body
    if isinstance(body, DeckrModel):
        raise TypeError(
            f"{message_type!r} requires body type {body_type.__name__}, "
            f"got {type(body).__name__}"
        )
    return body_type.model_validate(thaw_json(dict(body)))


def hardware_body_to_dict(body: HardwareMessageBody) -> dict[str, Any]:
    return body.model_dump(by_alias=True, exclude_none=True, mode="json")


def hardware_body_from_message(message: DeckrMessage) -> HardwareMessageBody:
    return hardware_body_for_type(message.message_type, message.body)


def hardware_device_ref_from_message(message: DeckrMessage) -> DeviceRef | None:
    """Return the manager-local device ref for capability-targeted hardware traffic."""
    if message.message_type in {
        CAPABILITY_STATE_CHANGED,
        CAPABILITY_STATE_REPLY,
        CAPABILITY_STATE_REQUEST,
        COMMAND_ACCEPTED,
        COMMAND_REJECTED,
        COMMAND_REPLY,
        CONTROL_INPUT,
        CONTROL_COMMAND,
        DEVICE_UNAVAILABLE,
    }:
        body = HARDWARE_BODY_BY_MESSAGE_TYPE[message.message_type].model_validate(
            thaw_json(dict(message.body))
        )
        return body.device_ref
    if message.message_type in {DEVICE_AVAILABLE, DEVICE_DESCRIPTOR_CHANGED}:
        body = HARDWARE_BODY_BY_MESSAGE_TYPE[message.message_type].model_validate(
            thaw_json(dict(message.body))
        )
        manager_id = message.subject.identifiers.get("managerId")
        if manager_id is None:
            return None
        descriptor = body.descriptor
        return DeviceRef(
            managerId=manager_id,
            deviceId=descriptor.device_id,
            fingerprint=descriptor.fingerprint,
        )
    return None


def hardware_message(
    *,
    sender: str | EndpointAddress,
    sender_session_id: str,
    recipient: str | EndpointAddress | MessageTarget,
    recipient_session_id: str | None = None,
    message_type: str,
    body: HardwareMessageBody | Mapping[str, Any],
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    target = (
        recipient
        if not isinstance(recipient, str | EndpointAddress)
        else endpoint_target(recipient)
    )
    parsed_body = hardware_body_for_type(message_type, body)
    return DeckrMessage(
        lane=HARDWARE_MESSAGES_LANE,
        messageType=message_type,
        sender=sender,
        senderSessionId=sender_session_id,
        recipient=target,
        recipientSessionId=recipient_session_id,
        subject=subject,
        body=hardware_body_to_dict(parsed_body),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def device_available_message(
    *,
    manager_id: str,
    sender_session_id: str,
    descriptor: DeviceDescriptor,
) -> DeckrMessage:
    body = DeviceAvailableMessage(descriptor=descriptor)
    return hardware_message(
        sender=hardware_manager_address(manager_id),
        sender_session_id=sender_session_id,
        recipient=controllers_broadcast(),
        message_type=DEVICE_AVAILABLE,
        body=body,
        subject=hardware_subject_for_device(
            DeviceRef(managerId=manager_id, deviceId=descriptor.device_id),
        ),
    )


def device_descriptor_changed_message(
    *,
    manager_id: str,
    sender_session_id: str,
    descriptor: DeviceDescriptor,
) -> DeckrMessage:
    body = DeviceDescriptorChangedMessage(descriptor=descriptor)
    return hardware_message(
        sender=hardware_manager_address(manager_id),
        sender_session_id=sender_session_id,
        recipient=controllers_broadcast(),
        message_type=DEVICE_DESCRIPTOR_CHANGED,
        body=body,
        subject=hardware_subject_for_device(
            DeviceRef(managerId=manager_id, deviceId=descriptor.device_id),
        ),
    )


def control_input_message(
    *,
    manager_id: str,
    sender_session_id: str,
    device_id: str,
    fingerprint: str | None = None,
    control_id: str,
    capability_id: str,
    event_type: str,
    value: JsonValue | None = None,
    sequence: int | None = None,
    occurred_at: datetime | None = None,
    sources: tuple[DeviceSourceReference, ...] = (),
) -> DeckrMessage:
    device_ref = DeviceRef(
        managerId=manager_id,
        deviceId=device_id,
        fingerprint=fingerprint,
    )
    body = ControlInputMessage(
        deviceRef=device_ref,
        controlId=control_id,
        capabilityId=capability_id,
        eventType=event_type,
        value=value,
        sequence=sequence,
        occurredAt=occurred_at or _now_utc(),
        sources=sources,
    )
    return hardware_message(
        sender=hardware_manager_address(manager_id),
        sender_session_id=sender_session_id,
        recipient=controllers_broadcast(),
        message_type=CONTROL_INPUT,
        body=body,
        subject=hardware_subject_for_capability(
            CapabilityRef(
                deviceRef=DeviceRef(managerId=manager_id, deviceId=device_id),
                controlId=control_id,
                capabilityId=capability_id,
            )
        ),
    )


def device_unavailable_message(
    *,
    manager_id: str,
    sender_session_id: str,
    device_id: str,
    fingerprint: str | None = None,
    reason: str | None = None,
) -> DeckrMessage:
    device_ref = DeviceRef(
        managerId=manager_id,
        deviceId=device_id,
        fingerprint=fingerprint,
    )
    body = DeviceUnavailableMessage(deviceRef=device_ref, reason=reason)
    return hardware_message(
        sender=hardware_manager_address(manager_id),
        sender_session_id=sender_session_id,
        recipient=controllers_broadcast(),
        message_type=DEVICE_UNAVAILABLE,
        body=body,
        subject=hardware_subject_for_device(device_ref),
    )


def control_command_for_capability(
    *,
    controller_id: str,
    sender_session_id: str,
    ref: CapabilityRef,
    command_type: str,
    params: JsonObject | None = None,
    recipient_session_id: str | None = None,
) -> DeckrMessage:
    device = ref.device_ref
    if device is None:
        raise ValueError("capability command requires deviceRef")
    control_id = ref.control_id
    return control_command_message(
        controller_id=controller_id,
        sender_session_id=sender_session_id,
        manager_id=device.manager_id,
        device_id=device.device_id,
        control_id=control_id,
        capability_id=ref.capability_id,
        command_type=command_type,
        params=params,
        recipient_session_id=recipient_session_id,
    )


def control_command_message(
    *,
    controller_id: str,
    sender_session_id: str,
    manager_id: str,
    device_id: str,
    capability_id: str,
    command_type: str,
    control_id: str | None = None,
    params: JsonObject | None = None,
    recipient_session_id: str | None = None,
) -> DeckrMessage:
    device_ref = DeviceRef(managerId=manager_id, deviceId=device_id)
    body = ControlCommandMessage(
        deviceRef=device_ref,
        controlId=control_id,
        capabilityId=capability_id,
        commandType=command_type,
        params=dict(params or {}),
    )
    return hardware_message(
        sender=f"controller:{controller_id}",
        sender_session_id=sender_session_id,
        recipient=endpoint_target(hardware_manager_address(manager_id)),
        recipient_session_id=recipient_session_id,
        message_type=CONTROL_COMMAND,
        body=body,
        subject=hardware_subject_for_capability(
            CapabilityRef(
                deviceRef=device_ref,
                controlId=control_id,
                capabilityId=capability_id,
            )
        ),
    )


def hardware_message_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    envelope_ref = _add_schema_model(definitions, DeckrMessage)
    variants: list[dict[str, Any]] = []
    for message_type, body_type in HARDWARE_BODY_BY_MESSAGE_TYPE.items():
        body_ref = _add_schema_model(definitions, body_type)
        variants.append(
            {
                "allOf": [
                    envelope_ref,
                    {
                        "type": "object",
                        "required": ["lane", "messageType", "body"],
                        "properties": {
                            "lane": {"const": HARDWARE_MESSAGES_LANE},
                            "messageType": {"const": message_type},
                            "body": body_ref,
                        },
                    },
                ]
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": HARDWARE_MESSAGES_SCHEMA_ID,
        "title": "Deckr hardware_messages Lane Message",
        "x-deckr-schema-version": "1",
        "oneOf": variants,
        "$defs": definitions,
    }


def _add_schema_model(
    definitions: dict[str, Any],
    model: type[DeckrModel],
) -> dict[str, str]:
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    for name, definition in schema.pop("$defs", {}).items():
        _add_schema_definition(definitions, name, definition)
    schema.pop("$schema", None)
    name = model.__name__
    _add_schema_definition(definitions, name, schema)
    return {"$ref": f"#/$defs/{name}"}


def _add_schema_definition(
    definitions: dict[str, Any],
    name: str,
    definition: Mapping[str, Any],
) -> None:
    schema = deepcopy(dict(definition))
    existing = definitions.get(name)
    if existing is not None:
        if existing != schema:
            raise RuntimeError(f"Conflicting schema definition {name!r}")
        return
    definitions[name] = schema


__all__ = [
    "CAPABILITY_STATE_CHANGED",
    "CAPABILITY_STATE_REPLY",
    "CAPABILITY_STATE_REQUEST",
    "COMMAND_ACCEPTED",
    "COMMAND_REJECTED",
    "COMMAND_REPLY",
    "CONTROL_COMMAND",
    "CONTROL_INPUT",
    "CapabilityStateStatus",
    "CapabilityStateChangedMessage",
    "CapabilityStateReplyMessage",
    "CapabilityStateRequestMessage",
    "CommandAcceptedMessage",
    "CommandRejectedMessage",
    "CommandRejectionReason",
    "CommandReplyMessage",
    "ControlCommandMessage",
    "ControlInputMessage",
    "DEVICE_AVAILABLE",
    "DEVICE_DESCRIPTOR_CHANGED",
    "DEVICE_UNAVAILABLE",
    "DeviceAvailableMessage",
    "DeviceDescriptorChangedMessage",
    "DeviceUnavailableMessage",
    "HARDWARE_BODY_BY_MESSAGE_TYPE",
    "HARDWARE_MESSAGE_TYPE_BY_BODY",
    "HardwareMessageBody",
    "hardware_body_for_type",
    "control_command_for_capability",
    "control_command_message",
    "control_input_message",
    "device_available_message",
    "device_descriptor_changed_message",
    "device_unavailable_message",
    "hardware_body_from_message",
    "hardware_body_to_dict",
    "hardware_capability_ref_from_subject",
    "hardware_device_ref_from_message",
    "hardware_message",
    "hardware_message_schema",
    "hardware_subject_for_capability",
    "hardware_subject_for_device",
]
