from __future__ import annotations

from typing import Literal

from pydantic import Field

from deckr.contracts.models import DeckrModel


class Size(DeckrModel):
    width: int
    height: int


class Coordinates(DeckrModel):
    column: int
    row: int


class DeviceInfo(DeckrModel):
    name: str
    size: Size
    type: str


class ImageFormat(DeckrModel):
    width: int
    height: int
    format: str
    rotation: int | None


class Control(DeckrModel):
    name: str
    type: str
    image_format: ImageFormat | None = None


class Layout(DeckrModel):
    name: str
    controls: tuple[Control, ...]


class SlotInfo(DeckrModel):
    slot_id: str
    slot_type: str
    coordinates: Coordinates | None = None
    gestures: tuple[str, ...] = Field(default_factory=tuple)
    image_format: ImageFormat | None = None


class PluginEvent(DeckrModel):
    pass


class DeviceDidConnect(DeckrModel):
    event: Literal["deviceDidConnect"] = "deviceDidConnect"
    device: str = Field(description="The ID of the device that connected")
    device_info: DeviceInfo
    layout: Layout | None


class DeviceDidDisconnect(DeckrModel):
    event: Literal["deviceDidDisconnect"] = "deviceDidDisconnect"
    device: str


class WillAppear(DeckrModel):
    event: Literal["willAppear"] = "willAppear"
    context: str
    slot: SlotInfo


class WillDisappear(DeckrModel):
    event: Literal["willDisappear"] = "willDisappear"
    context: str
    slot_id: str


class KeyUp(DeckrModel):
    event: Literal["keyUp"] = "keyUp"
    context: str
    slot_id: str


class KeyDown(DeckrModel):
    event: Literal["keyDown"] = "keyDown"
    context: str
    slot_id: str


class DialRotate(DeckrModel):
    event: Literal["dialRotate"] = "dialRotate"
    context: str
    slot_id: str
    direction: Literal["clockwise", "counterclockwise"]


class TouchTap(DeckrModel):
    event: Literal["touchTap"] = "touchTap"
    context: str
    slot_id: str


class TouchSwipe(DeckrModel):
    event: Literal["touchSwipe"] = "touchSwipe"
    context: str
    slot_id: str
    direction: Literal["left", "right"]


class PageAppear(DeckrModel):
    event: Literal["pageAppear"] = "pageAppear"
    context: str
    page_id: str
    timeout_ms: int | None = None


class PageDisappear(DeckrModel):
    event: Literal["pageDisappear"] = "pageDisappear"
    context: str
    page_id: str
    reason: str | None = None
