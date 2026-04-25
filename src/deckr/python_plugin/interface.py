"""Python plugin SDK contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Protocol

from deckr.pluginhost.messages import DynamicPageDescriptor, TitleOptions
from deckr.python_plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    PageAppear,
    PageDisappear,
    TouchSwipe,
    TouchTap,
    WillAppear,
    WillDisappear,
)

CAPABILITY_PAGES = "deckr.pages"
CAPABILITY_SCREEN_POWER = "deckr.screen-power"

PLUGIN_CAPABILITIES = frozenset(
    {
        CAPABILITY_PAGES,
        CAPABILITY_SCREEN_POWER,
    }
)


class ControlContext(Protocol):
    async def on_will_appear(self) -> None: ...
    async def on_will_disappear(self) -> None: ...
    async def on_key_up(self, event: KeyUp) -> None: ...
    async def on_key_down(self, event: KeyDown) -> None: ...
    async def on_dial_rotate(self, event: DialRotate) -> None: ...
    async def on_touch_tap(self, event: TouchTap) -> None: ...
    async def on_touch_swipe(self, event: TouchSwipe) -> None: ...


class PluginContext(Protocol):
    """Action context exposed to Python plugins."""

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
    async def set_page(
        self,
        *,
        profile: str = "default",
        page: int = 0,
    ) -> None: ...
    async def open_page(self, descriptor: DynamicPageDescriptor) -> None: ...
    async def close_page(self) -> None: ...
    async def sleep_screen(self) -> None: ...
    async def wake_screen(self) -> None: ...


class PluginAction(Protocol):
    """Deckr action protocol used by the current controller runtime."""

    uuid: str

    async def on_will_appear(
        self, event: WillAppear, context: PluginContext
    ) -> None: ...
    async def on_will_disappear(
        self, event: WillDisappear, context: PluginContext
    ) -> None: ...
    async def on_key_up(self, event: KeyUp, context: PluginContext) -> None: ...
    async def on_key_down(self, event: KeyDown, context: PluginContext) -> None: ...


class DialAndTouchAction(Protocol):
    """Optional dial/touch hooks supported by richer devices."""

    async def on_dial_rotate(
        self, event: DialRotate, context: PluginContext
    ) -> None: ...
    async def on_touch_tap(self, event: TouchTap, context: PluginContext) -> None: ...
    async def on_touch_swipe(
        self, event: TouchSwipe, context: PluginContext
    ) -> None: ...


class PageAwareAction(Protocol):
    """Optional page lifecycle hooks for dynamic-page controllers."""

    async def on_page_appear(
        self,
        event: PageAppear,
        context: PluginContext,
    ) -> None: ...
    async def on_page_disappear(
        self,
        event: PageDisappear,
        context: PluginContext,
    ) -> None: ...


class Plugin(Protocol):
    """Deckr plugin provider protocol."""

    async def provides_actions(self) -> list[str]: ...
    async def get_action(self, uuid: str) -> PluginAction | None: ...
