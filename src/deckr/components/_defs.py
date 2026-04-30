from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


class ReadinessState(StrEnum):
    """Runtime-local component readiness states."""

    UNKNOWN = "unknown"
    READY = "ready"
    UNREADY = "unready"


@dataclass(frozen=True)
class ComponentStatus:
    """Runtime-local component lifecycle and readiness snapshot."""

    runtime_name: str
    lifecycle_state: ComponentState
    readiness_state: ReadinessState = ReadinessState.UNKNOWN
    readiness_reasons: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)


class ComponentStatusReporter(Protocol):
    async def report(
        self,
        readiness_state: ReadinessState,
        *,
        reasons: Sequence[str] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> None: ...


@dataclass
class RunContext:
    tg: anyio.abc.TaskGroup
    stopping: anyio.Event
    status: ComponentStatusReporter | None = None

    def start_task(self, func, *args, name: str | None = None) -> None:
        """Start a task in the task group."""
        self.tg.start_soon(func, *args, name=name)

    async def report_status(
        self,
        readiness_state: ReadinessState,
        *,
        reasons: Sequence[str] = (),
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        """Report runtime-local readiness for this component."""
        if self.status is None:
            return
        await self.status.report(
            readiness_state,
            reasons=reasons,
            diagnostics=diagnostics,
        )

    async def report_ready(
        self,
        *,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        await self.report_status(ReadinessState.READY, diagnostics=diagnostics)

    async def report_unready(
        self,
        *reasons: str,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        await self.report_status(
            ReadinessState.UNREADY,
            reasons=reasons,
            diagnostics=diagnostics,
        )

    async def report_readiness_unknown(
        self,
        *,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        await self.report_status(ReadinessState.UNKNOWN, diagnostics=diagnostics)


@runtime_checkable
class Component(Protocol):
    """A runtime participant that can be started and stopped."""

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
    readiness_state: ReadinessState = ReadinessState.UNKNOWN
    readiness_reasons: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ComponentLifecycleEvent:
    component: Component
    event_type: ComponentLifecycleEventType
