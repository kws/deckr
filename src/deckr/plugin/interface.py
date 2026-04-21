from typing import Protocol

from deckr.plugin.core_api import CorePlugin, CorePluginAction, CorePluginContext
from deckr.plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
)
from deckr.plugin.extensions import DeckrPluginContextExtensions


class ControlContext(Protocol):
    async def on_will_appear(self): ...
    async def on_will_disappear(self): ...
    async def on_key_up(self, event: KeyUp): ...
    async def on_key_down(self, event: KeyDown): ...
    async def on_dial_rotate(self, event: DialRotate): ...
    async def on_touch_tap(self, event: TouchTap): ...
    async def on_touch_swipe(self, event: TouchSwipe): ...


class PluginContext(CorePluginContext, DeckrPluginContextExtensions, Protocol):
    """Compatibility aggregate of the core contract plus Deckr extensions."""


class PluginAction(CorePluginAction, Protocol):
    """Compatibility aggregate for actions used by the current controller.

    Actions may have manifest_state_defaults (dict[int, StateOverride] | None) set by
    the plugin provider for render resolution. Use getattr(action, "manifest_state_defaults", None).
    """

    # Optional: on_touch_tap(event: TouchTap, context: PluginContext)
    # Optional: on_touch_swipe(event: TouchSwipe, context: PluginContext)
    # Optional: on_page_appear(event: PageAppear, context: PluginContext)
    # Optional: on_page_disappear(event: PageDisappear, context: PluginContext)


class Plugin(CorePlugin, Protocol):
    """Compatibility aggregate for plugin providers used by the current controller."""
