from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.beacon import Beacon, BeaconAdvertisementSpec, BeaconDirectory
from deckr.substrates.nats_kv import KvChange, KvEntry, KvUnavailable

FEATURE_ID = "dev.deckr.test.directory"


class _CountingBeacon:
    def __init__(self, beacon: Beacon) -> None:
        self._beacon = beacon
        self.candidate_calls = 0
        self.exact_candidate_calls = 0

    def candidates(self, *args, **kwargs):
        self.candidate_calls += 1
        return self._beacon.candidates(*args, **kwargs)

    async def candidates_exact(self, *args, **kwargs):
        self.exact_candidate_calls += 1
        return await self._beacon.candidates_exact(*args, **kwargs)

    def watch(self, *args, **kwargs):
        return self._beacon.watch(*args, **kwargs)

    def is_current(self) -> bool:
        return self._beacon.is_current()

    async def wait_current(self) -> None:
        await self._beacon.wait_current()


class _ManualCurrentBeacon:
    def is_current(self) -> bool:
        return True


class _RecoveringBeaconBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket, ttl_seconds=300)
        self._close_events: list[anyio.Event] = []
        self._pause_next_watch = False
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

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

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        if self._pause_next_watch:
            self._pause_next_watch = False
            self._watch_paused.set()
            await self._resume_watch.wait()
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[KvChange | None](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._watchers[send] = prefix
            snapshot: tuple[KvEntry, ...] = tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )

        async def run() -> None:
            for entry in snapshot:
                await send.send(
                    KvChange(self.bucket, entry.key, entry.revision, "put", entry)
                )
            await send.send(None)
            await close_event.wait()
            await send.aclose()

        try:
            async with send, receive, anyio.create_task_group() as task_group:
                task_group.start_soon(run)
                yield receive
                task_group.cancel_scope.cancel()
        finally:
            async with self._lock:
                self._watchers.pop(send, None)


def _parse_payload(candidate) -> str | list[str] | None:
    payload = candidate.advertisement.payload or {}
    if payload.get("raise"):
        raise ValueError("bad payload")
    value = payload.get("value")
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return str(value)


async def _publish(
    beacon: Beacon,
    advertisement_id: str,
    payload: Mapping[str, Any],
):
    return await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=FEATURE_ID,
            endpoint=f"service:{advertisement_id}",
            session_id=f"session-{advertisement_id}",
            advertisement_id=advertisement_id,
            payload=payload,
        )
    )


async def _eventually_records(
    directory: BeaconDirectory[str],
    expected: tuple[str, ...],
) -> None:
    with anyio.fail_after(1):
        while True:
            try:
                if directory.records() == expected:
                    return
            except KvUnavailable:
                pass
            await anyio.sleep(0)


@pytest.mark.asyncio
async def test_beacon_directory_replays_cached_candidates_without_exact_scan(caplog) -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)
    counting = _CountingBeacon(beacon)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await _publish(beacon, "ad-1", {"value": "one"})
        await _publish(beacon, "ad-2", {"value": ["two", "three"]})
        await _publish(beacon, "ad-3", {})
        await _publish(beacon, "ad-4", {"raise": True})

        directory = BeaconDirectory(
            counting,
            FEATURE_ID,
            _parse_payload,
            log_label="TestDirectory",
        )
        directory.start(tg)
        await directory.wait_ready()

        assert directory.records() == ("one", "two", "three")
        assert counting.candidate_calls == 0
        assert counting.exact_candidate_calls == 0
        assert "Beacon directory parser rejected" in caplog.text
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_removes_replayed_record_after_withdraw() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        first = await _publish(beacon, "ad-1", {"value": "one"})

        directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)
        directory.start(tg)
        await directory.wait_ready()
        assert directory.records() == ("one",)

        await first.withdraw()
        await _eventually_records(directory, ())
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_tracks_update_withdraw_expire_and_invalid() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        directory.start(tg)
        await directory.wait_ready()

        first = await _publish(beacon, "ad-1", {"value": "one"})
        await _eventually_records(directory, ("one",))

        await first.update(payload={"value": ["two", "three"]})
        await _eventually_records(directory, ("two", "three"))

        await first.update(payload={})
        await _eventually_records(directory, ())

        await first.update(payload={"value": "back"})
        await _eventually_records(directory, ("back",))

        await raw.put(first.handle.key, {"invalid": "advertisement"})
        await _eventually_records(directory, ())

        second = await _publish(beacon, "ad-2", {"value": "expire-me"})
        await _eventually_records(directory, ("expire-me",))
        await raw.expire(second.handle.key)
        await _eventually_records(directory, ())

        third = await _publish(beacon, "ad-3", {"value": "withdraw-me"})
        await _eventually_records(directory, ("withdraw-me",))
        await third.withdraw()
        await _eventually_records(directory, ())
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_wait_for_returns_later_match_and_times_out() -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300))
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        directory.start(tg)
        await directory.wait_ready()

        async def publish_later() -> None:
            await anyio.sleep(0.01)
            await _publish(beacon, "ad-1", {"value": "later"})

        tg.start_soon(publish_later)
        assert await directory.wait_for(lambda item: item == "later", timeout=1) == "later"
        with pytest.raises(TimeoutError):
            await directory.wait_for(lambda item: item == "missing", timeout=0.01)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_watch_records_replays_and_updates_snapshots() -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300))
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await _publish(beacon, "ad-1", {"value": "one"})
        directory.start(tg)

        snapshots = directory.watch_records()
        try:
            assert await anext(snapshots) == ("one",)

            second = await _publish(beacon, "ad-2", {"value": "two"})
            assert await anext(snapshots) == ("one", "two")

            await second.withdraw()
            assert await anext(snapshots) == ("one",)
        finally:
            await snapshots.aclose()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_watch_records_recovers_from_stale_view() -> None:
    directory = BeaconDirectory(_ManualCurrentBeacon(), FEATURE_ID, _parse_payload)
    directory._records_by_key["ad-1"] = ("one",)  # noqa: SLF001
    directory._mark_current()  # noqa: SLF001

    snapshots = directory.watch_records()
    try:
        assert await anext(snapshots) == ("one",)
        directory._mark_stale()  # noqa: SLF001

        async def recover() -> None:
            await anyio.sleep(0.01)
            directory._mark_current()  # noqa: SLF001

        async with anyio.create_task_group() as tg:
            tg.start_soon(recover)
            assert await anext(snapshots) == ("one",)
            tg.cancel_scope.cancel()
    finally:
        await snapshots.aclose()


@pytest.mark.asyncio
async def test_beacon_directory_watch_records_closes_cleanly() -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300))
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        directory.start(tg)

        snapshots = directory.watch_records()
        assert await anext(snapshots) == ()

        await directory.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(snapshots)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_watch_records_does_not_miss_change_between_waits() -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300))
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        directory.start(tg)

        snapshots = directory.watch_records()
        try:
            assert await anext(snapshots) == ()
            await _publish(beacon, "ad-1", {"value": "one"})

            with anyio.fail_after(1):
                assert await anext(snapshots) == ("one",)
        finally:
            await snapshots.aclose()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_directory_surfaces_stale_and_recovers_current_view() -> None:
    raw = _RecoveringBeaconBucket(bucket="beacon")
    beacon = Beacon(raw)
    directory = BeaconDirectory(beacon, FEATURE_ID, _parse_payload)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        directory.start(tg)
        await directory.wait_ready()
        await _publish(beacon, "ad-1", {"value": "one"})
        await _eventually_records(directory, ("one",))

        raw.pause_next_watch()
        raw.close_current_watch()
        await raw.wait_next_watch_paused()

        with pytest.raises(KvUnavailable):
            directory.resolve()

        raw.resume_next_watch()
        await beacon.wait_current()
        assert directory.resolve() == "one"
        tg.cancel_scope.cancel()
