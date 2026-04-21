from __future__ import annotations

from typing import Annotated, Literal

from deckr.core.util.pydantic import CamelModel, JsonObject
from pydantic import BaseModel, Field


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


class MultiActionPayload(CamelModel):
    controller: Literal["Keypad"]
    is_in_multi_action: Literal[True] = True
    resources: dict[str, str]
    settings: JsonObject
    state: int | None = None


class SingleActionPayload(CamelModel):
    controller: Literal["Encoder", "Keypad"]
    is_in_multi_action: Literal[False] = False
    coordinates: Coordinates
    resources: dict[str, str]
    settings: JsonObject
    state: int | None = None


WillAppearPayload = Annotated[
    MultiActionPayload | SingleActionPayload,
    Field(discriminator="is_in_multi_action"),
]


class WillAppear(BaseModel):
    event: Literal["willAppear"] = "willAppear"
    action: str
    context: str
    device: str
    payload: WillAppearPayload


class WillDisappear(BaseModel):
    event: Literal["willDisappear"] = "willDisappear"
    action: str
    context: str
    device: str


class KeyUp(BaseModel):
    event: Literal["keyUp"] = "keyUp"
    context: str
    key: str
    coordinates: Coordinates | None = None


class KeyDown(BaseModel):
    event: Literal["keyDown"] = "keyDown"
    context: str
    key: str
    coordinates: Coordinates | None = None


class DialRotate(BaseModel):
    event: Literal["dialRotate"] = "dialRotate"
    context: str
    dial: str
    coordinates: Coordinates | None = None
    direction: Literal["clockwise", "counterclockwise"]


class TouchTap(BaseModel):
    event: Literal["touchTap"] = "touchTap"
    context: str
    touch: str
    coordinates: Coordinates | None = None


class TouchSwipe(BaseModel):
    event: Literal["touchSwipe"] = "touchSwipe"
    context: str
    touch: str
    direction: Literal["left", "right"]
    coordinates: Coordinates | None = None


class PageAppear(CamelModel):
    event: Literal["pageAppear"] = "pageAppear"
    action: str
    context: str
    device: str
    page_id: str
    owner_profile: str | None = None
    owner_page: int | None = None
    timeout_ms: int | None = None
    settings: JsonObject | None = None


class PageDisappear(CamelModel):
    event: Literal["pageDisappear"] = "pageDisappear"
    action: str
    context: str
    device: str
    page_id: str
    reason: str | None = None
