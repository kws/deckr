import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

import anyio

_PROCESS_NONCE = secrets.token_hex(3)


class ComponentState(StrEnum):
    """Component lifecycle states."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ComponentLifecycleEventType(StrEnum):
    """Component lifecycle event types."""

    ADDED = "added"
    STARTED = "started"  # emitted when state transitions to RUNNING
    REMOVED = "removed"
    CRASHED = "crashed"


@dataclass
class RunContext:
    tg: anyio.abc.TaskGroup
    stopping: anyio.Event

    def start_task(self, func, *args, name: str | None = None) -> None:
        """Start a task in the task group."""
        self.tg.start_soon(func, *args, name=name)


@runtime_checkable
class Component(Protocol):
    """A component is a service that can be started and stopped."""

    name: str

    async def start(self, ctx: RunContext) -> None:
        """Schedule background tasks into ctx.tg and return promptly."""
        ...

    async def stop(self) -> None:
        """Request graceful shutdown; should be idempotent."""
        ...


class BaseComponent(ABC):
    """Convenience base class that provides a default name."""

    name: str

    def __init__(self, name: str | None = None) -> None:
        if name is None:
            name = f"{self.__class__.__name__}:{_PROCESS_NONCE}:{id(self)}"
        self.name = name

    @abstractmethod
    async def start(self, ctx: RunContext) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...


@dataclass
class RunningComponent:
    component: Component
    stopping: anyio.Event
    cancel_scope: anyio.CancelScope
    state: ComponentState = ComponentState.IDLE


@dataclass(frozen=True)
class ComponentLifecycleEvent:
    plugin: Component
    event_type: ComponentLifecycleEventType
