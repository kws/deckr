"""Deterministic fault injection for authority lifecycle acceptance tests."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import anyio

from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvEntry, KvUnavailable
from deckr.testing.kv import MemoryJsonKvBucket


class LifecycleFaultPhase(StrEnum):
    BEFORE = "before"
    AFTER = "after"


@dataclass(slots=True)
class LifecycleOperationBarrier:
    """One-shot barrier reached immediately before or after an operation."""

    entered: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)

    async def wait(self) -> None:
        await self.entered.wait()

    def unblock(self) -> None:
        self.release.set()


@dataclass(slots=True)
class _LifecycleFault:
    phase: LifecycleFaultPhase
    key_contains: str | None
    error: BaseException | None
    barrier: LifecycleOperationBarrier | None


class LifecycleVirtualClock:
    """Deterministic clock and jitter source used by retry tests."""

    def __init__(self, *, jitter_ratios: tuple[float, ...] = (0.5,)) -> None:
        if not jitter_ratios or any(not 0.0 <= value <= 1.0 for value in jitter_ratios):
            raise ValueError("jitter ratios must contain values between zero and one")
        self.now = 0.0
        self.sleeps: list[float] = []
        self._jitter = deque(jitter_ratios)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await anyio.lowlevel.checkpoint()

    def uniform(self, low: float, high: float) -> float:
        ratio = self._jitter[0]
        self._jitter.rotate(-1)
        return low + ((high - low) * ratio)


class LifecycleFaultHarness(MemoryJsonKvBucket):
    """Memory KV with one-shot before/after faults and operation barriers.

    An ``after`` failure models a server commit whose acknowledgement was lost:
    the underlying mutation and watch notification happen before the configured
    exception is raised. Operation counts include attempted reads and writes;
    ``mutation_count`` inherited from :class:`MemoryJsonKvBucket` counts only
    committed mutations.
    """

    def __init__(
        self,
        *,
        bucket: str,
        buffer_size: int = 100,
        ttl_seconds: float | None = None,
    ) -> None:
        super().__init__(
            bucket=bucket,
            buffer_size=buffer_size,
            ttl_seconds=ttl_seconds,
        )
        self.operation_counts: defaultdict[str, int] = defaultdict(int)
        self._faults: defaultdict[str, deque[_LifecycleFault]] = defaultdict(deque)

    def fail_next(
        self,
        operation: str,
        *,
        phase: LifecycleFaultPhase | str,
        key_contains: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._faults[operation].append(
            _LifecycleFault(
                LifecycleFaultPhase(phase),
                key_contains,
                error or KvUnavailable(f"{operation} acknowledgement unavailable"),
                None,
            )
        )

    def block_next(
        self,
        operation: str,
        *,
        phase: LifecycleFaultPhase | str,
        key_contains: str | None = None,
        error: BaseException | None = None,
    ) -> LifecycleOperationBarrier:
        barrier = LifecycleOperationBarrier()
        self._faults[operation].append(
            _LifecycleFault(
                LifecycleFaultPhase(phase),
                key_contains,
                error,
                barrier,
            )
        )
        return barrier

    async def get(self, key: str) -> KvEntry | None:
        await self._trip("get", LifecycleFaultPhase.BEFORE, key)
        result = await super().get(key)
        await self._trip("get", LifecycleFaultPhase.AFTER, key)
        return result

    async def get_exact(self, key: str) -> KvEntry | None:
        await self._trip("get_exact", LifecycleFaultPhase.BEFORE, key)
        result = await super().get(key)
        await self._trip("get_exact", LifecycleFaultPhase.AFTER, key)
        return result

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        await self._trip("create", LifecycleFaultPhase.BEFORE, key)
        result = await super().create(key, value, ttl=ttl)
        await self._trip("create", LifecycleFaultPhase.AFTER, key)
        return result

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        await self._trip("update", LifecycleFaultPhase.BEFORE, key)
        result = await super().update(key, value, revision=revision, ttl=ttl)
        await self._trip("update", LifecycleFaultPhase.AFTER, key)
        return result

    async def delete(self, key: str, *, revision: int | None = None) -> int | None:
        await self._trip("delete", LifecycleFaultPhase.BEFORE, key)
        result = await super().delete(key, revision=revision)
        await self._trip("delete", LifecycleFaultPhase.AFTER, key)
        return result

    @asynccontextmanager
    async def watch(self, prefix: str = "") -> AsyncIterator[Any]:
        await self._trip("watch", LifecycleFaultPhase.BEFORE, prefix)
        async with super().watch(prefix) as changes:
            await self._trip("watch", LifecycleFaultPhase.AFTER, prefix)
            yield changes

    async def _trip(
        self,
        operation: str,
        phase: LifecycleFaultPhase,
        key: str,
    ) -> None:
        if phase == LifecycleFaultPhase.BEFORE:
            self.operation_counts[operation] += 1
        queued = self._faults[operation]
        if not queued:
            return
        fault = queued[0]
        if fault.phase != phase or (
            fault.key_contains is not None and fault.key_contains not in key
        ):
            return
        queued.popleft()
        if fault.barrier is not None:
            fault.barrier.entered.set()
            await fault.barrier.release.wait()
        if fault.error is not None:
            raise fault.error


__all__ = [
    "LifecycleFaultHarness",
    "LifecycleFaultPhase",
    "LifecycleOperationBarrier",
    "LifecycleVirtualClock",
]
