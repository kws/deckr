"""Elgato-aligned plugin contracts.

This module defines the minimum action/runtime surface that a "controller-lite"
implementation should expose. The intent is to stay close to the Stream Deck
plugin protocol rather than Deckr-specific controller features.
"""

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


class CorePluginContext(Protocol):
    """Elgato-like action context for a controller-lite runtime.

    `set_image` intentionally follows the Stream Deck `setImage` model: the
    supplied string is an image reference for the action instance, typically a
    local plugin-relative path or a data URI / base64-encoded image string.
    """

    async def set_title(
        self,
        text: str,
        state: int | None = None,
        *,
        slot: str | None = None,
    ) -> None: ...
    async def set_image(
        self,
        image: str,
        state: int | None = None,
        *,
        slot: str | None = None,
    ) -> None: ...
    async def set_state(self, state: int) -> None: ...
    async def show_alert(self, *, slot: str | None = None) -> None: ...
    async def show_ok(self, *, slot: str | None = None) -> None: ...
    async def get_settings(self) -> SimpleNamespace: ...
    async def set_settings(self, settings: dict) -> None: ...
    async def open_url(self, url: str) -> None: ...
    async def switch_to_profile(
        self,
        *,
        profile: str = "default",
        page: int = 0,
    ) -> None: ...


class CorePluginAction(Protocol):
    """Core action protocol aligned with the standard Stream Deck lifecycle."""

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
    """Optional Stream Deck dial/touch hooks supported by richer devices."""

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
