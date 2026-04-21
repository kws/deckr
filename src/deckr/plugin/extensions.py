"""Deckr-specific plugin extensions beyond the Elgato-aligned core contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from deckr.plugin.events import PageAppear, PageDisappear

if TYPE_CHECKING:
    from deckr.plugin.core_api import CorePluginContext
    from deckr.plugin.types import DynamicPageDescriptor

CAPABILITY_PAGES = "deckr.pages"
CAPABILITY_IMAGE_GRAPH = "deckr.image-graph"
CAPABILITY_SCREEN_POWER = "deckr.screen-power"

DECKR_EXTENSION_CAPABILITIES = frozenset(
    {
        CAPABILITY_PAGES,
        CAPABILITY_IMAGE_GRAPH,
        CAPABILITY_SCREEN_POWER,
    }
)


class DeckrPluginContextExtensions(Protocol):
    """Optional Deckr controller features a richer controller may expose."""

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
