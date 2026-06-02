from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from deckr.substrates.nats_kv import (
    KvBucketPolicy,
    KvChange,
    KvEntry,
    NatsJsonKvBucket,
    NatsKvMaterializedBucket,
    kv_value,
)


@pytest.mark.asyncio
async def test_nats_json_kv_creates_bucket_with_policy_ttl() -> None:
    fake_js = _FakeJs(existing=False)
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_beacon_advertisement_v1",
            ttl_seconds=30.0,
            allow_write_ttl=True,
        ),
    )

    entry = await bucket.put("advertisements.by_feature.hardware.deck", {"owner": "hw"})

    assert entry.bucket == "deckr_beacon_advertisement_v1"
    assert entry.value == {"owner": "hw"}
    assert fake_js.created_config is not None
    assert fake_js.created_config.bucket == "deckr_beacon_advertisement_v1"
    assert fake_js.created_config.ttl == 30.0
    assert fake_js.created_config.history == 1


@pytest.mark.asyncio
async def test_nats_json_kv_updates_existing_bucket_policy() -> None:
    fake_js = _FakeJs(existing=True, max_age=None, allow_msg_ttl=False)
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_concord_token_v1",
            ttl_seconds=30.0,
            allow_write_ttl=True,
        ),
    )

    await bucket.put("contracts.main.1.participants.controller", {"token": "one"})

    assert fake_js.updated_config is not None
    assert fake_js.updated_config.max_age == 30.0
    assert fake_js.updated_config.max_msgs_per_subject == 1
    assert fake_js.updated_config.allow_msg_ttl is True


@pytest.mark.asyncio
async def test_nats_json_kv_watch_maps_put_delete_and_expire_markers() -> None:
    fake_js = _FakeJs()
    fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"open"}')
    fake_js.kv.add_marker(
        "contracts.main.1.participants.controller",
        operation="DEL",
    )
    fake_js.kv.add_marker(
        "contracts.main.1.participants.manager",
        headers={"Nats-Marker-Reason": "MaxAge"},
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    async with bucket.watch("contracts.") as changes:
        put = await changes.receive()
        deleted = await changes.receive()
        expired = await changes.receive()
        ready = await changes.receive()

    assert put is not None
    assert put.operation == "put"
    assert put.entry is not None
    assert put.entry.value == {"state": "open"}
    assert deleted is not None
    assert deleted.operation == "delete"
    assert expired is not None
    assert expired.operation == "expire"
    assert ready is None
    assert fake_js.deleted_consumers == [("KV_deckr_concord_contract_v1", "consumer-1")]


@pytest.mark.asyncio
async def test_materialized_bucket_reconciles_absent_keys_on_watch_recovery() -> None:
    raw = _RecoveringWatchBucket(bucket="recovering")
    raw.add("items.a", {"value": "a"})
    stale = raw.add("items.b", {"value": "b"})
    materialized = NatsKvMaterializedBucket(bucket=raw, key_prefix="items.")

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_ready()

        assert materialized.get_cached("items.b") == stale

        raw.remove_without_publish("items.b")
        raw.close_current_watch()

        with anyio.fail_after(1):
            while materialized.get_cached("items.b") is not None:
                await anyio.sleep(0)

        tombstone_revision = materialized.revision_cached("items.b")
        assert tombstone_revision == stale.revision + 1

        await materialized._apply_change(  # noqa: SLF001
            KvChange(raw.bucket, stale.key, stale.revision, "put", stale)
        )
        assert materialized.get_cached("items.b") is None
        assert materialized.revision_cached("items.b") == tombstone_revision

        fresh = raw.add("items.b", {"value": "fresh"})
        await materialized._apply_change(  # noqa: SLF001
            KvChange(raw.bucket, fresh.key, fresh.revision, "put", fresh)
        )
        assert materialized.get_cached("items.b") == fresh
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_bucket_cached_reads_do_not_scan_native_bucket() -> None:
    raw = _RecoveringWatchBucket(bucket="recovering")
    raw.add("items.a", {"value": "a"})
    materialized = NatsKvMaterializedBucket(bucket=raw, key_prefix="items.")

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_ready()
        get_count = raw.get_count
        watch_count = raw.watch_count

        assert materialized.get_cached("items.a") is not None
        assert materialized.items_cached("items.") == (materialized.get_cached("items.a"),)
        assert materialized.revision_cached("items.a") == 1

        assert raw.get_count == get_count
        assert raw.watch_count == watch_count
        task_group.cancel_scope.cancel()


class _RecoveringWatchBucket:
    def __init__(self, *, bucket: str) -> None:
        self.bucket = bucket
        self._revision = 0
        self._entries: dict[str, KvEntry] = {}
        self._close_events: list[anyio.Event] = []
        self.get_count = 0
        self.watch_count = 0

    def add(self, key: str, value: Mapping[str, Any]) -> KvEntry:
        self._revision += 1
        entry = KvEntry(self.bucket, key, kv_value(value), self._revision)
        self._entries[key] = entry
        return entry

    def remove_without_publish(self, key: str) -> None:
        if key in self._entries:
            self._revision += 1
            self._entries.pop(key)

    def close_current_watch(self) -> None:
        self._close_events[-1].set()

    async def get(self, key: str) -> KvEntry | None:
        self.get_count += 1
        return self._entries.get(key)

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        self.watch_count += 1
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[KvChange | None](100)
        snapshot = tuple(
            entry for key, entry in sorted(self._entries.items()) if key.startswith(prefix)
        )

        async def run() -> None:
            for entry in snapshot:
                await send.send(KvChange(self.bucket, entry.key, entry.revision, "put", entry))
            await send.send(None)
            await close_event.wait()
            await send.aclose()

        async with send, receive, anyio.create_task_group() as task_group:
            task_group.start_soon(run)
            yield receive
            task_group.cancel_scope.cancel()


class _FakeKvEntry:
    def __init__(
        self,
        *,
        key: str,
        value: bytes,
        revision: int,
        operation: str = "PUT",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.key = key
        self.value = value
        self.revision = revision
        self.operation = operation
        self.headers = headers or {}


class _FakeKv:
    def __init__(self, js: _FakeJs) -> None:
        self._js = js
        self._stream = f"KV_{js.bucket}"
        self._revision = 0
        self._entries: dict[str, _FakeKvEntry] = {}

    def add_entry(self, key: str, value: bytes) -> _FakeKvEntry:
        self._revision += 1
        entry = _FakeKvEntry(key=key, value=value, revision=self._revision)
        self._entries[key] = entry
        return entry

    def add_marker(
        self,
        key: str,
        *,
        operation: str = "PUT",
        headers: dict[str, str] | None = None,
    ) -> _FakeKvEntry:
        self._revision += 1
        entry = _FakeKvEntry(
            key=key,
            value=b"",
            revision=self._revision,
            operation=operation,
            headers=headers,
        )
        self._entries[key] = entry
        return entry

    async def get(self, key: str) -> _FakeKvEntry:
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("missing")
        return entry

    async def put(self, key: str, value: bytes) -> int:
        return self.add_entry(key, value).revision

    async def create(self, key: str, value: bytes) -> int:
        if key in self._entries:
            raise RuntimeError("wrong last")
        return await self.put(key, value)

    async def update(self, key: str, value: bytes, *, last: int) -> int:
        entry = self._entries.get(key)
        if entry is None or entry.revision != last:
            raise RuntimeError("revision changed")
        return await self.put(key, value)

    async def delete(self, key: str, *, last: int | None = None) -> None:
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("missing")
        if last is not None and entry.revision != last:
            raise RuntimeError("revision changed")
        self.add_marker(key, operation="DEL")

    async def watch(self, keys, **kwargs):
        del kwargs
        entries = [
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(keys.rstrip(">"))
        ]
        return _FakeKvWatcher(
            entries,
            _FakeSubscription(self._js),
        )


class _FakeSubscription:
    def __init__(self, js: _FakeJs) -> None:
        self._js = js
        self._stream = f"KV_{js.bucket}"
        self._consumer = js.next_consumer_name()

    async def unsubscribe(self) -> None:
        return None


class _FakeKvWatcher:
    def __init__(
        self,
        entries: list[_FakeKvEntry],
        subscription: _FakeSubscription,
    ) -> None:
        self._entries = [*entries, None]
        self._index = 0
        self._sub = subscription

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._entries):
            raise StopAsyncIteration
        item = self._entries[self._index]
        self._index += 1
        return item

    async def stop(self) -> None:
        self._index = len(self._entries)


class _FakeStreamConfig:
    def __init__(
        self,
        *,
        name: str,
        max_age: float | None,
        max_msgs_per_subject: int | None = 1,
        allow_msg_ttl: bool | None = True,
    ) -> None:
        self.name = name
        self.max_age = max_age
        self.max_msgs_per_subject = max_msgs_per_subject
        self.allow_msg_ttl = allow_msg_ttl


class _FakeStreamInfo:
    def __init__(self, config: _FakeStreamConfig) -> None:
        self.config = config


class _FakeJs:
    def __init__(
        self,
        *,
        existing: bool = True,
        max_age: float | None = None,
        allow_msg_ttl: bool | None = True,
    ) -> None:
        self.bucket = "deckr_concord_contract_v1"
        self.kv = _FakeKv(self) if existing else None
        self.config = _FakeStreamConfig(
            name=f"KV_{self.bucket}",
            max_age=max_age,
            allow_msg_ttl=allow_msg_ttl,
        )
        self.created_config = None
        self.updated_config = None
        self.deleted_consumers: list[tuple[str, str]] = []
        self._consumer_index = 0
        self._jsm = self

    def next_consumer_name(self) -> str:
        self._consumer_index += 1
        return f"consumer-{self._consumer_index}"

    async def key_value(self, bucket: str) -> _FakeKv:
        self.bucket = bucket
        if self.kv is None:
            raise RuntimeError("missing")
        self.kv._stream = f"KV_{bucket}"
        self.config.name = f"KV_{bucket}"
        return self.kv

    async def create_key_value(self, config=None, **params) -> _FakeKv:
        if config is not None:
            self.created_config = config
            self.bucket = config.bucket
            self.config = _FakeStreamConfig(
                name=f"KV_{config.bucket}",
                max_age=config.ttl,
                max_msgs_per_subject=config.history,
                allow_msg_ttl=True,
            )
        else:
            self.created_config = params
            self.bucket = params["bucket"]
            self.config = _FakeStreamConfig(
                name=f"KV_{self.bucket}",
                max_age=params.get("ttl"),
                max_msgs_per_subject=params.get("history"),
                allow_msg_ttl=True,
            )
        self.kv = _FakeKv(self)
        return self.kv

    async def stream_info(self, name: str) -> _FakeStreamInfo:
        assert name == self.config.name
        return _FakeStreamInfo(self.config)

    async def update_stream(self, config) -> None:
        self.updated_config = config
        self.config = config

    async def delete_consumer(self, stream: str, consumer: str) -> bool:
        self.deleted_consumers.append((stream, consumer))
        return True
