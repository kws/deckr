from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.substrates.nats_kv import (
    KvBucketPolicy,
    KvChange,
    KvConflict,
    KvEntry,
    KvViewStatus,
    NatsJsonKvBucket,
    NatsKvMaterializedBucket,
    kv_entry_is_absent_marker,
    kv_value,
)


@pytest.mark.asyncio
async def test_nats_json_kv_creates_bucket_with_policy_ttl() -> None:
    fake_js = _FakeJs(existing=False)
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_beacon_advertisement_v1",
            ttl_seconds=300.0,
            allow_write_ttl=True,
        ),
    )

    entry = await bucket.put("advertisements.by_feature.hardware.deck", {"owner": "hw"})

    assert entry.bucket == "deckr_beacon_advertisement_v1"
    assert entry.value == {"owner": "hw"}
    assert fake_js.created_config is not None
    assert fake_js.created_config.bucket == "deckr_beacon_advertisement_v1"
    assert fake_js.created_config.ttl == 300.0
    assert fake_js.created_config.history == 1
    assert fake_js.updated_raw_config is not None
    assert fake_js.updated_raw_config["max_age"] == 300_000_000_000
    assert fake_js.updated_raw_config["subject_delete_marker_ttl"] == 300_000_000_000


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

    assert fake_js.updated_raw_config is not None
    assert fake_js.updated_raw_config["max_age"] == 30_000_000_000
    assert fake_js.updated_raw_config["max_msgs_per_subject"] == 1
    assert fake_js.updated_raw_config["allow_msg_ttl"] is True
    assert fake_js.updated_raw_config["subject_delete_marker_ttl"] == 30_000_000_000


@pytest.mark.asyncio
async def test_nats_json_kv_updates_existing_ttl_bucket_missing_delete_markers() -> None:
    fake_js = _FakeJs(
        existing=True,
        max_age=300.0,
        allow_msg_ttl=True,
        subject_delete_marker_ttl=None,
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_beacon_advertisement_v1",
            ttl_seconds=300.0,
            allow_write_ttl=True,
        ),
    )

    await bucket.put("advertisements.by_feature.hardware.deck", {"owner": "hw"})

    assert fake_js.updated_raw_config is not None
    assert fake_js.updated_raw_config["subject_delete_marker_ttl"] == 300_000_000_000


@pytest.mark.asyncio
async def test_nats_json_kv_keeps_existing_ttl_bucket_with_delete_markers() -> None:
    fake_js = _FakeJs(
        existing=True,
        max_age=300.0,
        allow_msg_ttl=True,
        subject_delete_marker_ttl=300_000_000_000,
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_beacon_advertisement_v1",
            ttl_seconds=300.0,
            allow_write_ttl=True,
        ),
    )

    await bucket.put("advertisements.by_feature.hardware.deck", {"owner": "hw"})

    assert fake_js.updated_raw_config is None
    assert fake_js.updated_config is None


@pytest.mark.asyncio
async def test_nats_json_kv_exposes_resolved_bucket_ttl() -> None:
    fake_js = _FakeJs(
        existing=True,
        max_age=45.0,
        allow_msg_ttl=True,
        subject_delete_marker_ttl=45_000_000_000,
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(
            bucket="deckr_concord_token_v1",
            ttl_seconds=45.0,
            allow_write_ttl=True,
        ),
    )
    materialized = NatsKvMaterializedBucket(bucket=bucket)

    assert await bucket.ttl_seconds() == 45.0
    assert await materialized.ttl_seconds() == 45.0


@pytest.mark.asyncio
async def test_nats_json_kv_persistent_bucket_does_not_require_delete_markers() -> None:
    fake_js = _FakeJs(
        existing=True,
        max_age=0.0,
        allow_msg_ttl=False,
        subject_delete_marker_ttl=None,
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    await bucket.put("contracts.main.1.meta", {"state": "open"})

    assert fake_js.updated_raw_config is None
    assert fake_js.updated_config is None


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
    fake_js.kv.add_marker(
        "contracts.main.1.participants.worker",
        operation="",
    )
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    async with bucket.watch("contracts.") as changes:
        put = await changes.receive()
        deleted = await changes.receive()
        expired = await changes.receive()
        unclassified_marker = await changes.receive()
        ready = await changes.receive()

    assert put is not None
    assert put.operation == "put"
    assert put.entry is not None
    assert put.entry.value == {"state": "open"}
    assert deleted is not None
    assert deleted.operation == "delete"
    assert expired is not None
    assert expired.operation == "expire"
    assert unclassified_marker is not None
    assert unclassified_marker.operation == "delete"
    assert unclassified_marker.marker_reason == "absent"
    assert ready is None
    assert fake_js.deleted_consumers == [("KV_deckr_concord_contract_v1", "consumer-1")]


@pytest.mark.asyncio
async def test_nats_json_kv_items_lists_current_entries_by_prefix() -> None:
    fake_js = _FakeJs()
    fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"open"}')
    fake_js.kv.add_entry("contracts.main.2.meta", b'{"state":"cancelled"}')
    fake_js.kv.add_marker("contracts.main.3.meta", operation="DEL")
    fake_js.kv.add_entry("other.main.1.meta", b'{"state":"open"}')
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    entries = await bucket.items("contracts.")

    assert [entry.key for entry in entries] == [
        "contracts.main.1.meta",
        "contracts.main.2.meta",
    ]
    assert entries[0].value == {"state": "open"}
    assert fake_js.kv.watch_patterns == ["contracts.>"]
    assert fake_js.kv.key_filters == []


@pytest.mark.asyncio
async def test_nats_json_kv_items_does_not_use_substring_key_filters() -> None:
    fake_js = _FakeJs()
    fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"open"}')
    fake_js.kv.add_entry("other.main.1.meta", b'{"state":"open"}')
    fake_js.kv.raise_no_keys_for_filters = True
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    entries = await bucket.items("contracts.")

    assert [entry.key for entry in entries] == ["contracts.main.1.meta"]
    assert fake_js.kv.watch_patterns == ["contracts.>"]
    assert fake_js.kv.key_filters == []


@pytest.mark.asyncio
async def test_nats_json_kv_items_falls_back_to_unfiltered_keys_without_watch() -> None:
    fake_js = _FakeJs()
    fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"open"}')
    fake_js.kv.add_entry("other.main.1.meta", b'{"state":"open"}')
    fake_js.kv.watch_unsupported = True
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    entries = await bucket.items("contracts.")

    assert [entry.key for entry in entries] == ["contracts.main.1.meta"]
    assert fake_js.kv.watch_patterns == []
    assert fake_js.kv.key_filters == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["DEL", "PURGE", "PUT"])
async def test_nats_json_kv_create_reclaims_absent_marker(operation: str) -> None:
    fake_js = _FakeJs()
    marker = fake_js.kv.add_marker("contracts.main.1.meta", operation=operation)
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    entry = await bucket.create("contracts.main.1.meta", {"state": "open"})

    assert entry.value == {"state": "open"}
    assert entry.revision == marker.revision + 1
    raw_entry = await fake_js.kv.get("contracts.main.1.meta")
    assert not kv_entry_is_absent_marker(raw_entry)


@pytest.mark.asyncio
async def test_nats_json_kv_create_keeps_live_entry_conflict() -> None:
    fake_js = _FakeJs()
    fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"existing"}')
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    with pytest.raises(KvConflict):
        await bucket.create("contracts.main.1.meta", {"state": "open"})

    entry = await bucket.get("contracts.main.1.meta")
    assert entry is not None
    assert entry.value == {"state": "existing"}


@pytest.mark.asyncio
async def test_nats_json_kv_delete_returns_real_marker_revision_after_stream_gap() -> None:
    fake_js = _FakeJs()
    current = fake_js.kv.add_entry("contracts.main.1.meta", b'{"state":"open"}')
    gap = fake_js.kv.add_entry("contracts.main.2.meta", b'{"state":"open"}')
    bucket = NatsJsonKvBucket(
        js=fake_js,
        policy=KvBucketPolicy(bucket="deckr_concord_contract_v1", ttl_seconds=None),
    )

    marker_revision = await bucket.delete(
        "contracts.main.1.meta",
        revision=current.revision,
    )

    assert marker_revision == gap.revision + 1
    assert marker_revision != current.revision + 1
    assert kv_entry_is_absent_marker(await fake_js.kv.get("contracts.main.1.meta"))


@pytest.mark.asyncio
async def test_materialized_bucket_status_tracks_stale_and_current_recovery() -> None:
    raw = _RecoveringWatchBucket(bucket="recovering")
    raw.add("items.a", {"value": "a"})
    materialized = NatsKvMaterializedBucket(bucket=raw, key_prefix="items.")

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_current()

        assert materialized.status == KvViewStatus.READY
        assert materialized.is_ready()
        assert materialized.is_current()

        raw.pause_next_watch()
        raw.close_current_watch()
        with anyio.fail_after(1):
            await raw.wait_next_watch_paused()

        assert materialized.status == KvViewStatus.STALE
        assert materialized.is_ready()
        assert not materialized.is_current()

        with anyio.move_on_after(0.05) as scope:
            await materialized.wait_current()
        assert scope.cancelled_caught

        raw.resume_next_watch()
        with anyio.fail_after(1):
            await materialized.wait_current()

        assert materialized.status == KvViewStatus.READY
        assert materialized.is_current()
        task_group.cancel_scope.cancel()


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
async def test_materialized_bucket_delete_uses_exact_marker_revision_after_stream_gap() -> None:
    raw = MemoryJsonKvBucket(bucket="materialized")
    materialized = NatsKvMaterializedBucket(bucket=raw, key_prefix="items.")

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_current()
        current = await materialized.put("items.a", {"value": "a"})
        gap = await raw.put("other.a", {"value": "gap"})

        async with materialized.subscribe() as changes:
            marker_revision = await materialized.delete(
                "items.a",
                revision=current.revision,
            )
            with anyio.fail_after(1):
                deleted = await changes.receive()
            with anyio.move_on_after(0.05) as duplicate_scope:
                await changes.receive()

        assert marker_revision == gap.revision + 1
        assert marker_revision != current.revision + 1
        assert deleted.operation == "delete"
        assert deleted.key == "items.a"
        assert deleted.revision == marker_revision
        assert duplicate_scope.cancelled_caught
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_bucket_changes_carry_view_generation() -> None:
    raw = MemoryJsonKvBucket(bucket="materialized")
    materialized = NatsKvMaterializedBucket(bucket=raw, key_prefix="items.")

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_current()
        initial_generation = materialized.generation

        async with materialized.subscribe() as changes:
            entry = await materialized.put("items.a", {"value": "a"})
            delivered = await changes.receive()

        assert materialized.generation == initial_generation + 1
        assert delivered.key == entry.key
        assert delivered.revision == entry.revision
        assert delivered.view_generation == materialized.generation

        await materialized._apply_change(  # noqa: SLF001
            KvChange(raw.bucket, entry.key, entry.revision, "put", entry)
        )
        assert materialized.generation == initial_generation + 1
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_materialized_bucket_generation_advances_when_subscriber_drops_change() -> None:
    raw = MemoryJsonKvBucket(bucket="materialized")
    materialized = NatsKvMaterializedBucket(
        bucket=raw,
        key_prefix="items.",
        buffer_size=1,
    )

    async with anyio.create_task_group() as task_group:
        materialized.start(task_group)
        await materialized.wait_current()
        initial_generation = materialized.generation

        async with materialized.subscribe() as changes:
            await materialized.put("items.a", {"value": "a"})
            await materialized.put("items.b", {"value": "b"})
            delivered = await changes.receive()
            with anyio.move_on_after(0.05) as scope:
                await changes.receive()

        assert delivered.view_generation == initial_generation + 1
        assert scope.cancelled_caught
        assert materialized.generation == initial_generation + 2
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
        self._pause_next_watch = False
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()
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

    def pause_next_watch(self) -> None:
        self._pause_next_watch = True
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

    async def wait_next_watch_paused(self) -> None:
        await self._watch_paused.wait()

    def resume_next_watch(self) -> None:
        self._resume_watch.set()

    async def get(self, key: str) -> KvEntry | None:
        self.get_count += 1
        return self._entries.get(key)

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        self.watch_count += 1
        if self._pause_next_watch:
            self._pause_next_watch = False
            self._watch_paused.set()
            await self._resume_watch.wait()
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[KvChange | None](100)
        snapshot = tuple(
            entry for key, entry in sorted(self._entries.items()) if key.startswith(prefix)
        )

        async def run() -> None:
            for entry in snapshot:
                await send.send(
                    KvChange(self.bucket, entry.key, entry.revision, "put", entry)
                )
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
        operation: str | None = "PUT",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.key = key
        self.value = value
        self.revision = revision
        self.operation = operation
        self.headers = headers or {}


class _FakeKeyDeleted(RuntimeError):
    def __init__(self, entry: _FakeKvEntry) -> None:
        self.entry = entry


class NoKeysError(RuntimeError):
    pass


class _FakeKv:
    def __init__(self, js: _FakeJs) -> None:
        self._js = js
        self._stream = f"KV_{js.bucket}"
        self._revision = 0
        self._entries: dict[str, _FakeKvEntry] = {}
        self.raise_no_keys_for_filters = False
        self.watch_unsupported = False
        self.key_filters: list[object] = []
        self.watch_patterns: list[str] = []

    def add_entry(self, key: str, value: bytes) -> _FakeKvEntry:
        self._revision += 1
        entry = _FakeKvEntry(key=key, value=value, revision=self._revision)
        self._entries[key] = entry
        return entry

    def add_marker(
        self,
        key: str,
        *,
        operation: str | None = "PUT",
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

    async def _get(self, key: str) -> _FakeKvEntry:
        entry = await self.get(key)
        if kv_entry_is_absent_marker(entry):
            raise _FakeKeyDeleted(entry)
        return entry

    async def keys(self, filters=None) -> tuple[str, ...]:
        self.key_filters.append(filters)
        if filters is not None and self.raise_no_keys_for_filters:
            raise NoKeysError("no keys")
        if filters is None:
            prefixes = ("",)
        elif isinstance(filters, str):
            prefixes = (filters.rstrip(">"),)
        else:
            prefixes = tuple(str(item).rstrip(">") for item in filters)
        return tuple(
            key
            for key, entry in sorted(self._entries.items())
            if not kv_entry_is_absent_marker(entry)
            and any(key.startswith(prefix) for prefix in prefixes)
        )

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
        if self.watch_unsupported:
            raise TypeError("watch is unavailable")
        self.watch_patterns.append(keys)
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
        subject_delete_marker_ttl: int | None = None,
    ) -> None:
        self.name = name
        self.max_age = max_age
        self.max_msgs_per_subject = max_msgs_per_subject
        self.allow_msg_ttl = allow_msg_ttl
        self.subject_delete_marker_ttl = subject_delete_marker_ttl


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
        subject_delete_marker_ttl: int | None = None,
    ) -> None:
        self.bucket = "deckr_concord_contract_v1"
        self.kv = _FakeKv(self) if existing else None
        self.config = _FakeStreamConfig(
            name=f"KV_{self.bucket}",
            max_age=max_age,
            allow_msg_ttl=allow_msg_ttl,
            subject_delete_marker_ttl=subject_delete_marker_ttl,
        )
        self.created_config = None
        self.updated_config = None
        self.updated_raw_config = None
        self.deleted_consumers: list[tuple[str, str]] = []
        self._consumer_index = 0
        self._jsm = self
        self._prefix = "$JS.API"
        self._timeout = 5

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

    async def _api_request(
        self,
        subject: str,
        req: bytes = b"",
        *,
        timeout: float = 5,
    ) -> dict[str, object]:
        del timeout
        if subject == f"$JS.API.STREAM.INFO.{self.config.name}":
            return {"config": self._raw_config(), "state": {}}
        if subject == f"$JS.API.STREAM.UPDATE.{self.config.name}":
            raw_config = json.loads(req.decode("utf-8"))
            self.updated_raw_config = raw_config
            self.config = _FakeStreamConfig(
                name=str(raw_config["name"]),
                max_age=float(raw_config["max_age"]) / 1_000_000_000,
                max_msgs_per_subject=int(raw_config["max_msgs_per_subject"]),
                allow_msg_ttl=bool(raw_config.get("allow_msg_ttl", False)),
                subject_delete_marker_ttl=raw_config.get(
                    "subject_delete_marker_ttl"
                ),
            )
            return {"config": self._raw_config(), "state": {}}
        raise AssertionError(f"unexpected JetStream API subject {subject!r}")

    def _raw_config(self) -> dict[str, object]:
        raw: dict[str, object] = {
            "name": self.config.name,
            "max_age": (
                0
                if self.config.max_age is None
                else int(self.config.max_age * 1_000_000_000)
            ),
            "max_msgs_per_subject": self.config.max_msgs_per_subject,
            "allow_msg_ttl": self.config.allow_msg_ttl,
        }
        if self.config.subject_delete_marker_ttl is not None:
            raw["subject_delete_marker_ttl"] = self.config.subject_delete_marker_ttl
        return raw

    async def delete_consumer(self, stream: str, consumer: str) -> bool:
        self.deleted_consumers.append((stream, consumer))
        return True
