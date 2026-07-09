from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from memory_kv_bucket import MemoryJsonKvBucket
from pydantic import ValidationError

import deckr.beacon as beacon_module
import deckr.concord as concord_module
from deckr.actions.endpoints import action_provider_address
from deckr.beacon import (
    AdvertisementRecord,
    Beacon,
    BeaconAdvertisementSpec,
    BeaconFeatureEventType,
    CandidateStatus,
    beacon_advertisement_key,
)
from deckr.concord import (
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordEvent,
    ConcordEventType,
    ConcordManagedContractEventType,
    ConcordParticipant,
    ConcordUnavailable,
    ContractPointer,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
    concord_contract_key,
)
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import (
    controller_address,
    hardware_manager_address,
    service_address,
)
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    ProfileCapacity,
    hardware_claim_conflicts,
    hardware_payload_from_advertisement,
)
from deckr.substrates.nats_kv import KvChange, KvConflict, KvEntry, KvUnavailable


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


async def _receive_event_type(stream, *event_types):
    with anyio.fail_after(1):
        while True:
            event = await stream.receive()
            if event.event_type in event_types:
                return event


async def _receive_managed_event_type(stream, *event_types):
    with anyio.fail_after(1):
        while True:
            event = await stream.receive()
            if event.event_type in event_types:
                return event


async def _receive_notification_source(stream, source):
    with anyio.fail_after(1):
        while True:
            notification = await stream.receive()
            if notification.source == source:
                return notification


class RacingUpdateKvBucket:
    def __init__(self, inner: MemoryJsonKvBucket) -> None:
        self._inner = inner
        self.raced = False

    @property
    def bucket(self) -> str:
        return self._inner.bucket

    async def ttl_seconds(self):
        return await self._inner.ttl_seconds()

    async def get(self, *args, **kwargs):
        return await self._inner.get(*args, **kwargs)

    async def put(self, *args, **kwargs):
        return await self._inner.put(*args, **kwargs)

    async def create(self, *args, **kwargs):
        return await self._inner.create(*args, **kwargs)

    async def update(self, key, value, *, revision, ttl=None):
        if not self.raced and ".participants." in key:
            self.raced = True
            current = await self._inner.get(key)
            assert current is not None
            token = ParticipantTokenRecord.model_validate(current.value)
            bumped = token.model_copy(update={"refresh_seq": token.refresh_seq + 1})
            await self._inner.update(key, bumped, revision=current.revision, ttl=ttl)
        return await self._inner.update(key, value, revision=revision, ttl=ttl)

    async def delete(self, *args, **kwargs):
        return await self._inner.delete(*args, **kwargs)

    def watch(self, *args, **kwargs):
        return self._inner.watch(*args, **kwargs)


class FailingUpdateKvBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str, ttl_seconds: float | None = None) -> None:
        super().__init__(bucket=bucket, ttl_seconds=ttl_seconds)
        self.fail_updates = False

    async def update(self, *args, **kwargs):
        if self.fail_updates:
            raise KvUnavailable("broker unavailable")
        return await super().update(*args, **kwargs)


class UnavailableWatch:
    async def __aenter__(self):
        raise ConcordUnavailable("watch unavailable")

    async def __aexit__(self, *args):
        return None


class FailingWatchKvBucket(MemoryJsonKvBucket):
    def watch(self, prefix: str = ""):
        del prefix
        return UnavailableWatch()


class PausingWatchKvBucket(MemoryJsonKvBucket):
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


class CountingItemsKvBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket)
        self.items_prefixes: list[str] = []

    async def items(self, prefix: str = ""):
        self.items_prefixes.append(prefix)
        return ()


class RecordingItemsKvBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket)
        self.items_prefixes: list[str] = []

    async def items(self, prefix: str = ""):
        self.items_prefixes.append(prefix)
        return await super().items(prefix)


class FailingExactReadKvBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket)
        self.fail_exact_reads = False

    async def get(self, key: str):
        if self.fail_exact_reads:
            raise AssertionError(f"unexpected exact get for {key!r}")
        return await super().get(key)

    async def items(self, prefix: str = ""):
        if self.fail_exact_reads:
            raise AssertionError(f"unexpected exact items for {prefix!r}")
        return await super().items(prefix)


def _concord(
    contract_bucket: MemoryJsonKvBucket | object,
    token_bucket: MemoryJsonKvBucket | object,
    *,
    token_bucket_ttl_seconds: int | float = 120,
) -> Concord:
    inner_token_bucket = getattr(token_bucket, "_inner", token_bucket)
    if isinstance(inner_token_bucket, MemoryJsonKvBucket):
        inner_token_bucket._ttl_seconds = token_bucket_ttl_seconds
    return Concord(
        contract_bucket,
        token_bucket,
        MemoryJsonKvBucket(bucket=f"maintenance-{id(contract_bucket)}-{id(token_bucket)}"),
    )


def _raw_revision(bucket) -> int:
    inner = getattr(bucket, "_inner", bucket)
    return int(inner._revision)


def _inner_concord(service_or_concord) -> Concord:
    return service_or_concord


def _legacy_participant_profile_index_key(
    *,
    participant: str,
    profile: str,
    contract_id: str,
    generation: int,
) -> str:
    return ".".join(
        (
            "contracts",
            "by_participant",
            encode_key_token(participant),
            "by_profile",
            encode_key_token(profile),
            encode_key_token(contract_id),
            str(generation),
            "ref",
        )
    )


async def _delete_token_from_view(
    service_or_concord,
    bucket,
    token,
    *,
    operation: str = "delete",
) -> None:
    concord = _inner_concord(service_or_concord)
    if operation == "expire":
        await bucket.expire(token.key)
    else:
        await bucket.delete(token.key, revision=token.revision)
    await concord._apply_token_change(  # noqa: SLF001
        KvChange(concord.token_bucket, token.key, _raw_revision(bucket), operation)
    )


async def _put_token_from_view(service_or_concord, bucket, key: str, value) -> None:
    concord = _inner_concord(service_or_concord)
    entry = await bucket.put(key, value)
    await concord._apply_token_change(  # noqa: SLF001
        KvChange(concord.token_bucket, key, entry.revision, "put", entry)
    )


def _descriptor() -> DeviceDescriptor:
    return DeviceDescriptor.model_validate(stream_deck_bitmap_grid())


def _hardware_payload(*, session_id: str = "manager-session") -> HardwareBeaconPayload:
    descriptor = _descriptor()
    return HardwareBeaconPayload(
        managerId="manager-main",
        managerEndpoint=hardware_manager_address("manager-main"),
        sessionId=session_id,
        labels={"room": "office"},
        devices={
            descriptor.device_id: HardwareAdvertisementDevice(
                capacity=ProfileCapacity(
                    totalInstances=1,
                    claimedInstances=0,
                    availableInstances=1,
                ),
                deviceRef=DeviceRef(
                    managerId="manager-main",
                    deviceId=descriptor.device_id,
                    fingerprint=descriptor.fingerprint,
                ),
                descriptor=descriptor,
            )
        },
    )


def _hardware_advertisement_record(
    advertisement_id: str,
    *,
    session_id: str = "manager-session",
) -> AdvertisementRecord:
    endpoint = hardware_manager_address("manager-main")
    return AdvertisementRecord(
        advertisementId=advertisement_id,
        featureId=HARDWARE_FEATURE_ID,
        advertiser=endpoint,
        endpoint=endpoint,
        sessionId=session_id,
        refreshSeq=1,
        ttlSeconds=300,
        payload=_hardware_payload(session_id=session_id).to_dict(),
    )


def _hardware_claim_terms(
    *,
    claim_id: str = "claim-1",
    device_id: str = "stream-deck-mini",
) -> HardwareClaimTerms:
    return HardwareClaimTerms(
        claimId=claim_id,
        controllerEndpoint=controller_address("controller-main"),
        managerEndpoint=hardware_manager_address("manager-main"),
        devices=(
            HardwareClaimDevice(
                deviceRef=DeviceRef(
                    managerId="manager-main",
                    deviceId=device_id,
                    fingerprint="usb:0fd9:0063:serial-abc",
                ),
                instanceCount=1,
            ),
        ),
    )


def _beacon() -> tuple[Beacon, MemoryJsonKvBucket]:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    return Beacon(raw), raw


@pytest.mark.asyncio
async def test_beacon_wait_current_rebuilds_generation_stale_cache() -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")
    record = AdvertisementRecord(
        advertisementId="advertisement-1",
        featureId=HARDWARE_FEATURE_ID,
        advertiser=endpoint,
        endpoint=endpoint,
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=300,
        payload=_hardware_payload().to_dict(),
    )
    key = beacon_advertisement_key(
        feature_id=record.feature_id,
        advertisement_id=record.advertisement_id,
    )
    await raw.put(key, record.to_dict())

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_current()
        assert [candidate.key for candidate in beacon.candidates(HARDWARE_FEATURE_ID)] == [
            key
        ]

        async with beacon._lock:  # noqa: SLF001
            beacon._entries_by_key.clear()  # noqa: SLF001
            beacon._revision_by_key.clear()  # noqa: SLF001
            beacon._invalid_by_key.clear()  # noqa: SLF001
            beacon._keys_by_feature.clear()  # noqa: SLF001
            beacon._keys_by_feature_endpoint.clear()  # noqa: SLF001
            beacon._bucket_generation = 0  # noqa: SLF001

        assert not beacon.is_current()
        with pytest.raises(KvUnavailable):
            beacon.candidates(HARDWARE_FEATURE_ID)

        await beacon.wait_current()

        assert [candidate.key for candidate in beacon.candidates(HARDWARE_FEATURE_ID)] == [
            key
        ]
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_candidates_exact_reads_bucket_directly() -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")
    record = AdvertisementRecord(
        advertisementId="advertisement-1",
        featureId=HARDWARE_FEATURE_ID,
        advertiser=endpoint,
        endpoint=endpoint,
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=300,
        payload=_hardware_payload().to_dict(),
    )
    key = beacon_advertisement_key(
        feature_id=record.feature_id,
        advertisement_id=record.advertisement_id,
    )
    await raw.put(key, record.to_dict())

    candidates = await beacon.candidates_exact(HARDWARE_FEATURE_ID)

    assert [candidate.key for candidate in candidates] == [key]
    assert candidates[0].advertisement.session_id == "manager-session"


@pytest.mark.asyncio
async def test_beacon_rebuilds_from_bucket_after_generation_gap() -> None:
    beacon, _raw = _beacon()
    first = _hardware_advertisement_record("advertisement-1")
    second = _hardware_advertisement_record("advertisement-2")
    first_key = beacon_advertisement_key(
        feature_id=first.feature_id,
        advertisement_id=first.advertisement_id,
    )
    second_key = beacon_advertisement_key(
        feature_id=second.feature_id,
        advertisement_id=second.advertisement_id,
    )

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_current()
        await beacon._bucket.put(first_key, first)  # noqa: SLF001
        second_entry = await beacon._bucket.put(second_key, second)  # noqa: SLF001
        await beacon.wait_current()
        bucket_generation = beacon._bucket.generation  # noqa: SLF001

        async with beacon._lock:  # noqa: SLF001
            beacon._entries_by_key.clear()  # noqa: SLF001
            beacon._revision_by_key.clear()  # noqa: SLF001
            beacon._invalid_by_key.clear()  # noqa: SLF001
            beacon._keys_by_feature.clear()  # noqa: SLF001
            beacon._keys_by_feature_endpoint.clear()  # noqa: SLF001
            beacon._bucket_generation = 0  # noqa: SLF001

        await beacon._apply_kv_change(  # noqa: SLF001
            KvChange(
                beacon.bucket,
                second_key,
                second_entry.revision,
                "put",
                second_entry,
                view_generation=bucket_generation,
            )
        )

        assert beacon.is_current()
        assert {candidate.key for candidate in beacon.candidates(HARDWARE_FEATURE_ID)} == {
            first_key,
            second_key,
        }
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_generation_gap_rebuild_notifies_watchers() -> None:
    beacon, _raw = _beacon()
    first = _hardware_advertisement_record("advertisement-1")
    second = _hardware_advertisement_record("advertisement-2")
    first_key = beacon_advertisement_key(
        feature_id=first.feature_id,
        advertisement_id=first.advertisement_id,
    )
    second_key = beacon_advertisement_key(
        feature_id=second.feature_id,
        advertisement_id=second.advertisement_id,
    )

    async with beacon.watch(HARDWARE_FEATURE_ID, replay_current=False) as events:
        await beacon._bucket.put(first_key, first)  # noqa: SLF001
        second_entry = await beacon._bucket.put(second_key, second)  # noqa: SLF001
        bucket_generation = beacon._bucket.generation  # noqa: SLF001

        await beacon._apply_kv_change(  # noqa: SLF001
            KvChange(
                beacon.bucket,
                second_key,
                second_entry.revision,
                "put",
                second_entry,
                view_generation=bucket_generation,
            )
        )

        first_event = await _receive(events)
        second_event = await _receive(events)

    assert {
        first_event.event_type,
        second_event.event_type,
    } == {BeaconFeatureEventType.ADVERTISED}
    assert {first_event.key, second_event.key} == {first_key, second_key}


@pytest.mark.asyncio
async def test_beacon_create_refresh_withdraw_find_watch_and_validate() -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_ready()
        async with beacon.watch(HARDWARE_FEATURE_ID) as changes:
            advertisement = await beacon.advertise(
                BeaconAdvertisementSpec(
                    feature_id=HARDWARE_FEATURE_ID,
                    endpoint=endpoint,
                    session_id="manager-session",
                    advertisement_id="advertisement-1",
                    labels={"room": "office"},
                    payload=_hardware_payload().to_dict(),
                )
            )
            handle = advertisement.handle
            change = await _receive(changes)

        assert change.key == handle.key
        candidates = beacon.candidates(HARDWARE_FEATURE_ID)
        assert len(candidates) == 1
        assert candidates[0].advertisement.payload is not None
        assert await beacon.validate(candidates[0]) == CandidateStatus.CANDIDATE
        assert (
            await beacon.validate(
                candidates[0],
                current_sessions={str(endpoint): "different-session"},
            )
            == CandidateStatus.SESSION_MISMATCH
        )

        refreshed = await advertisement.update(hints={"load": "light"})
        assert refreshed.refresh_seq == 2
        assert beacon.candidates(HARDWARE_FEATURE_ID)[0].advertisement.hints == {
            "load": "light"
        }

        with pytest.raises(KvConflict):
            await beacon.advertise(
                BeaconAdvertisementSpec(
                    feature_id=HARDWARE_FEATURE_ID,
                    endpoint=endpoint,
                    session_id="manager-session",
                    advertisement_id="advertisement-1",
                ),
                cleanup_stale_same_endpoint=False,
            )

        assert await advertisement.withdraw()
        assert await beacon.validate(candidates[0]) == CandidateStatus.MISSING

        invalid = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=endpoint,
                session_id="manager-session",
                advertisement_id="advertisement-2",
            )
        )
        invalid_candidate = beacon.candidates(HARDWARE_FEATURE_ID)[0]
        await raw.put(
            invalid.handle.key,
            {
                "schema": "dev.deckr.beacon.advertisement.v1",
                "advertisementId": "advertisement-2",
            },
        )
        with anyio.fail_after(1):
            while await beacon.validate(invalid_candidate) != CandidateStatus.SCHEMA_INVALID:
                await anyio.sleep(0)
        tg.cancel_scope.cancel()



@pytest.mark.asyncio
async def test_beacon_watch_defers_live_events_until_replay_finishes() -> None:
    beacon, _raw = _beacon()
    advertisement = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="manager-session",
            advertisement_id="advertisement-1",
            payload=_hardware_payload().to_dict(),
        )
    )
    send, receive = anyio.create_memory_object_stream[
        beacon_module.BeaconFeatureEvent
    ](10)
    subscriber = beacon_module._BeaconSubscriber(  # noqa: SLF001
        send,
        HARDWARE_FEATURE_ID,
        None,
        replay_pending=True,
    )

    try:
        async with send, receive:
            async with beacon._lock:  # noqa: SLF001
                beacon._subscribers.add(subscriber)  # noqa: SLF001
                initial_candidates = beacon._matching_candidates(  # noqa: SLF001
                    HARDWARE_FEATURE_ID,
                    None,
                )
                subscriber.known_keys.update(
                    candidate.key for candidate in initial_candidates
                )
                initial = tuple(
                    beacon_module.BeaconFeatureEvent(
                        BeaconFeatureEventType.ADVERTISED,
                        candidate.advertisement.feature_id,
                        candidate.key,
                        candidate=candidate,
                    )
                    for candidate in initial_candidates
                )
                assert len(initial) == 1

                candidate = initial_candidates[0]
                value = candidate.advertisement.to_dict()
                value["refreshSeq"] = candidate.advertisement.refresh_seq + 1
                value["hints"] = {"race": "live"}
                entry = KvEntry(
                    beacon.bucket,
                    candidate.key,
                    value,
                    candidate.revision + 1,
                )
                deliveries = beacon._apply_kv_change_locked(  # noqa: SLF001
                    KvChange(beacon.bucket, candidate.key, entry.revision, "put", entry)
                )

            assert deliveries == ()
            assert len(subscriber.pending_events) == 1

            await send.send(initial[0])
            await beacon._finish_subscriber_replay(subscriber)  # noqa: SLF001
            replay = await _receive(receive)
            live = await _receive(receive)
    finally:
        async with beacon._lock:  # noqa: SLF001
            beacon._subscribers.discard(subscriber)  # noqa: SLF001

    assert replay.event_type == BeaconFeatureEventType.ADVERTISED
    assert replay.candidate is not None
    assert replay.candidate.revision == advertisement.handle.revision
    assert live.event_type == BeaconFeatureEventType.UPDATED
    assert live.candidate is not None
    assert live.candidate.revision == advertisement.handle.revision + 1


@pytest.mark.asyncio
async def test_beacon_find_treats_refresh_as_newest_write() -> None:
    beacon, _raw = _beacon()
    old = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="old-session",
            advertisement_id="a-old",
            payload=_hardware_payload(session_id="old-session").to_dict(),
        )
    )
    await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="new-session",
            advertisement_id="z-new",
            payload=_hardware_payload(session_id="new-session").to_dict(),
        ),
        cleanup_stale_same_endpoint=False,
    )

    await old.update(hints={"refreshed": "true"})

    candidates = beacon.candidates(HARDWARE_FEATURE_ID)

    assert [candidate.advertisement.advertisement_id for candidate in candidates] == [
        "a-old",
        "z-new",
    ]
    assert [candidate.revision for candidate in candidates] == [3, 2]
    assert candidates[0].advertisement.refresh_seq == 2


@pytest.mark.asyncio
async def test_beacon_heartbeat_uses_jittered_bucket_ttl(monkeypatch) -> None:
    samples: list[tuple[float, float]] = []

    def sample(lower: float, upper: float) -> float:
        samples.append((lower, upper))
        return 0.01

    monkeypatch.setattr("deckr.beacon.random.uniform", sample)
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=1)
    beacon = Beacon(raw)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_ready()
        advertisement = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=hardware_manager_address("manager-main"),
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload=_hardware_payload().to_dict(),
            )
        )
        first = advertisement.handle

        with anyio.fail_after(1):
            while True:
                current = await raw.get(first.key)
                assert current is not None
                record = AdvertisementRecord.model_validate(current.value)
                if record.refresh_seq > first.refresh_seq:
                    break
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert samples[0] == (0.5, 0.75)


@pytest.mark.asyncio
async def test_beacon_heartbeat_reschedules_after_manual_update(
    monkeypatch,
) -> None:
    samples: list[tuple[float, float]] = []

    def sample(lower: float, upper: float) -> float:
        samples.append((lower, upper))
        return lower if len(samples) == 1 else upper

    monkeypatch.setattr("deckr.beacon.random.uniform", sample)
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=1)
    beacon = Beacon(raw)

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_ready()
        advertisement = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=hardware_manager_address("manager-main"),
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload=_hardware_payload().to_dict(),
            )
        )
        await anyio.sleep(0.1)
        updated = await advertisement.update(payload={"status": "updated"})

        with anyio.fail_after(1):
            while True:
                current = await raw.get(updated.key)
                assert current is not None
                record = AdvertisementRecord.model_validate(current.value)
                if record.refresh_seq > updated.refresh_seq:
                    break
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert samples[0] == (0.5, 0.75)
    assert samples[1] == (0.5, 0.75)


@pytest.mark.asyncio
async def test_beacon_noop_updates_do_not_bypass_heartbeat_cadence() -> None:
    beacon, raw = _beacon()
    advertisement = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="manager-session",
            advertisement_id="advertisement-1",
            payload=_hardware_payload().to_dict(),
        )
    )
    first_revision = _raw_revision(raw)

    repeated = await advertisement.update()
    await advertisement.update(payload=_hardware_payload().to_dict())

    assert repeated.revision == advertisement.handle.revision
    assert _raw_revision(raw) == first_revision


@pytest.mark.asyncio
async def test_beacon_lease_recreates_missing_advertisement_key(caplog) -> None:
    beacon, raw = _beacon()
    caplog.set_level("WARNING", logger="deckr.beacon")
    advertisement = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="manager-session",
            advertisement_id="advertisement-1",
            payload=_hardware_payload().to_dict(),
            log_label="TestHardware",
        )
    )
    first = advertisement.handle
    await advertisement.update(hints={"load": "light"})

    await raw.expire(first.key)
    recovered = await advertisement.update(labels={"room": "lab"})

    assert recovered.key == first.key
    assert recovered.revision > first.revision
    entry = await raw.get(first.key)
    assert entry is not None
    record = AdvertisementRecord.model_validate(entry.value)
    assert record.advertisement_id == "advertisement-1"
    assert record.refresh_seq == 1
    assert record.labels == {"room": "lab"}
    assert record.hints == {"load": "light"}
    assert [
        candidate.advertisement.advertisement_id
        for candidate in beacon.candidates(HARDWARE_FEATURE_ID)
    ] == ["advertisement-1"]
    assert "TestHardware Beacon advertisement missing; recreating" in caplog.text


@pytest.mark.asyncio
async def test_beacon_advertise_cleans_stale_same_endpoint_by_default() -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")
    stale = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=endpoint,
            session_id="old-session",
            advertisement_id="stale-ad",
            payload=_hardware_payload(session_id="old-session").to_dict(),
        )
    )

    fresh = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=endpoint,
            session_id="new-session",
            advertisement_id="fresh-ad",
            payload=_hardware_payload(session_id="new-session").to_dict(),
        )
    )

    assert await raw.get(stale.handle.key) is None
    assert await raw.get(fresh.handle.key) is not None
    assert [
        candidate.advertisement.advertisement_id
        for candidate in beacon.candidates(HARDWARE_FEATURE_ID)
    ] == ["fresh-ad"]


@pytest.mark.asyncio
async def test_beacon_watch_emits_withdrawn_when_candidate_leaves_selector() -> None:
    beacon, _raw = _beacon()
    advertisement = await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="manager-session",
            advertisement_id="advertisement-1",
            labels={"room": "lab"},
            payload=_hardware_payload().to_dict(),
        )
    )

    async with beacon.watch(
        HARDWARE_FEATURE_ID,
        selector=lambda record: record.labels.get("room") == "office",
        replay_current=False,
    ) as events:
        await advertisement.update(labels={"room": "office"})
        advertised = await _receive(events)
        await advertisement.update(labels={"room": "lab"})
        withdrawn = await _receive(events)

    assert advertised.event_type == BeaconFeatureEventType.ADVERTISED
    assert withdrawn.event_type == BeaconFeatureEventType.WITHDRAWN
    assert withdrawn.reason == "selector_mismatch"


@pytest.mark.asyncio
async def test_beacon_validate_reports_unavailable_while_view_stale() -> None:
    raw = PausingWatchKvBucket(bucket="beacon")
    beacon = Beacon(raw)
    endpoint = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_ready()
        advertisement = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=endpoint,
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload=_hardware_payload().to_dict(),
            )
        )
        candidate = beacon.get(
            feature_id=HARDWARE_FEATURE_ID,
            advertisement_id=advertisement.handle.advertisement_id,
        )
        assert candidate is not None

        raw.pause_next_watch()
        raw.close_current_watch()
        with anyio.fail_after(1):
            await raw.wait_next_watch_paused()

        assert await beacon.validate(candidate) == CandidateStatus.UNAVAILABLE
        with pytest.raises(KvUnavailable, match="not current"):
            beacon.get(
                feature_id=HARDWARE_FEATURE_ID,
                advertisement_id=advertisement.handle.advertisement_id,
            )

        raw.resume_next_watch()
        with anyio.fail_after(1):
            while await beacon.validate(candidate) != CandidateStatus.CANDIDATE:
                await anyio.sleep(0)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_create_attach_refresh_validate_cancel_and_token_loss() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    concord = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    resolved = await concord.get_contract(
        {"contractId": "hardware-contract-1", "generation": 1}
    )
    assert resolved == contract
    assert await concord.get_contract(
        {"contractId": "missing-contract", "generation": 1}
    ) is None
    assert (await concord.validate(contract)).status == (
        ContractValidityStatus.NOT_YET_FULFILLED
    )

    controller_token = await concord._attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )
    assert (await concord.validate(contract)).status == (
        ContractValidityStatus.NOT_YET_FULFILLED
    )

    manager_token = await concord._attach(
        contract,
        manager,
        "manager-session",
        token_id="manager-token",
    )
    validity = await concord.validate(contract)
    assert validity.status == ContractValidityStatus.VALID
    assert validity.valid
    assert validity.tokens[str(controller)].key == controller_token.key
    assert validity.tokens[str(manager)].key == manager_token.key

    repeated_manager_token = await concord._attach(
        contract,
        manager,
        "manager-session",
        token_id="manager-token",
    )
    assert repeated_manager_token.key == manager_token.key
    assert repeated_manager_token.revision == manager_token.revision
    with pytest.raises(ConcordConflict, match="already attached"):
        await concord._attach(
            contract,
            manager,
            "manager-session",
        )
    with pytest.raises(ConcordConflict, match="already attached"):
        await concord._attach(
            contract,
            manager,
            "manager-session",
            token_id="manager-token-2",
        )

    refreshed = await concord._refresh_token(controller_token)
    assert refreshed.refresh_seq == 2
    assert (
        await concord.validate(
            contract,
            current_sessions={str(controller): "old-controller-session"},
        )
    ).status == ContractValidityStatus.SESSION_MISMATCH

    await _delete_token_from_view(concord, token_state, manager_token)
    missing = await concord.validate(contract)
    assert missing.status == ContractValidityStatus.MISSING_TOKEN
    with pytest.raises(ConcordConflict, match="already attached"):
        await concord._attach(
            contract,
            manager,
            "manager-session",
            token_id="manager-token-2",
        )

    async with concord.watch(replay_current=False) as changes:
        assert await concord._cancel(contract, controller, reason="test complete")
        change = await _receive(changes)
    assert change.change is not None
    assert change.change.key == contract.key
    assert (await concord.validate(contract)).status == ContractValidityStatus.CANCELLED
    with pytest.raises(ConcordConflict, match="cancelled"):
        await concord._attach(contract, controller, "new-session")


@pytest.mark.asyncio
async def test_concord_validate_reports_unavailable_while_token_view_stale() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = PausingWatchKvBucket(bucket="tokens")
    concord = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as tg:
        concord.start(tg)
        await concord.wait_ready()
        contract = await concord._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        await concord._attach(
            contract,
            controller,
            "controller-session",
            token_id="controller-token",
        )
        manager_token = await concord._attach(
            contract,
            manager,
            "manager-session",
            token_id="manager-token",
        )
        with anyio.fail_after(1):
            while (await concord.validate(contract)).status != (
                ContractValidityStatus.VALID
            ):
                await anyio.sleep(0)

        token_state.pause_next_watch()
        token_state.close_current_watch()
        with anyio.fail_after(1):
            await token_state.wait_next_watch_paused()
        await token_state.delete(manager_token.key, revision=manager_token.revision)

        assert (await concord.validate(contract)).status == (
            ContractValidityStatus.UNAVAILABLE
        )

        token_state.resume_next_watch()
        with anyio.fail_after(1):
            while (await concord.validate(contract)).status != (
                ContractValidityStatus.MISSING_TOKEN
            ):
                await anyio.sleep(0)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_refresh_returns_latest_token_after_revision_race() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = RacingUpdateKvBucket(MemoryJsonKvBucket(bucket="tokens"))
    concord = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    controller_token = await concord._attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )

    refreshed = await concord._refresh_token(controller_token)

    assert token_state.raced
    assert refreshed.refresh_seq == 2
    assert refreshed.revision != controller_token.revision


@pytest.mark.asyncio
async def test_concord_participant_heartbeat_reschedules_after_early_wake(
    monkeypatch,
) -> None:
    monkeypatch.setattr("deckr.concord.random.uniform", lambda _lower, upper: upper)
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as tg:
        agreement = await service.propose(
            ConcordAgreementSpec(
                profile=HARDWARE_CLAIM_PROFILE_ID,
                participants=(manager, controller),
                local_participant=controller,
                local_session_id="controller-session",
                terms=_hardware_claim_terms(),
                refresh_interval=0.2,
                log_label="TestConcord",
            ),
            start_soon=tg.start_soon,
        )
        token = agreement.local_token
        assert token is not None
        assert token.refresh_seq == 1

        with anyio.fail_after(0.88):
            while True:
                entry = await token_state.get(token.key)
                assert entry is not None
                record = ParticipantTokenRecord.model_validate(entry.value)
                if record.refresh_seq > token.refresh_seq:
                    break
                await anyio.sleep(0.01)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_participant_lease_cancels_on_refresh_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr("deckr.concord.random.uniform", lambda lower, _upper: lower)
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = FailingUpdateKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    lease = service._participant_lease(
        contract=contract,
        participant=controller,
        session_id="controller-session",
        refresh_interval=0.05,
    )
    await lease.attach_or_refresh()

    token_state.fail_updates = True
    await anyio.sleep(0.55)
    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        await lease.attach_or_refresh()

    assert lease.token is None
    with pytest.raises(ConcordConflict, match="closed"):
        await lease.attach_or_refresh()
    record = await service._coordinator.contract_record(contract)  # noqa: SLF001
    assert record is not None
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == concord_module.CONCORD_REFRESH_UNAVAILABLE_CANCEL_REASON


@pytest.mark.asyncio
async def test_concord_participant_lease_closes_when_refresh_and_cancel_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr("deckr.concord.random.uniform", lambda lower, _upper: lower)
    contract_state = FailingUpdateKvBucket(bucket="contracts")
    token_state = FailingUpdateKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    lease = service._participant_lease(
        contract=contract,
        participant=controller,
        session_id="controller-session",
        refresh_interval=0.05,
    )
    await lease.attach_or_refresh()

    contract_state.fail_updates = True
    token_state.fail_updates = True
    await anyio.sleep(0.55)
    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        await lease.attach_or_refresh()

    assert lease.token is None
    with pytest.raises(ConcordConflict, match="closed"):
        await lease.attach_or_refresh()
    record = await service._coordinator.contract_record(contract)  # noqa: SLF001
    assert record is not None
    assert record.state == ContractState.OPEN


@pytest.mark.asyncio
async def test_concord_participant_lease_close_does_not_withdraw_changed_owner() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    lease = service._participant_lease(
        contract=contract,
        participant=controller,
        session_id="controller-session",
    )
    token = await lease.attach_or_refresh()
    entry = await token_state.get(token.key)
    assert entry is not None
    record = ParticipantTokenRecord.model_validate(entry.value)
    changed_owner = record.model_copy(update={"token_id": "different-token"})
    await token_state.update(token.key, changed_owner, revision=entry.revision)

    await lease.aclose()

    current = await token_state.get(token.key)
    assert current is not None
    assert ParticipantTokenRecord.model_validate(current.value).token_id == (
        "different-token"
    )


@pytest.mark.asyncio
async def test_concord_participant_manager_watch_periodic_and_valid_dedupe() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        refresh_interval=0.05,
        reconcile_interval=0.05,
        accept_contract=lambda _contract, _record: True,
    )

    async with lifecycle.watch() as events, anyio.create_task_group() as task_group:
        lifecycle.start(task_group)
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        pending = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.PENDING,
        )
        assert pending.contract.contract_id == contract.contract_id

        await service._attach(contract, controller, "controller-session")
        valid = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.VALID,
        )
        assert valid.validity is not None
        assert valid.validity.status == ContractValidityStatus.VALID

        with anyio.fail_after(2):
            while True:
                managed = lifecycle.managed_contract(contract)
                if (
                    managed is not None
                    and managed.token is not None
                    and managed.token.refresh_seq > 1
                ):
                    break
                await anyio.sleep(0.01)

        await lifecycle.reconcile(reason="dedupe check")
        with anyio.move_on_after(0.1) as scope:
            await events.receive()
        assert scope.cancel_called
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_participant_token_notification_does_not_discover_unmanaged_contract(
    monkeypatch,
) -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    controller_token = await service._attach(contract, controller, "controller-session")
    accepted: list[str] = []
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda candidate, _record: (
            accepted.append(candidate.key) or True
        ),
        refresh_interval=30.0,
        reconcile_interval=30.0,
    )

    async def fail_contracts(*args, **kwargs):
        del args, kwargs
        raise AssertionError("token notification must not discover contracts")

    monkeypatch.setattr(service, "contracts", fail_contracts)
    notification = concord_module._ConcordContractNotification(  # noqa: SLF001
        concord_module._ConcordNotificationSource.TOKEN,  # noqa: SLF001
        "put",
        contract.contract_id,
        contract.generation,
        participant=controller,
        profile=HARDWARE_CLAIM_PROFILE_ID,
        change=KvChange(
            service.token_bucket,
            controller_token.key,
            controller_token.revision,
            "put",
        ),
    )

    managed = await lifecycle.reconcile_notification(notification)

    assert managed == ()
    assert accepted == []
    assert lifecycle.managed_contracts == ()


@pytest.mark.asyncio
async def test_concord_participant_manager_notification_reconciles_expiry_and_cancel() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = ConcordParticipant(
        concord=service,
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
        reconcile_interval=30.0,
    )

    async with lifecycle.watch() as events, anyio.create_task_group() as task_group:
        lifecycle.start(task_group)
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.PENDING,
        )
        controller_token = await service._attach(
            contract,
            controller,
            "controller-session",
        )
        valid = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.VALID,
        )
        assert valid.validity is not None
        assert valid.validity.status == ContractValidityStatus.VALID

        await _delete_token_from_view(
            service,
            token_state,
            controller_token,
            operation="expire",
        )
        invalid = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.INVALID,
        )
        assert invalid.validity is not None
        assert invalid.validity.status == ContractValidityStatus.MISSING_TOKEN
        released = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.RELEASED,
        )
        assert released.reason == ContractValidityStatus.MISSING_TOKEN.value
        record = await service._contract_record(contract)
        assert record is not None
        assert record.state == ContractState.CANCELLED
        assert record.cancel_reason == "concord_managed_missing_token"

        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-2",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(claim_id="claim-2"),
            created_by=controller,
        )
        await service._attach(contract, controller, "controller-session")
        await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.VALID,
        )
        await service._cancel(contract, controller, reason="done")
        cancelled = await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.CANCELLED,
        )
        assert cancelled.validity is not None
        assert cancelled.validity.status == ContractValidityStatus.CANCELLED
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_participant_manager_not_selected_withdraws_owned_token() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    managed = (await lifecycle.reconcile(reason="test live"))[0]
    manager_token = managed.token
    assert manager_token is not None

    lifecycle.profile = "dev.deckr.profile.other.v1"
    assert await lifecycle.reconcile(reason="profile changed") == ()

    assert await token_state.get(manager_token.key) is None
    assert lifecycle.managed_contracts == ()


@pytest.mark.asyncio
async def test_concord_participant_manager_restart_cancels_lost_local_token() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    managed = (await lifecycle.reconcile(reason="test live"))[0]
    manager_token = managed.token
    assert manager_token is not None

    restarted = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )

    assert await restarted.reconcile(reason="restart") == ()
    assert restarted.managed_contracts == ()
    record = await service.contract_record(contract)
    assert record is not None
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == (
        concord_module.CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON
    )
    assert await token_state.get(manager_token.key) is not None


@pytest.mark.asyncio
async def test_concord_participant_manager_empty_cancel_statuses_preserves_open() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
        cancel_terminal_statuses=(),
    )
    managed = (await lifecycle.reconcile(reason="test live"))[0]
    manager_token = managed.token
    assert manager_token is not None

    lifecycle.session_id = "manager-session-new"
    assert await lifecycle.reconcile(reason="session changed") == ()

    contract_record = await service._contract_record(contract)
    assert contract_record is not None
    assert contract_record.state == ContractState.OPEN
    current = await token_state.get(manager_token.key)
    assert current is not None
    record = ParticipantTokenRecord.model_validate(current.value)
    assert record.token_id == manager_token.token_id


@pytest.mark.asyncio
async def test_concord_watch_replay_validates_each_contract_once(monkeypatch) -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(  # noqa: SLF001
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    other = await service._create_contract(  # noqa: SLF001
        (manager, controller),
        contract_id="other-contract-1",
        profile="dev.deckr.profile.other.v1",
        created_by=controller,
    )
    calls: list[str] = []
    original_validate = service._validate_from_cache_locked  # noqa: SLF001

    def count_validate(contract, *, current_sessions=None):
        calls.append(contract.key)
        return original_validate(contract, current_sessions=current_sessions)

    monkeypatch.setattr(service, "_validate_from_cache_locked", count_validate)

    async with service.watch(HARDWARE_CLAIM_PROFILE_ID) as events:
        replay = await _receive_event_type(
            events,
            ConcordEventType.CONTRACT_PENDING,
        )
        with pytest.raises(anyio.WouldBlock):
            events.receive_nowait()

    assert replay.contract == contract
    assert sorted(calls) == sorted((contract.key, other.key))


@pytest.mark.asyncio
async def test_concord_watch_defers_live_events_until_replay_finishes() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(  # noqa: SLF001
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    send, receive = anyio.create_memory_object_stream[ConcordEvent](10)
    subscriber = concord_module._ConcordSubscriber(  # noqa: SLF001
        send,
        HARDWARE_CLAIM_PROFILE_ID,
        None,
        replay_pending=True,
    )

    try:
        async with send, receive:
            async with service._lock:  # noqa: SLF001
                service._subscribers.add(subscriber)  # noqa: SLF001
                validity = service._validate_from_cache_locked(contract)  # noqa: SLF001
                initial = ConcordEvent(
                    concord_module._concord_event_type(validity),  # noqa: SLF001
                    contract=contract,
                    record=validity.contract,
                    validity=validity,
                    profile=contract.profile,
                )
                live = ConcordEvent(
                    ConcordEventType.CONTRACT_UPDATED,
                    contract=contract,
                    profile=contract.profile,
                    change=KvChange(
                        service.contract_bucket,
                        contract.key,
                        contract.revision + 1,
                        "put",
                    ),
                )
                deliveries = service._subscriber_deliveries_locked((live,))  # noqa: SLF001

            assert deliveries == ()
            assert subscriber.pending_events == [live]

            await send.send(initial)
            await service._finish_subscriber_replay(subscriber)  # noqa: SLF001
            replay = await _receive(receive)
            deferred = await _receive(receive)
    finally:
        async with service._lock:  # noqa: SLF001
            service._subscribers.discard(subscriber)  # noqa: SLF001

    assert replay.event_type == ConcordEventType.CONTRACT_PENDING
    assert replay.contract == contract
    assert deferred == live


@pytest.mark.asyncio
async def test_concord_wait_current_rebuilds_generation_stale_cache() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(  # noqa: SLF001
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )

    async with anyio.create_task_group() as tg:
        service.start(tg)
        await service.wait_current()
        pointer = {"contractId": contract.contract_id, "generation": contract.generation}
        assert await service.get_contract(pointer) == contract

        async with service._lock:  # noqa: SLF001
            service._clear_indexes_locked()  # noqa: SLF001
            service._contract_bucket_generation = 0  # noqa: SLF001
            service._token_bucket_generation = (  # noqa: SLF001
                service._coordinator._token_bucket.generation  # noqa: SLF001
            )
            service._maintenance_bucket_generation = (  # noqa: SLF001
                service._maintenance_bucket.generation  # noqa: SLF001
            )

        assert not service.is_current()
        await service.wait_current()

        assert await service.get_contract(pointer) == contract
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_ignores_legacy_participant_profile_index_keys() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    concord = _concord(contract_state, token_state)
    legacy_key = _legacy_participant_profile_index_key(
        participant="hardware_manager:manager-main",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        contract_id="hardware-contract-1",
        generation=1,
    )
    legacy_entry = KvEntry(
        "contracts",
        legacy_key,
        {
            "schema": "dev.deckr.concord.participant-profile-proposal-ref.v1",
            "contractId": "hardware-contract-1",
            "generation": 1,
            "contractKey": concord_contract_key(
                contract_id="hardware-contract-1",
                generation=1,
            ),
            "profile": HARDWARE_CLAIM_PROFILE_ID,
            "participant": "hardware_manager:manager-main",
            "contractRevision": 1,
            "state": "open",
            "updatedAt": "2024-01-01T00:00:00Z",
        },
        1,
    )

    async with concord.watch(replay_current=False) as events:
        await concord._apply_contract_change(
            KvChange("contracts", legacy_key, legacy_entry.revision, "put", legacy_entry)
        )
        async with concord._lock:
            assert legacy_key not in concord._invalid_contracts_by_key
        with anyio.move_on_after(0.05) as scope:
            await events.receive()
        assert scope.cancelled_caught


@pytest.mark.asyncio
async def test_concord_generation_gap_rebuild_notifies_watchers() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(  # noqa: SLF001
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )

    async with anyio.create_task_group() as tg:
        service.start(tg)
        await service.wait_current()
        bucket_generation = service._coordinator._contract_bucket.generation  # noqa: SLF001

        async with service.watch(
            HARDWARE_CLAIM_PROFILE_ID,
            replay_current=False,
        ) as events:
            async with service._lock:  # noqa: SLF001
                service._clear_indexes_locked()  # noqa: SLF001
                service._contract_bucket_generation = 0  # noqa: SLF001
                service._token_bucket_generation = (  # noqa: SLF001
                    service._coordinator._token_bucket.generation  # noqa: SLF001
                )
                service._maintenance_bucket_generation = (  # noqa: SLF001
                    service._maintenance_bucket.generation  # noqa: SLF001
                )

            await service._apply_contract_change(  # noqa: SLF001
                KvChange(
                    service.contract_bucket,
                    contract.key,
                    contract.revision,
                    "put",
                    view_generation=bucket_generation,
                )
            )
            event = await _receive_event_type(
                events,
                ConcordEventType.CONTRACT_PENDING,
                ConcordEventType.CONTRACT_PROPOSED,
            )

        assert event.contract is not None
        assert event.contract.contract_id == contract.contract_id
        assert event.contract.generation == contract.generation
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_service_watch_preserves_caller_state_unavailable() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)

    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        async with service.watch(replay_current=False):
            raise ConcordUnavailable("broker unavailable")


@pytest.mark.asyncio
async def test_concord_ensure_agreement_uses_opaque_successor_after_token_loss() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    service_endpoint = service_address("openhab-home")
    client = action_provider_address("python-dev.deckr.openhab")
    spec = ConcordAgreementSpec(
        profile="dev.deckr.openhab.service_use.v1",
        participants=(service_endpoint, client),
        local_participant=client,
        local_session_id="client-session",
        current_sessions={
            str(service_endpoint): "service-session",
            str(client): "client-session",
        },
        log_label="TestConcord",
    )

    agreement = await service.propose(spec)
    await service._attach(agreement.contract, service_endpoint, "service-session")
    assert (await agreement.refresh()).status == ContractValidityStatus.VALID
    assert agreement.local_token is not None
    await _delete_token_from_view(service, token_state, agreement.local_token)

    successor = await service.propose(
        ConcordAgreementSpec(
            profile="dev.deckr.openhab.service_use.v1",
            participants=(service_endpoint, client),
            local_participant=client,
            local_session_id="client-session",
            supersedes=ContractPointer(
                contractId=agreement.contract_id,
                generation=agreement.generation,
            ),
            current_sessions={
                str(service_endpoint): "service-session",
                str(client): "client-session",
            },
            log_label="TestConcord",
        )
    )

    assert successor.contract_id != agreement.contract_id
    assert successor.generation == 1
    assert successor.local_token is not None
    successor_record = await service.contract_record(successor.contract)
    assert successor_record is not None
    assert successor_record.supersedes == ContractPointer(
        contractId=agreement.contract_id,
        generation=agreement.generation,
    )
    assert (await service._validate(agreement.contract)).status == (
        ContractValidityStatus.MISSING_TOKEN
    )
    assert (await successor.refresh()).status == (
        ContractValidityStatus.NOT_YET_FULFILLED
    )


@pytest.mark.asyncio
async def test_concord_agreement_refresh_cancels_lost_local_token_handle() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    service_endpoint = service_address("openhab-home")
    client = action_provider_address("python-dev.deckr.openhab")
    agreement = await service.propose(
        ConcordAgreementSpec(
            profile="dev.deckr.openhab.service_use.v1",
            participants=(service_endpoint, client),
            local_participant=client,
            local_session_id="client-session",
            current_sessions={
                str(service_endpoint): "service-session",
                str(client): "client-session",
            },
            log_label="TestConcord",
        )
    )
    await service._attach(agreement.contract, service_endpoint, "service-session")
    assert (await agreement.refresh()).status == ContractValidityStatus.VALID
    token = agreement.local_token
    assert token is not None

    agreement._lease._token = None  # noqa: SLF001
    agreement._lease._last_refresh_at = None  # noqa: SLF001

    refreshed = await agreement.refresh()

    assert refreshed.status == ContractValidityStatus.CANCELLED
    assert not refreshed.valid
    assert agreement.closed
    record = await service.contract_record(agreement.contract)
    assert record is not None
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == (
        concord_module.CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON
    )
    assert await token_state.get(token.key) is not None


@pytest.mark.asyncio
async def test_concord_agreement_refresh_closes_lost_token_when_cancel_unavailable() -> None:
    contract_state = FailingUpdateKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    service_endpoint = service_address("openhab-home")
    client = action_provider_address("python-dev.deckr.openhab")
    agreement = await service.propose(
        ConcordAgreementSpec(
            profile="dev.deckr.openhab.service_use.v1",
            participants=(service_endpoint, client),
            local_participant=client,
            local_session_id="client-session",
            current_sessions={
                str(service_endpoint): "service-session",
                str(client): "client-session",
            },
            log_label="TestConcord",
        )
    )
    await service._attach(agreement.contract, service_endpoint, "service-session")
    assert (await agreement.refresh()).status == ContractValidityStatus.VALID
    token = agreement.local_token
    assert token is not None

    contract_state.fail_updates = True
    agreement._lease._token = None  # noqa: SLF001
    agreement._lease._last_refresh_at = None  # noqa: SLF001

    refreshed = await agreement.refresh()

    assert refreshed.status == ContractValidityStatus.INVALID_TOKEN
    assert refreshed.reason == (
        concord_module.CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON
    )
    assert not refreshed.valid
    assert agreement.closed
    with pytest.raises(ConcordConflict, match="closed"):
        await agreement.refresh()
    record = await service.contract_record(agreement.contract)
    assert record is not None
    assert record.state == ContractState.OPEN
    assert await token_state.get(token.key) is not None


@pytest.mark.asyncio
async def test_concord_agreement_refresh_confirms_terminal_cache_status_exactly() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    spec = ConcordAgreementSpec(
        profile=HARDWARE_CLAIM_PROFILE_ID,
        participants=(controller, manager),
        local_participant=controller,
        local_session_id="controller-session",
        current_sessions={
            str(controller): "controller-session",
            str(manager): "manager-session",
        },
        log_label="TestConcord",
    )

    agreement = await service.propose(spec)
    token = agreement.local_token
    assert token is not None
    await service._apply_token_change(  # noqa: SLF001
        KvChange(
            service.token_bucket,
            token.key,
            token.revision + 1,
            "delete",
        )
    )
    assert (await service._validate(agreement.contract)).status == (  # noqa: SLF001
        ContractValidityStatus.MISSING_TOKEN
    )

    refreshed = await agreement.refresh()

    assert refreshed.status == ContractValidityStatus.NOT_YET_FULFILLED
    assert not agreement.closed
    assert agreement.local_token is not None


@pytest.mark.asyncio
async def test_concord_public_contract_helpers_preserve_validation() -> None:
    service = _concord(
        MemoryJsonKvBucket(bucket="contracts"),
        MemoryJsonKvBucket(bucket="tokens"),
    )
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (controller, manager),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )

    assert await service.contracts(
        HARDWARE_CLAIM_PROFILE_ID,
        contract_id="hardware-contract-1",
    ) == (contract,)
    assert await service.contract_record(contract) == await service._contract_record(contract)
    with pytest.raises(ValueError, match="Concord contract id"):
        await service.contracts(contract_id="")
    with pytest.raises(ValueError, match="participant"):
        await service.cancel(
            contract,
            participant=service_address("not-a-participant"),
            reason="test",
        )

    assert await service.cancel(contract, participant=controller, reason="test")
    record = await service.contract_record(contract)
    assert record is not None
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == "test"


@pytest.mark.asyncio
async def test_concord_service_lease_events_and_logs(caplog, monkeypatch) -> None:
    monkeypatch.setattr("deckr.concord.random.uniform", lambda lower, _upper: lower)
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_bucket_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()
    caplog.set_level("INFO", logger="deckr.concord")

    async with service.watch(
        HARDWARE_CLAIM_PROFILE_ID,
        replay_current=False,
    ) as events:
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=terms,
            created_by=controller,
            log_label="TestConcord",
        )
        pending = await _receive_event_type(
            events,
            ConcordEventType.CONTRACT_PENDING,
        )
        assert pending.contract == contract

        controller_lease = service._participant_lease(
            contract=contract,
            participant=controller,
            session_id="controller-session",
            refresh_interval=0.01,
            log_label="TestConcord",
        )
        controller_token = await controller_lease.attach_or_refresh()
        repeated_controller_token = await controller_lease.attach_or_refresh()
        assert repeated_controller_token.refresh_seq == 1
        await anyio.sleep(0.55)
        refreshed_controller_token = await controller_lease.attach_or_refresh()
        assert refreshed_controller_token.refresh_seq == 2

        manager_lease = service._participant_lease(
            contract=contract,
            participant=manager,
            session_id="manager-session",
            log_label="TestConcord",
        )
        await manager_lease.attach_or_refresh()
        valid = await _receive_event_type(events, ConcordEventType.CONTRACT_VALID)
        assert valid.validity is not None
        assert valid.validity.status == ContractValidityStatus.VALID
        adopted_manager_lease = service._participant_lease(
            contract=contract,
            participant=manager,
            session_id="manager-session",
            log_label="TestConcord",
        )
        with pytest.raises(ValueError, match="without an existing local handle"):
            adopted_manager_lease.adopt(valid.validity.tokens[str(manager)])

        await _delete_token_from_view(
            service,
            token_state,
            controller_token,
            operation="expire",
        )
        expired = await _receive_event_type(events, ConcordEventType.TOKEN_EXPIRED)
        assert expired.participant == controller
        assert expired.reason == "expire"
        with pytest.raises(ConcordConflict, match="missing"):
            await controller_lease.attach_or_refresh()
        with pytest.raises(ConcordConflict, match="closed"):
            await controller_lease.attach_or_refresh()

        assert await service._cancel(
            contract,
            controller,
            reason="test complete",
            log_label="TestConcord",
        )
        cancelled = await _receive_event_type(
            events,
            ConcordEventType.CONTRACT_CANCELLED,
        )
        assert cancelled.contract is not None
        assert cancelled.contract.contract_id == contract.contract_id
        assert cancelled.contract.generation == contract.generation

    assert "TestConcord Concord contract opened" in caplog.text
    assert "TestConcord Concord participant token attached" in caplog.text
    assert "Concord participant token expired" in caplog.text
    assert "TestConcord Concord contract cancelled" in caplog.text


@pytest.mark.asyncio
async def test_concord_find_and_watch_contracts() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    concord = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    other = await concord._create_contract(
        (manager, controller),
        contract_id="other-contract-1",
        profile="dev.deckr.profile.other.v1",
        created_by=controller,
    )
    await contract_state.put(
        "contracts.not-a-contract",
        {"schema": "dev.deckr.concord.contract.v1"},
    )

    assert await concord.contracts() == (contract, other)
    assert await concord.contracts(HARDWARE_CLAIM_PROFILE_ID) == (contract,)
    assert await concord.contracts(contract_id="hardware-contract-1") == (contract,)
    assert await concord.contracts(contract_id="missing") == ()

    async with concord.watch(replay_current=False) as changes:
        await concord._cancel(contract, controller, reason="done")
        change = await _receive(changes)
    assert change.change is not None
    assert change.change.key == contract.key


@pytest.mark.asyncio
async def test_concord_duplicate_contract_and_generation_mismatch_are_rejected() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    concord = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()
    contract = await concord._create_contract(
        (controller, manager),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
    )
    await concord._attach(contract, controller, "controller-session")
    manager_token = await concord._attach(contract, manager, "manager-session")

    with pytest.raises(ConcordConflict):
        await concord._create_contract(
            (controller, manager),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=terms,
        )

    token_entry = await token_state.get(manager_token.key)
    assert token_entry is not None
    token = ParticipantTokenRecord.model_validate(token_entry.value)
    mutated = token.model_copy(update={"generation": 2})
    await _put_token_from_view(concord, token_state, manager_token.key, mutated)

    assert (await concord.validate(contract)).status == (
        ContractValidityStatus.GENERATION_MISMATCH
    )


def test_hardware_profile_payloads_and_claim_conflicts() -> None:
    hardware_payload = _hardware_payload()
    hardware_advertisement = AdvertisementRecord(
        advertisementId="advertisement-1",
        featureId=HARDWARE_FEATURE_ID,
        advertiser=hardware_manager_address("manager-main"),
        endpoint=hardware_manager_address("manager-main"),
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=300,
        payload=hardware_payload.to_dict(),
    )
    assert hardware_payload_from_advertisement(hardware_advertisement) == hardware_payload

    claim_terms = _hardware_claim_terms()
    conflicting = _hardware_claim_terms(claim_id="claim-2")
    non_conflicting = _hardware_claim_terms(
        claim_id="claim-3",
        device_id="stream-deck-xl",
    )
    assert hardware_claim_conflicts((conflicting, non_conflicting), claim_terms) == (
        conflicting,
    )


def test_concord_contract_attached_participants_must_be_named() -> None:
    with pytest.raises(ValidationError, match="attachedParticipants"):
        ContractRecord(
            contractId="contract-1",
            generation=1,
            participants=(controller_address("controller-main"),),
            attachedParticipants=(hardware_manager_address("manager-main"),),
        )
