"""Deckr-specific plugin extensions beyond the core contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from deckr.python_plugin.events import PageAppear, PageDisappear

if TYPE_CHECKING:
    from deckr.python_plugin.core_api import CorePluginContext
    from deckr.pluginhost.messages import DynamicPageDescriptor

CAPABILITY_PAGES = "deckr.pages"
CAPABILITY_SCREEN_POWER = "deckr.screen-power"

DECKR_EXTENSION_CAPABILITIES = frozenset(
    {
        CAPABILITY_PAGES,
        CAPABILITY_SCREEN_POWER,
    }
)


class DeckrPluginContextExtensions(Protocol):
    """Optional Deckr controller features a richer controller may expose."""

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


class DeckrPageAwareAction(Protocol):
    """Optional page lifecycle hooks for Deckr dynamic-page controllers."""

    async def on_page_appear(
        self,
        event: PageAppear,
        context: CorePluginContext,
    ) -> None: ...
    async def on_page_disappear(
        self,
        event: PageDisappear,
        context: CorePluginContext,
    ) -> None: ...
