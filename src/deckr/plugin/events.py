from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from deckr.core.util.pydantic import CamelModel


class Size(BaseModel):
    width: int
    height: int


class Coordinates(BaseModel):
    column: int
    row: int


class DeviceInfo(BaseModel):
    name: str
    size: Size
    type: str


class ImageFormat(BaseModel):
    width: int
    height: int
    format: str
    rotation: int | None


class Control(BaseModel):
    name: str
    type: str
    image_format: ImageFormat | None = None


class Layout(BaseModel):
    name: str
    controls: list[Control]


class SlotInfo(CamelModel):
    slot_id: str
    slot_type: str
    coordinates: Coordinates | None = None
    gestures: list[str] = Field(default_factory=list)
    image_format: ImageFormat | None = None


class PluginEvent(BaseModel):
    pass


class DeviceDidConnect(CamelModel):
    event: Literal["deviceDidConnect"] = "deviceDidConnect"
    device: str = Field(description="The ID of the device that connected")
    device_info: DeviceInfo
    layout: Layout | None


class DeviceDidDisconnect(BaseModel):
    event: Literal["deviceDidDisconnect"] = "deviceDidDisconnect"
    device: str


class WillAppear(CamelModel):
    event: Literal["willAppear"] = "willAppear"
    context: str
    slot: SlotInfo


class WillDisappear(CamelModel):
    event: Literal["willDisappear"] = "willDisappear"
    context: str
    slot_id: str


class KeyUp(CamelModel):
    event: Literal["keyUp"] = "keyUp"
    context: str
    slot_id: str


class KeyDown(CamelModel):
    event: Literal["keyDown"] = "keyDown"
    context: str
    slot_id: str


class DialRotate(CamelModel):
    event: Literal["dialRotate"] = "dialRotate"
    context: str
    slot_id: str
    direction: Literal["clockwise", "counterclockwise"]


class TouchTap(CamelModel):
    event: Literal["touchTap"] = "touchTap"
    context: str
    slot_id: str


class TouchSwipe(CamelModel):
    event: Literal["touchSwipe"] = "touchSwipe"
    context: str
    slot_id: str
    direction: Literal["left", "right"]


class PageAppear(CamelModel):
    event: Literal["pageAppear"] = "pageAppear"
    context: str
    page_id: str
    timeout_ms: int | None = None


class PageDisappear(CamelModel):
    event: Literal["pageDisappear"] = "pageDisappear"
    context: str
    page_id: str
    reason: str | None = None
