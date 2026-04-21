"""Re-exports of core lifecycle types for plugin implementations.

Plugins should import BaseComponent, RunContext, AsyncMap, etc. from deckr.plugin
to satisfy the plugin-only dependency rule.
"""

from deckr.core.component import (
    BaseComponent,
    ComponentLifecycleEvent,
    ComponentLifecycleEventType,
    ComponentManager,
    ComponentState,
    RunContext,
    RunningComponent,
)
from deckr.core.util.anyio import (
    AsyncMap,
    EnsureStarted,
    SubscribableQueue,
)

__all__ = [
    "AsyncMap",
    "BaseComponent",
    "ComponentLifecycleEvent",
    "ComponentLifecycleEventType",
    "ComponentManager",
    "ComponentState",
    "EnsureStarted",
    "RunContext",
    "RunningComponent",
    "SubscribableQueue",
]
