from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

from pydantic import Field, RootModel

from deckr.contracts.models import DeckrModel


def _encode_runtime_id(value: str) -> str:
    return quote(value, safe="")


def _decode_runtime_id(value: str) -> str:
    return unquote(value)


def build_remote_device_id(manager_id: str, device_id: str) -> str:
    return (
        f"manager={_encode_runtime_id(manager_id)}"
        f"|device={_encode_runtime_id(device_id)}"
    )


def parse_remote_device_id(remote_device_id: str) -> dict[str, str | None]:
    if not remote_device_id.startswith("manager="):
        return {"manager_id": None, "device_id": remote_device_id or None}

    parsed: dict[str, str | None] = {
        "manager_id": None,
        "device_id": None,
    }
    for item in remote_device_id.split("|"):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        decoded = _decode_runtime_id(value)
        if key == "manager":
            parsed["manager_id"] = decoded
        elif key == "device":
            parsed["device_id"] = decoded
    return parsed


class WireCoordinates(DeckrModel):
    column: int
    row: int


class WireHWSImageFormat(DeckrModel):
    width: int
    height: int
    format: str = "JPEG"
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False
    format_options: dict[str, Any] = Field(default_factory=dict)


class WireHWSlot(DeckrModel):
    id: str
    coordinates: WireCoordinates
    image_format: WireHWSImageFormat | None = None
    slot_type: str = "key"
    gestures: tuple[str, ...] = Field(default_factory=tuple)


class WireHWDevice(DeckrModel):
    id: str
    hid: str
    slots: tuple[WireHWSlot, ...]
    name: str | None = None


class DeviceConnectedMessage(DeckrModel):
    type: Literal["deviceConnected"] = "deviceConnected"
    device_id: str
    device: WireHWDevice


class DeviceDisconnectedMessage(DeckrModel):
    type: Literal["deviceDisconnected"] = "deviceDisconnected"
    device_id: str


class KeyDownMessage(DeckrModel):
    type: Literal["keyDown"] = "keyDown"
    device_id: str
    key_id: str


class KeyUpMessage(DeckrModel):
    type: Literal["keyUp"] = "keyUp"
    device_id: str
    key_id: str


class DialRotateMessage(DeckrModel):
    type: Literal["dialRotate"] = "dialRotate"
    device_id: str
    dial_id: str
    direction: Literal["clockwise", "counterclockwise"]


class TouchTapMessage(DeckrModel):
    type: Literal["touchTap"] = "touchTap"
    device_id: str
    touch_id: str


class TouchSwipeMessage(DeckrModel):
    type: Literal["touchSwipe"] = "touchSwipe"
    device_id: str
    touch_id: str
    direction: Literal["left", "right"]


class SetImageMessage(DeckrModel):
    type: Literal["setImage"] = "setImage"
    device_id: str
    slot_id: str
    image: bytes


class ClearSlotMessage(DeckrModel):
    type: Literal["clearSlot"] = "clearSlot"
    device_id: str
    slot_id: str


class SleepScreenMessage(DeckrModel):
    type: Literal["sleepScreen"] = "sleepScreen"
    device_id: str


class WakeScreenMessage(DeckrModel):
    type: Literal["wakeScreen"] = "wakeScreen"
    device_id: str


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

HardwareTransportMessage = Annotated[
    HardwareInputMessage | HardwareCommandMessage,
    Field(discriminator="type"),
]

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


class HardwareTransportEnvelope(RootModel[HardwareTransportMessage]):
    root: HardwareTransportMessage


def hardware_message_to_wire(message: HardwareTransportMessage) -> dict[str, Any]:
    return HardwareTransportEnvelope(root=message).model_dump(
        by_alias=True,
        mode="json",
    )


def hardware_message_from_wire(data: dict[str, Any]) -> HardwareTransportMessage:
    return HardwareTransportEnvelope.model_validate(data).root


def hardware_transport_message_schema() -> dict[str, Any]:
    return HardwareTransportEnvelope.model_json_schema(
        by_alias=True,
    )
