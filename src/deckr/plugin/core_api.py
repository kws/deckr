"""Core plugin contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Protocol

from deckr.plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
    WillAppear,
    WillDisappear,
)
from deckr.plugin.messages import TitleOptions


class CorePluginContext(Protocol):
    """Action context for a controller runtime."""

    async def set_title(
        self,
        text: str,
        *,
        title_options: TitleOptions | None = None,
        slot: str | None = None,
    ) -> None: ...
    async def set_image(
        self,
        image: str,
        *,
        slot: str | None = None,
    ) -> None: ...
    async def show_alert(self, *, slot: str | None = None) -> None: ...
    async def show_ok(self, *, slot: str | None = None) -> None: ...
    async def get_settings(self) -> SimpleNamespace: ...
    async def set_settings(self, settings: dict) -> SimpleNamespace: ...


class CorePluginAction(Protocol):
    """Core action lifecycle protocol."""

    uuid: str

    async def on_will_appear(
        self, event: WillAppear, context: CorePluginContext
    ) -> None: ...
    async def on_will_disappear(
        self, event: WillDisappear, context: CorePluginContext
    ) -> None: ...
    async def on_key_up(self, event: KeyUp, context: CorePluginContext) -> None: ...
    async def on_key_down(self, event: KeyDown, context: CorePluginContext) -> None: ...


class CoreDialAndTouchAction(Protocol):
    """Optional dial/touch hooks supported by richer devices."""

    async def on_dial_rotate(
        self, event: DialRotate, context: CorePluginContext
    ) -> None: ...
    async def on_touch_tap(
        self, event: TouchTap, context: CorePluginContext
    ) -> None: ...
    async def on_touch_swipe(
        self, event: TouchSwipe, context: CorePluginContext
    ) -> None: ...


class CorePlugin(Protocol):
    """Core plugin provider protocol."""

    async def provides_actions(self) -> list[str]: ...
    async def get_action(self, uuid: str) -> CorePluginAction | None: ...
