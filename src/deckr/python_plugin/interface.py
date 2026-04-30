"""Python plugin SDK contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from deckr.hardware.descriptors import CapabilityRef
from deckr.pluginhost.messages import (
    ActionDescriptor,
    ActionInstanceMetadata,
    BindingMetadata,
    CapabilityInputEvent,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageSessionMetadata,
)

JsonSettings = Mapping[str, Any]


class ScopedTasks(Protocol):
    """Task nursery scoped to a plugin, action instance, page session, or binding."""

    def start_soon(
        self,
        func: Callable[..., Awaitable[Any] | Any],
        *args: Any,
        name: object = None,
    ) -> None: ...

    async def cancel_all(self) -> None: ...


class ActionInstanceContext(Protocol):
    """Controller-owned action instance metadata and settings."""

    metadata: ActionInstanceMetadata
    settings: JsonSettings
    tasks: ScopedTasks


class ControlBinding(Protocol):
    """One active lease between an action instance or page session and a control."""

    metadata: BindingMetadata
    settings: JsonSettings
    tasks: ScopedTasks

    async def output(
        self,
        capability: CapabilityRef,
        command_type: str,
        params: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def set_raster_frame(
        self,
        frame: bytes | str,
        *,
        capability: CapabilityRef | None = None,
    ) -> None: ...

    async def clear(
        self,
        *,
        capability: CapabilityRef | None = None,
    ) -> None: ...
    async def open_page(self, descriptor: DynamicPageCommand) -> None: ...


class DynamicPageSession(Protocol):
    """One concrete dynamic page session owned by an action instance."""

    metadata: PageSessionMetadata
    tasks: ScopedTasks
    bindings: Sequence[ControlBinding]

    async def update(self, bindings: Sequence[PageChildBindingDescriptor]) -> None: ...
    async def replace(self, bindings: Sequence[PageChildBindingDescriptor]) -> None: ...
    async def close(self) -> None: ...


class ActionInstance(Protocol):
    """Capability-native Python action object."""

    async def on_bind(self, binding: ControlBinding) -> None: ...
    async def on_unbind(self, binding: ControlBinding, reason: str) -> None: ...
    async def on_input(
        self,
        binding: ControlBinding,
        event: CapabilityInputEvent,
    ) -> None: ...
    async def on_page_opened(self, page: DynamicPageSession) -> None: ...
    async def on_page_closed(
        self,
        page: DynamicPageSession,
        reason: str,
    ) -> None: ...


class ActionFactory(Protocol):
    """Factory for one action type advertised by a Python plugin."""

    descriptor: ActionDescriptor

    async def create(self, context: ActionInstanceContext) -> ActionInstance: ...


class PluginProvider(Protocol):
    """Capability-native Python plugin provider."""

    async def actions(self) -> Sequence[ActionDescriptor]: ...
    async def create_action(
        self,
        action_id: str,
        context: ActionInstanceContext,
    ) -> ActionInstance | None: ...

CAPABILITY_PAGES = "deckr.pages"
CAPABILITY_SCREEN_POWER = "deckr.screen-power"

PLUGIN_CAPABILITIES = frozenset(
    {
        CAPABILITY_PAGES,
        CAPABILITY_SCREEN_POWER,
    }
)
