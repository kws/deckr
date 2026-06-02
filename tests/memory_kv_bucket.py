from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio

from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvChange, KvConflict, KvEntry, kv_value


class MemoryJsonKvBucket:
    def __init__(self, *, bucket: str, buffer_size: int = 100) -> None:
        self.bucket = bucket
        self._buffer_size = buffer_size
        self._revision = 0
        self._entries: dict[str, KvEntry] = {}
        self._watchers: dict[anyio.abc.ObjectSendStream[KvChange | None], str] = {}
        self._lock = anyio.Lock()

    async def get(self, key: str) -> KvEntry | None:
        async with self._lock:
            return self._entries.get(key)

    async def items(self, prefix: str = "") -> tuple[KvEntry, ...]:
        async with self._lock:
            return tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        entry, watchers = await self._write(key, value)
        await self._publish(watchers, KvChange(self.bucket, key, entry.revision, "put", entry))
        return entry

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        async with self._lock:
            if key in self._entries:
                raise KvConflict(f"KV key {key!r} already exists")
        return await self.put(key, value)

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        del ttl
        async with self._lock:
            current = self._entries.get(key)
            if current is None or current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
        return await self.put(key, value)

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        async with self._lock:
            current = self._entries.get(key)
            if current is None:
                return
            if revision is not None and current.revision != revision:
                raise KvConflict(f"KV key {key!r} revision changed")
            self._revision += 1
            self._entries.pop(key, None)
            delete_revision = self._revision
            watchers = self._watchers_for(key)
        await self._publish(
            watchers,
            KvChange(self.bucket, key, delete_revision, "delete"),
        )

    async def expire(self, key: str) -> None:
        async with self._lock:
            current = self._entries.pop(key, None)
            if current is None:
                return
            self._revision += 1
            expire_revision = self._revision
            watchers = self._watchers_for(key)
        await self._publish(
            watchers,
            KvChange(self.bucket, key, expire_revision, "expire"),
        )

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        send, receive = anyio.create_memory_object_stream[KvChange | None](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._watchers[send] = prefix
            snapshot = tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )
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
    ) -> tuple[KvEntry, tuple[anyio.abc.ObjectSendStream[KvChange | None], ...]]:
        normalized = kv_value(value)
        async with self._lock:
            self._revision += 1
            entry = KvEntry(self.bucket, key, normalized, self._revision)
            self._entries[key] = entry
            watchers = self._watchers_for(key)
        return entry, watchers

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
        change: KvChange,
    ) -> None:
        for watcher in watchers:
            await watcher.send(change)
