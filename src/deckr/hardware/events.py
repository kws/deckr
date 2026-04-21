from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import quote, unquote

from pydantic import ConfigDict, Field, TypeAdapter

from deckr.core.util.pydantic import CamelModel, to_camel


class HWDevice(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def hid(self) -> str: ...

    @property
    def slots(self) -> list[HWSlot]: ...

    async def set_image(self, slot_id: str, image: bytes) -> None: ...

    async def clear_slot(self, slot_id: str) -> None: ...

    async def sleep_screen(self) -> None: ...

    async def wake_screen(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Coordinates:
    column: int
    row: int


@dataclass(frozen=True, slots=True, kw_only=True)
class HWSImageFormat:
    width: int
    height: int
    format: str = "JPEG"
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False
    format_options: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class HWSlot:
    id: str
    coordinates: Coordinates
    image_format: HWSImageFormat | None = None
    slot_type: str = (
        "key"  # "key" | "button" | "encoder" | "touch_dial" | "touch_strip"
    )
    gestures: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class HWDeviceInfo:
    id: str
    hid: str
    slots: list[HWSlot]
    name: str | None = None


def device_info_from_device(device: HWDevice) -> HWDeviceInfo:
    return HWDeviceInfo(
        id=device.id,
        hid=device.hid,
        slots=list(device.slots),
        name=getattr(device, "name", None),
    )


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


@dataclass(frozen=True, slots=True, kw_only=True)
class HardwareEvent:
    device_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceConnectedEvent(HardwareEvent):
    type: Literal["device_connected"] = "device_connected"
    device: HWDevice


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceDisconnectedEvent(HardwareEvent):
    type: Literal["device_disconnected"] = "device_disconnected"


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyDownEvent(HardwareEvent):
    type: Literal["key_down"] = "key_down"
    key_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyUpEvent(HardwareEvent):
    type: Literal["key_up"] = "key_up"
    key_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DialRotateEvent(HardwareEvent):
    type: Literal["dial_rotate"] = "dial_rotate"
    dial_id: str
    direction: Literal["clockwise", "counterclockwise"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TouchTapEvent(HardwareEvent):
    type: Literal["touch_tap"] = "touch_tap"
    touch_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TouchSwipeEvent(HardwareEvent):
    type: Literal["touch_swipe"] = "touch_swipe"
    touch_id: str
    direction: Literal["left", "right"]


@dataclass(frozen=True, slots=True, kw_only=True)
class HardwareCommand:
    device_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SetImageCommand(HardwareCommand):
    type: Literal["set_image"] = "set_image"
    slot_id: str
    image: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class ClearSlotCommand(HardwareCommand):
    type: Literal["clear_slot"] = "clear_slot"
    slot_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SleepScreenCommand(HardwareCommand):
    type: Literal["sleep_screen"] = "sleep_screen"


@dataclass(frozen=True, slots=True, kw_only=True)
class WakeScreenCommand(HardwareCommand):
    type: Literal["wake_screen"] = "wake_screen"


HardwareInteractionEvent = (
    KeyDownEvent | KeyUpEvent | DialRotateEvent | TouchTapEvent | TouchSwipeEvent
)
HardwareInputEvent = (
    DeviceConnectedEvent | DeviceDisconnectedEvent | HardwareInteractionEvent
)
HardwareOutputCommand = (
    SetImageCommand | ClearSlotCommand | SleepScreenCommand | WakeScreenCommand
)


class HardwareWireModel(CamelModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class WireCoordinates(HardwareWireModel):
    column: int
    row: int


class WireHWSImageFormat(HardwareWireModel):
    width: int
    height: int
    format: str = "JPEG"
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False
    format_options: dict[str, Any] = Field(default_factory=dict)


class WireHWSlot(HardwareWireModel):
    id: str
    coordinates: WireCoordinates
    image_format: WireHWSImageFormat | None = None
    slot_type: str = "key"
    gestures: list[str] = Field(default_factory=list)


class WireHWDevice(HardwareWireModel):
    id: str
    hid: str
    slots: list[WireHWSlot]
    name: str | None = None


class ManagerHelloMessage(HardwareWireModel):
    type: Literal["managerHello"] = "managerHello"
    manager_id: str


class ControllerHelloMessage(HardwareWireModel):
    type: Literal["controllerHello"] = "controllerHello"
    controller_id: str


class DeviceConnectedMessage(HardwareWireModel):
    type: Literal["deviceConnected"] = "deviceConnected"
    device_id: str
    device: WireHWDevice


class DeviceDisconnectedMessage(HardwareWireModel):
    type: Literal["deviceDisconnected"] = "deviceDisconnected"
    device_id: str


class KeyDownMessage(HardwareWireModel):
    type: Literal["keyDown"] = "keyDown"
    device_id: str
    key_id: str


class KeyUpMessage(HardwareWireModel):
    type: Literal["keyUp"] = "keyUp"
    device_id: str
    key_id: str


class DialRotateMessage(HardwareWireModel):
    type: Literal["dialRotate"] = "dialRotate"
    device_id: str
    dial_id: str
    direction: Literal["clockwise", "counterclockwise"]


class TouchTapMessage(HardwareWireModel):
    type: Literal["touchTap"] = "touchTap"
    device_id: str
    touch_id: str


class TouchSwipeMessage(HardwareWireModel):
    type: Literal["touchSwipe"] = "touchSwipe"
    device_id: str
    touch_id: str
    direction: Literal["left", "right"]


class SetImageMessage(HardwareWireModel):
    type: Literal["setImage"] = "setImage"
    device_id: str
    slot_id: str
    image: bytes


class ClearSlotMessage(HardwareWireModel):
    type: Literal["clearSlot"] = "clearSlot"
    device_id: str
    slot_id: str


class SleepScreenMessage(HardwareWireModel):
    type: Literal["sleepScreen"] = "sleepScreen"
    device_id: str


class WakeScreenMessage(HardwareWireModel):
    type: Literal["wakeScreen"] = "wakeScreen"
    device_id: str


HardwareTransportMessage = Annotated[
    ManagerHelloMessage
    | ControllerHelloMessage
    | DeviceConnectedMessage
    | DeviceDisconnectedMessage
    | KeyDownMessage
    | KeyUpMessage
    | DialRotateMessage
    | TouchTapMessage
    | TouchSwipeMessage
    | SetImageMessage
    | ClearSlotMessage
    | SleepScreenMessage
    | WakeScreenMessage,
    Field(discriminator="type"),
]

_transport_message_adapter = TypeAdapter(HardwareTransportMessage)


def _slot_to_wire(slot: HWSlot) -> WireHWSlot:
    image_format = None
    if slot.image_format is not None:
        image_format = WireHWSImageFormat(
            width=slot.image_format.width,
            height=slot.image_format.height,
            format=slot.image_format.format,
            rotation=slot.image_format.rotation,
            flip_x=slot.image_format.flip_x,
            flip_y=slot.image_format.flip_y,
            format_options=dict(slot.image_format.format_options),
        )
    return WireHWSlot(
        id=slot.id,
        coordinates=WireCoordinates(
            column=slot.coordinates.column,
            row=slot.coordinates.row,
        ),
        image_format=image_format,
        slot_type=slot.slot_type,
        gestures=sorted(slot.gestures),
    )


def _slot_from_wire(slot: WireHWSlot) -> HWSlot:
    image_format = None
    if slot.image_format is not None:
        image_format = HWSImageFormat(
            width=slot.image_format.width,
            height=slot.image_format.height,
            format=slot.image_format.format,
            rotation=slot.image_format.rotation,
            flip_x=slot.image_format.flip_x,
            flip_y=slot.image_format.flip_y,
            format_options=dict(slot.image_format.format_options),
        )
    return HWSlot(
        id=slot.id,
        coordinates=Coordinates(
            column=slot.coordinates.column,
            row=slot.coordinates.row,
        ),
        image_format=image_format,
        slot_type=slot.slot_type,
        gestures=frozenset(slot.gestures),
    )


def device_info_to_wire(device: HWDevice | HWDeviceInfo) -> WireHWDevice:
    info = (
        device if isinstance(device, HWDeviceInfo) else device_info_from_device(device)
    )
    return WireHWDevice(
        id=info.id,
        hid=info.hid,
        slots=[_slot_to_wire(slot) for slot in info.slots],
        name=info.name,
    )


def device_info_from_wire(device: WireHWDevice) -> HWDeviceInfo:
    return HWDeviceInfo(
        id=device.id,
        hid=device.hid,
        slots=[_slot_from_wire(slot) for slot in device.slots],
        name=device.name,
    )


def hardware_message_to_wire(message: HardwareTransportMessage) -> dict[str, Any]:
    return _transport_message_adapter.dump_python(message, by_alias=True, mode="json")


def hardware_message_from_wire(data: dict[str, Any]) -> HardwareTransportMessage:
    return _transport_message_adapter.validate_python(data)


def event_to_transport_message(event: HardwareInputEvent) -> HardwareTransportMessage:
    if isinstance(event, DeviceConnectedEvent):
        return DeviceConnectedMessage(
            device_id=event.device_id,
            device=device_info_to_wire(event.device),
        )
    if isinstance(event, DeviceDisconnectedEvent):
        return DeviceDisconnectedMessage(device_id=event.device_id)
    if isinstance(event, KeyDownEvent):
        return KeyDownMessage(device_id=event.device_id, key_id=event.key_id)
    if isinstance(event, KeyUpEvent):
        return KeyUpMessage(device_id=event.device_id, key_id=event.key_id)
    if isinstance(event, DialRotateEvent):
        return DialRotateMessage(
            device_id=event.device_id,
            dial_id=event.dial_id,
            direction=event.direction,
        )
    if isinstance(event, TouchTapEvent):
        return TouchTapMessage(device_id=event.device_id, touch_id=event.touch_id)
    if isinstance(event, TouchSwipeEvent):
        return TouchSwipeMessage(
            device_id=event.device_id,
            touch_id=event.touch_id,
            direction=event.direction,
        )
    raise TypeError(f"Unsupported hardware event type: {type(event)!r}")


def command_to_transport_message(
    command: HardwareOutputCommand,
) -> HardwareTransportMessage:
    if isinstance(command, SetImageCommand):
        return SetImageMessage(
            device_id=command.device_id,
            slot_id=command.slot_id,
            image=command.image,
        )
    if isinstance(command, ClearSlotCommand):
        return ClearSlotMessage(
            device_id=command.device_id,
            slot_id=command.slot_id,
        )
    if isinstance(command, SleepScreenCommand):
        return SleepScreenMessage(device_id=command.device_id)
    if isinstance(command, WakeScreenCommand):
        return WakeScreenMessage(device_id=command.device_id)
    raise TypeError(f"Unsupported hardware command type: {type(command)!r}")


def transport_message_to_event(
    message: HardwareTransportMessage,
    *,
    device: HWDevice | None = None,
    device_id: str | None = None,
) -> HardwareInputEvent:
    resolved_device_id = device_id or getattr(message, "device_id", None)
    if isinstance(message, DeviceConnectedMessage):
        if device is None:
            raise ValueError("device is required for deviceConnected messages")
        return DeviceConnectedEvent(device_id=device.id, device=device)
    if resolved_device_id is None:
        raise ValueError("device_id is required for hardware event messages")
    if isinstance(message, DeviceDisconnectedMessage):
        return DeviceDisconnectedEvent(device_id=resolved_device_id)
    if isinstance(message, KeyDownMessage):
        return KeyDownEvent(device_id=resolved_device_id, key_id=message.key_id)
    if isinstance(message, KeyUpMessage):
        return KeyUpEvent(device_id=resolved_device_id, key_id=message.key_id)
    if isinstance(message, DialRotateMessage):
        return DialRotateEvent(
            device_id=resolved_device_id,
            dial_id=message.dial_id,
            direction=message.direction,
        )
    if isinstance(message, TouchTapMessage):
        return TouchTapEvent(device_id=resolved_device_id, touch_id=message.touch_id)
    if isinstance(message, TouchSwipeMessage):
        return TouchSwipeEvent(
            device_id=resolved_device_id,
            touch_id=message.touch_id,
            direction=message.direction,
        )
    raise TypeError(f"Unsupported hardware event message: {type(message)!r}")


def transport_message_to_command(
    message: HardwareTransportMessage,
    *,
    device_id: str | None = None,
) -> HardwareOutputCommand:
    resolved_device_id = device_id or getattr(message, "device_id", None)
    if resolved_device_id is None:
        raise ValueError("device_id is required for hardware command messages")
    if isinstance(message, SetImageMessage):
        return SetImageCommand(
            device_id=resolved_device_id,
            slot_id=message.slot_id,
            image=message.image,
        )
    if isinstance(message, ClearSlotMessage):
        return ClearSlotCommand(
            device_id=resolved_device_id,
            slot_id=message.slot_id,
        )
    if isinstance(message, SleepScreenMessage):
        return SleepScreenCommand(device_id=resolved_device_id)
    if isinstance(message, WakeScreenMessage):
        return WakeScreenCommand(device_id=resolved_device_id)
    raise TypeError(f"Unsupported hardware command message: {type(message)!r}")


async def apply_command(device: HWDevice, command: HardwareOutputCommand) -> None:
    if isinstance(command, SetImageCommand):
        await device.set_image(command.slot_id, command.image)
        return
    if isinstance(command, ClearSlotCommand):
        await device.clear_slot(command.slot_id)
        return
    if isinstance(command, SleepScreenCommand):
        await device.sleep_screen()
        return
    if isinstance(command, WakeScreenCommand):
        await device.wake_screen()
        return
    raise TypeError(f"Unsupported hardware command type: {type(command)!r}")
