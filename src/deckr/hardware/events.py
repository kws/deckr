from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from deckr.contracts.messages import (
    HARDWARE_EVENTS_LANE,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    MessageTarget,
    controllers_broadcast,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
)
from deckr.contracts.models import DeckrModel


class HardwareCoordinates(DeckrModel):
    column: int
    row: int


class HardwareImageFormat(DeckrModel):
    width: int
    height: int
    format: str = "JPEG"
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False
    format_options: dict[str, Any] = Field(default_factory=dict)


class HardwareSlot(DeckrModel):
    id: str
    coordinates: HardwareCoordinates
    image_format: HardwareImageFormat | None = None
    slot_type: str = "key"
    gestures: tuple[str, ...] = Field(default_factory=tuple)


class HardwareDevice(DeckrModel):
    id: str
    fingerprint: str
    hid: str
    slots: tuple[HardwareSlot, ...]
    name: str | None = None


class HardwareDeviceRef(DeckrModel):
    manager_id: str
    device_id: str


class HardwareControlRef(HardwareDeviceRef):
    control_id: str
    control_kind: str


class DeviceConnectedMessage(DeckrModel):
    device: HardwareDevice


class DeviceDisconnectedMessage(DeckrModel):
    pass


class KeyDownMessage(DeckrModel):
    key_id: str


class KeyUpMessage(DeckrModel):
    key_id: str


class DialRotateMessage(DeckrModel):
    dial_id: str
    direction: Literal["clockwise", "counterclockwise"]


class TouchTapMessage(DeckrModel):
    touch_id: str


class TouchSwipeMessage(DeckrModel):
    touch_id: str
    direction: Literal["left", "right"]


class SetImageMessage(DeckrModel):
    slot_id: str
    image: bytes


class ClearSlotMessage(DeckrModel):
    slot_id: str


class SleepScreenMessage(DeckrModel):
    pass


class WakeScreenMessage(DeckrModel):
    pass


HardwareInputMessage = (
    DeviceConnectedMessage
    | DeviceDisconnectedMessage
    | KeyDownMessage
    | KeyUpMessage
    | DialRotateMessage
    | TouchTapMessage
    | TouchSwipeMessage
)

HardwareCommandMessage = (
    SetImageMessage
    | ClearSlotMessage
    | SleepScreenMessage
    | WakeScreenMessage
)

HardwareTransportMessage = HardwareInputMessage | HardwareCommandMessage

HARDWARE_INPUT_MESSAGE_TYPES = (
    DeviceConnectedMessage,
    DeviceDisconnectedMessage,
    KeyDownMessage,
    KeyUpMessage,
    DialRotateMessage,
    TouchTapMessage,
    TouchSwipeMessage,
)

HARDWARE_COMMAND_MESSAGE_TYPES = (
    SetImageMessage,
    ClearSlotMessage,
    SleepScreenMessage,
    WakeScreenMessage,
)


DEVICE_CONNECTED = "deviceConnected"
DEVICE_DISCONNECTED = "deviceDisconnected"
KEY_DOWN = "keyDown"
KEY_UP = "keyUp"
DIAL_ROTATE = "dialRotate"
TOUCH_TAP = "touchTap"
TOUCH_SWIPE = "touchSwipe"
SET_IMAGE = "setImage"
CLEAR_SLOT = "clearSlot"
SLEEP_SCREEN = "sleepScreen"
WAKE_SCREEN = "wakeScreen"

HARDWARE_BODY_BY_MESSAGE_TYPE: dict[str, type[HardwareTransportMessage]] = {
    DEVICE_CONNECTED: DeviceConnectedMessage,
    DEVICE_DISCONNECTED: DeviceDisconnectedMessage,
    KEY_DOWN: KeyDownMessage,
    KEY_UP: KeyUpMessage,
    DIAL_ROTATE: DialRotateMessage,
    TOUCH_TAP: TouchTapMessage,
    TOUCH_SWIPE: TouchSwipeMessage,
    SET_IMAGE: SetImageMessage,
    CLEAR_SLOT: ClearSlotMessage,
    SLEEP_SCREEN: SleepScreenMessage,
    WAKE_SCREEN: WakeScreenMessage,
}
HARDWARE_MESSAGE_TYPE_BY_BODY = {
    body_type: message_type
    for message_type, body_type in HARDWARE_BODY_BY_MESSAGE_TYPE.items()
}


def hardware_subject(
    *,
    manager_id: str,
    device_id: str,
    control_id: str | None = None,
    control_kind: str | None = None,
) -> EntitySubject:
    identifiers: dict[str, str] = {
        "managerId": manager_id,
        "deviceId": device_id,
    }
    if control_id is not None:
        identifiers["controlId"] = control_id
    if control_kind is not None:
        identifiers["controlKind"] = control_kind
    return entity_subject(
        "hardware_control" if control_id is not None else "hardware_device",
        **identifiers,
    )


def hardware_subject_for_device(ref: HardwareDeviceRef) -> EntitySubject:
    return hardware_subject(manager_id=ref.manager_id, device_id=ref.device_id)


def hardware_subject_for_control(ref: HardwareControlRef) -> EntitySubject:
    return hardware_subject(
        manager_id=ref.manager_id,
        device_id=ref.device_id,
        control_id=ref.control_id,
        control_kind=ref.control_kind,
    )


def subject_manager_id(subject: EntitySubject) -> str | None:
    return subject.identifiers.get("managerId")


def subject_device_id(subject: EntitySubject) -> str | None:
    return subject.identifiers.get("deviceId")


def subject_control_id(subject: EntitySubject) -> str | None:
    return subject.identifiers.get("controlId")


def subject_control_kind(subject: EntitySubject) -> str | None:
    return subject.identifiers.get("controlKind")


def hardware_device_ref_from_subject(subject: EntitySubject) -> HardwareDeviceRef | None:
    manager_id = subject_manager_id(subject)
    device_id = subject_device_id(subject)
    if manager_id is None or device_id is None:
        return None
    return HardwareDeviceRef(manager_id=manager_id, device_id=device_id)


def hardware_control_ref_from_subject(
    subject: EntitySubject,
) -> HardwareControlRef | None:
    manager_id = subject_manager_id(subject)
    device_id = subject_device_id(subject)
    control_id = subject_control_id(subject)
    control_kind = subject_control_kind(subject)
    if (
        manager_id is None
        or device_id is None
        or control_id is None
        or control_kind is None
    ):
        return None
    return HardwareControlRef(
        manager_id=manager_id,
        device_id=device_id,
        control_id=control_id,
        control_kind=control_kind,
    )


def hardware_device_ref_from_message(message: DeckrMessage) -> HardwareDeviceRef | None:
    return hardware_device_ref_from_subject(message.subject)


def hardware_body_to_dict(body: HardwareTransportMessage) -> dict[str, Any]:
    return body.model_dump(by_alias=True, exclude_none=True, mode="json")


def hardware_body_from_message(message: DeckrMessage) -> HardwareTransportMessage:
    body_type = HARDWARE_BODY_BY_MESSAGE_TYPE.get(message.message_type)
    if body_type is None:
        raise ValueError(f"Unsupported hardware message type {message.message_type!r}")
    return body_type.model_validate(message.body)


def hardware_message(
    *,
    sender: str | EndpointAddress,
    recipient: str | EndpointAddress | MessageTarget,
    message_type: str,
    body: HardwareTransportMessage,
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    target = recipient if not isinstance(recipient, str | EndpointAddress) else endpoint_target(recipient)
    return DeckrMessage(
        lane=HARDWARE_EVENTS_LANE,
        messageType=message_type,
        sender=sender,
        recipient=target,
        subject=subject,
        body=hardware_body_to_dict(body),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def hardware_event_message(
    *,
    manager_id: str,
    message_type: str,
    device_id: str,
    body: HardwareInputMessage,
    control_id: str | None = None,
    control_kind: str | None = None,
) -> DeckrMessage:
    return hardware_message(
        sender=hardware_manager_address(manager_id),
        recipient=controllers_broadcast(),
        message_type=message_type,
        body=body,
        subject=hardware_subject(
            manager_id=manager_id,
            device_id=device_id,
            control_id=control_id,
            control_kind=control_kind,
        ),
    )


def hardware_input_message(
    *,
    manager_id: str,
    device_id: str,
    body: HardwareInputMessage,
) -> DeckrMessage:
    message_type = HARDWARE_MESSAGE_TYPE_BY_BODY[type(body)]
    control_id: str | None = None
    control_kind: str | None = None
    if isinstance(body, KeyDownMessage | KeyUpMessage):
        control_id = body.key_id
        control_kind = "key"
    elif isinstance(body, DialRotateMessage):
        control_id = body.dial_id
        control_kind = "dial"
    elif isinstance(body, TouchTapMessage | TouchSwipeMessage):
        control_id = body.touch_id
        control_kind = "touch"
    return hardware_event_message(
        manager_id=manager_id,
        message_type=message_type,
        device_id=device_id,
        body=body,
        control_id=control_id,
        control_kind=control_kind,
    )


def hardware_command_message(
    *,
    controller_id: str,
    manager_id: str,
    message_type: str,
    device_id: str,
    body: HardwareCommandMessage,
    control_id: str | None = None,
    control_kind: str | None = None,
) -> DeckrMessage:
    return hardware_message(
        sender=f"controller:{controller_id}",
        recipient=hardware_manager_address(manager_id),
        message_type=message_type,
        body=body,
        subject=hardware_subject(
            manager_id=manager_id,
            device_id=device_id,
            control_id=control_id,
            control_kind=control_kind,
        ),
    )


def hardware_command_for_device(
    *,
    controller_id: str,
    ref: HardwareDeviceRef,
    message_type: str,
    body: HardwareCommandMessage,
) -> DeckrMessage:
    return hardware_command_message(
        controller_id=controller_id,
        manager_id=ref.manager_id,
        message_type=message_type,
        device_id=ref.device_id,
        body=body,
    )


def hardware_command_for_control(
    *,
    controller_id: str,
    ref: HardwareControlRef,
    message_type: str,
    body: HardwareCommandMessage,
) -> DeckrMessage:
    return hardware_command_message(
        controller_id=controller_id,
        manager_id=ref.manager_id,
        message_type=message_type,
        device_id=ref.device_id,
        control_id=ref.control_id,
        control_kind=ref.control_kind,
        body=body,
    )


def hardware_message_schema() -> dict[str, Any]:
    return DeckrMessage.model_json_schema(by_alias=True)
