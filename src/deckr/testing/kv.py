from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio

from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvChange, KvConflict, KvEntry, kv_value


class MemoryJsonKvBucket:
    """Deterministic in-memory JSON KV with exact CAS and bounded watches."""

    def __init__(
        self,
        *,
        bucket: str,
        buffer_size: int = 100,
        ttl_seconds: float | None = None,
    ) -> None:
        self.bucket = bucket
        self._buffer_size = buffer_size
        self._ttl_seconds = ttl_seconds
        self._revision = 0
        self._mutation_count = 0
        self._generation = 0
        self._entries: dict[str, KvEntry] = {}
        self._watchers: dict[anyio.abc.ObjectSendStream[KvChange | None], str] = {}
        self._subscribers: set[anyio.abc.ObjectSendStream[KvChange]] = set()
        self._lock = anyio.Lock()
        self._ready = anyio.Event()
        self._ready.set()
        self._current = True
        self._current_event = anyio.Event()
        self._current_event.set()
        self._watch_count = 0
        self._subscription_count = 0
        self._start_count = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def mutation_count(self) -> int:
        return self._mutation_count

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def watch_count(self) -> int:
        return self._watch_count

    @property
    def active_watch_count(self) -> int:
        return len(self._watchers)

    @property
    def subscription_count(self) -> int:
        return self._subscription_count

    @property
    def active_subscription_count(self) -> int:
        return len(self._subscribers)

    @property
    def start_count(self) -> int:
        return self._start_count

    def revision_for(self, key: str) -> int | None:
        entry = self._entries.get(key)
        return entry.revision if entry is not None else None

    async def ttl_seconds(self) -> float | None:
        return self._ttl_seconds

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        del task_group
        self._start_count += 1

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def is_current(self) -> bool:
        return self._current

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def wait_current(self) -> None:
        await self._current_event.wait()

    def mark_stale(self) -> None:
        self._current = False
        self._current_event = anyio.Event()

    def mark_current(self) -> None:
        self._current = True
        self._current_event.set()

    async def get(self, key: str) -> KvEntry | None:
        async with self._lock:
            return self._entries.get(key)

    async def get_exact(self, key: str) -> KvEntry | None:
        return await self.get(key)

    def get_cached(self, key: str) -> KvEntry | None:
        return self._entries.get(key)

    async def items(self, prefix: str = "") -> tuple[KvEntry, ...]:
        async with self._lock:
            return self.items_cached(prefix)

    async def items_exact(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return await self.items(prefix)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return tuple(
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(prefix)
        )

    def revision_cached(self, key: str) -> int | None:
        return self.revision_for(key)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        entry, watchers, subscribers = await self._write(key, value)
        await self._publish(
            watchers,
            subscribers,
            KvChange(
                self.bucket,
                key,
                entry.revision,
                "put",
                entry,
                view_generation=self._generation,
            ),
        )
        return entry

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        normalized = kv_value(value)
        async with self._lock:
            if key in self._entries:
                raise KvConflict(f"KV key {key!r} already exists")
            entry = self._next_entry(key, normalized)
            self._entries[key] = entry
            watchers = self._watchers_for(key)
            subscribers = tuple(self._subscribers)
        await self._publish(
            watchers,
            subscribers,
            KvChange(
                self.bucket,
                key,
                entry.revision,
                "put",
                entry,
                view_generation=self._generation,
            ),
        )
        return entry

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        normalized = kv_value(value)
        async with self._lock:
            current = self._entries.get(key)
            if current is None or current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
            entry = self._next_entry(key, normalized)
            self._entries[key] = entry
            watchers = self._watchers_for(key)
            subscribers = tuple(self._subscribers)
        await self._publish(
            watchers,
            subscribers,
            KvChange(
                self.bucket,
                key,
                entry.revision,
                "put",
                entry,
                view_generation=self._generation,
            ),
        )
        return entry

    async def delete(self, key: str, *, revision: int | None = None) -> int | None:
        async with self._lock:
            current = self._entries.get(key)
            if current is None:
                return None
            if revision is not None and current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
            self._advance_mutation()
            self._entries.pop(key, None)
            delete_revision = self._revision
            watchers = self._watchers_for(key)
            subscribers = tuple(self._subscribers)
        await self._publish(
            watchers,
            subscribers,
            KvChange(
                self.bucket,
                key,
                delete_revision,
                "delete",
                view_generation=self._generation,
            ),
        )
        return delete_revision

    async def expire(self, key: str) -> bool:
        async with self._lock:
            current = self._entries.pop(key, None)
            if current is None:
                return False
            self._advance_mutation()
            expire_revision = self._revision
            watchers = self._watchers_for(key)
            subscribers = tuple(self._subscribers)
        await self._publish(
            watchers,
            subscribers,
            KvChange(
                self.bucket,
                key,
                expire_revision,
                "expire",
                view_generation=self._generation,
            ),
        )
        return True

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        send, receive = anyio.create_memory_object_stream[KvChange | None](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._watch_count += 1
            self._watchers[send] = prefix
            snapshot = self.items_cached(prefix)
        for entry in snapshot:
            await send.send(KvChange(self.bucket, entry.key, entry.revision, "put", entry))
        await send.send(None)
        try:
            async with send, receive:
                yield receive
        finally:
            async with self._lock:
                self._watchers.pop(send, None)

    async def _write(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
    ) -> tuple[
        KvEntry,
        tuple[anyio.abc.ObjectSendStream[KvChange | None], ...],
        tuple[anyio.abc.ObjectSendStream[KvChange], ...],
    ]:
        normalized = kv_value(value)
        async with self._lock:
            entry = self._next_entry(key, normalized)
            self._entries[key] = entry
            return entry, self._watchers_for(key), tuple(self._subscribers)

    def _next_entry(self, key: str, value: Mapping[str, Any]) -> KvEntry:
        self._advance_mutation()
        return KvEntry(self.bucket, key, value, self._revision)

    def _advance_mutation(self) -> None:
        self._revision += 1
        self._mutation_count += 1
        self._generation += 1

    def _watchers_for(
        self,
        key: str,
    ) -> tuple[anyio.abc.ObjectSendStream[KvChange | None], ...]:
        return tuple(
            stream for stream, prefix in self._watchers.items() if key.startswith(prefix)
        )

    async def _publish(
        self,
        watchers: tuple[anyio.abc.ObjectSendStream[KvChange | None], ...],
        subscribers: tuple[anyio.abc.ObjectSendStream[KvChange], ...],
        change: KvChange,
    ) -> None:
        for watcher in watchers:
            await watcher.send(change)
        for subscriber in subscribers:
            await subscriber.send(change)
