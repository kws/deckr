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
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
    canonical_json_hash,
    concord_participant_profile_index_prefix,
)
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
from deckr.profiles import (
    ACTION_PROVIDER_SESSION_PROFILE_ID,
    ACTIONS_FEATURE_ID,
    ActionProviderSessionTerms,
    ActionsBeaconPayload,
    action_provider_session_contract_id,
    actions_payload_from_advertisement,
    profile_terms_hash,
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


async def _receive_notification_source(stream, source: str):
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
        super().__init__(bucket=bucket)
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


def _concord(
    contract_bucket: MemoryJsonKvBucket | object,
    token_bucket: MemoryJsonKvBucket | object,
    *,
    token_ttl_seconds: int = 30,
) -> Concord:
    return Concord(
        contract_bucket,
        token_bucket,
        MemoryJsonKvBucket(bucket=f"maintenance-{id(contract_bucket)}-{id(token_bucket)}"),
        token_ttl_seconds=token_ttl_seconds,
    )


def _raw_revision(bucket) -> int:
    inner = getattr(bucket, "_inner", bucket)
    return int(inner._revision)


def _inner_concord(service_or_concord) -> Concord:
    return service_or_concord


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
    raw = MemoryJsonKvBucket(bucket="beacon")
    return Beacon(raw, default_ttl_seconds=30), raw


def test_beacon_removes_statestore_construction_layer() -> None:
    import deckr.beacon as beacon_module

    assert not hasattr(beacon_module, "BeaconDiscovery")
    assert not hasattr(beacon_module, "BeaconService")
    assert not hasattr(Beacon, "find")
    assert not hasattr(Beacon, "watch_feature")


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
async def test_beacon_advertisement_lease_emits_semantic_events_and_logs(caplog) -> None:
    beacon, _raw = _beacon()
    endpoint = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.beacon")

    async with beacon.watch(HARDWARE_FEATURE_ID) as events:
        advertisement = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=endpoint,
                session_id="manager-session",
                advertisement_id="advertisement-1",
                labels={"room": "office"},
                payload=_hardware_payload().to_dict(),
                log_label="TestHardware",
            )
        )
        handle = advertisement.handle
        advertised = await _receive(events)
        assert advertised.event_type == BeaconFeatureEventType.ADVERTISED
        assert advertised.candidate is not None
        assert advertised.candidate.advertisement.advertisement_id == (
            handle.advertisement_id
        )

        refreshed = await advertisement.update(hints={"load": "light"})
        updated = await _receive(events)
        assert updated.event_type == BeaconFeatureEventType.UPDATED
        assert updated.candidate is not None
        assert updated.candidate.advertisement.refresh_seq == refreshed.refresh_seq

        await advertisement.aclose()
        withdrawn = await _receive(events)
        assert withdrawn.event_type == BeaconFeatureEventType.WITHDRAWN
        assert withdrawn.previous is not None
        assert withdrawn.previous.advertisement.advertisement_id == "advertisement-1"

    assert "TestHardware Beacon advertisement announced" in caplog.text
    assert "Beacon advertisement withdrawn" in caplog.text


@pytest.mark.asyncio
async def test_beacon_watch_replays_current_candidate() -> None:
    beacon, _raw = _beacon()
    spec = BeaconAdvertisementSpec(
        feature_id=HARDWARE_FEATURE_ID,
        endpoint=hardware_manager_address("manager-main"),
        session_id="manager-session",
        advertisement_id="advertisement-1",
        payload=_hardware_payload().to_dict(),
    )
    advertisement = await beacon.advertise(spec)

    async with beacon.watch(HARDWARE_FEATURE_ID) as events:
        event = await _receive(events)

    assert event.event_type == BeaconFeatureEventType.ADVERTISED
    assert event.candidate is not None
    assert event.candidate.key == advertisement.handle.key


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
async def test_beacon_managed_publish_serializes_concurrent_refreshes() -> None:
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
    refreshes = []

    async def publish(hint: str) -> None:
        refreshes.append(await advertisement.update(hints={"publish": hint}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(publish, "a")
        tg.start_soon(publish, "b")

    candidate = beacon.candidates(HARDWARE_FEATURE_ID)[0]
    assert sorted(handle.refresh_seq for handle in refreshes) == [2, 3]
    assert candidate.advertisement.refresh_seq == 3


@pytest.mark.asyncio
async def test_beacon_find_returns_newest_revision_first() -> None:
    beacon, _raw = _beacon()
    await beacon.advertise(
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

    candidates = beacon.candidates(HARDWARE_FEATURE_ID)

    assert [candidate.advertisement.advertisement_id for candidate in candidates] == [
        "z-new",
        "a-old",
    ]
    assert [candidate.revision for candidate in candidates] == [2, 1]


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
async def test_beacon_close_withdraws_and_stops_heartbeat() -> None:
    beacon, _raw = _beacon()

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
                refresh_interval=0.01,
            ),
        )
        await advertisement.aclose()
        await anyio.sleep(0.03)
        tg.cancel_scope.cancel()

    assert beacon.candidates(HARDWARE_FEATURE_ID) == ()


@pytest.mark.asyncio
async def test_beacon_heartbeat_cadence_is_clamped_by_ttl() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon")
    beacon = Beacon(raw, default_ttl_seconds=1)

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
                ttl_seconds=1,
                refresh_interval=0.01,
            )
        )
        first = advertisement.handle
        await anyio.sleep(0.05)
        early = await raw.get(first.key)
        assert early is not None
        assert early.revision == first.revision

        with anyio.fail_after(1):
            while True:
                current = await raw.get(first.key)
                assert current is not None
                record = AdvertisementRecord.model_validate(current.value)
                if record.refresh_seq > first.refresh_seq:
                    break
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()


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
async def test_beacon_service_feature_watch_reports_expiry(caplog) -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.beacon")

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        await beacon.wait_ready()
        async with beacon.watch(HARDWARE_FEATURE_ID) as events:
            advertisement = await beacon.advertise(
                BeaconAdvertisementSpec(
                    feature_id=HARDWARE_FEATURE_ID,
                    endpoint=endpoint,
                    session_id="manager-session",
                    advertisement_id="advertisement-1",
                    labels={"room": "office"},
                    payload=_hardware_payload().to_dict(),
                    log_label="TestHardware",
                )
            )
            handle = advertisement.handle
            await _receive(events)
            await raw.expire(handle.key)
            expired = await _receive(events)
        tg.cancel_scope.cancel()

    assert expired.event_type == BeaconFeatureEventType.EXPIRED
    assert expired.reason == "expire"
    assert "Beacon advertisement expired" in caplog.text


@pytest.mark.asyncio
async def test_beacon_validate_reports_unavailable_while_view_stale() -> None:
    raw = PausingWatchKvBucket(bucket="beacon")
    beacon = Beacon(raw, default_ttl_seconds=30)
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
async def test_concord_participant_lease_closes_after_cancelled_contract() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    lease = service._participant_lease(
        contract=contract,
        participant=controller,
        session_id="controller-session",
    )
    await lease.attach_or_refresh()
    await service._cancel(contract, controller, reason="test complete")

    with pytest.raises(ConcordConflict, match="cancelled"):
        await lease.attach_or_refresh()
    with pytest.raises(ConcordConflict, match="closed"):
        await lease.attach_or_refresh()


@pytest.mark.asyncio
async def test_concord_participant_lease_rate_limits_token_writes() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
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

    first = await lease.attach_or_refresh()
    repeated = await lease.attach_or_refresh()
    repeated_entry = await token_state.get(first.key)

    assert repeated.refresh_seq == first.refresh_seq
    assert repeated.revision == first.revision
    assert repeated_entry is not None
    assert repeated_entry.revision == first.revision

    await anyio.sleep(0.06)
    early = await lease.attach_or_refresh()
    assert early.refresh_seq == first.refresh_seq
    assert early.revision == first.revision

    await anyio.sleep(0.5)
    refreshed = await lease.attach_or_refresh()

    assert refreshed.refresh_seq == first.refresh_seq + 1
    assert refreshed.revision != first.revision


@pytest.mark.asyncio
async def test_concord_participant_lease_adopts_without_immediate_refresh() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
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
    manager_token = await service._attach(contract, manager, "manager-session")
    lease = service._participant_lease(
        contract=contract,
        participant=manager,
        session_id="manager-session",
        refresh_interval=0.01,
    )

    lease.adopt(manager_token)
    adopted = await lease.attach_or_refresh()

    assert adopted.refresh_seq == manager_token.refresh_seq
    assert adopted.revision == manager_token.revision

    await anyio.sleep(0.55)
    refreshed = await lease.attach_or_refresh()

    assert refreshed.refresh_seq == manager_token.refresh_seq + 1
    assert refreshed.revision != manager_token.revision


@pytest.mark.asyncio
async def test_concord_participant_lease_close_withdraws_owned_token() -> None:
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

    await lease.aclose()

    assert await token_state.get(token.key) is None
    assert (await service._validate(contract)).status == ContractValidityStatus.MISSING_TOKEN


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
async def test_concord_participant_manager_attaches_adopts_and_filters() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    other = await service._create_contract(
        (manager, controller),
        contract_id="other-contract-1",
        profile="dev.deckr.profile.other.v1",
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")
    await service._attach(other, controller, "controller-session")

    manager_lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    managed = await manager_lifecycle.reconcile(reason="test attach")

    assert [item.contract.contract_id for item in managed] == ["hardware-contract-1"]
    assert managed[0].validity.status == ContractValidityStatus.VALID
    assert managed[0].token is not None
    assert managed[0].token.refresh_seq == 1

    adopted_lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    adopted = await adopted_lifecycle.reconcile(reason="test adopt")

    assert adopted[0].token is not None
    assert adopted[0].token.token_id == managed[0].token.token_id
    assert adopted[0].token.refresh_seq == 1


@pytest.mark.asyncio
async def test_concord_participant_manager_discovers_claims_from_participant_profile_index() -> None:
    contract_state = RecordingItemsKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    other_manager = hardware_manager_address("other-manager")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    await service._create_contract(
        (manager, controller),
        contract_id="other-profile-contract",
        profile="dev.deckr.profile.other.v1",
        created_by=controller,
    )
    await service._create_contract(
        (other_manager, controller),
        contract_id="other-participant-contract",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(claim_id="claim-2"),
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")

    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    contract_state.items_prefixes.clear()

    managed = await lifecycle.reconcile(reason="indexed discovery")

    assert [item.contract.contract_id for item in managed] == ["hardware-contract-1"]
    assert contract_state.items_prefixes == [
        concord_participant_profile_index_prefix(
            participant=manager,
            profile=HARDWARE_CLAIM_PROFILE_ID,
        )
    ]
    assert "contracts." not in contract_state.items_prefixes


@pytest.mark.asyncio
async def test_concord_participant_manager_steady_reconcile_uses_watch_index() -> None:
    contract_state = RecordingItemsKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
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
        refresh_interval=30.0,
        reconcile_interval=0.05,
        accept_contract=lambda _contract, _record: True,
    )

    async with anyio.create_task_group() as task_group:
        lifecycle.start(task_group)
        with anyio.fail_after(2):
            while True:
                managed = lifecycle.managed_contract(contract)
                if managed is not None and managed.token is not None:
                    break
                await anyio.sleep(0.01)

        index_prefix = concord_participant_profile_index_prefix(
            participant=manager,
            profile=HARDWARE_CLAIM_PROFILE_ID,
        )
        assert "contracts." not in contract_state.items_prefixes
        assert set(contract_state.items_prefixes) <= {index_prefix}
        contract_state.items_prefixes.clear()
        refresh_seq = managed.token.refresh_seq

        await lifecycle.reconcile(reason="steady reconcile")
        await lifecycle.reconcile(reason="steady reconcile again")
        task_group.cancel_scope.cancel()

    managed = lifecycle.managed_contract(contract)
    assert managed is not None
    assert managed.token is not None
    assert managed.token.refresh_seq == refresh_seq
    assert contract_state.items_prefixes == [index_prefix, index_prefix]


@pytest.mark.asyncio
async def test_concord_participant_manager_watch_periodic_and_valid_dedupe() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
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
async def test_concord_participant_manager_notification_reconciles_expiry_and_cancel() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = ConcordParticipant(
        concord=service,
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
        reconcile_interval=30.0,
        notification_batch_interval=0.01,
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
async def test_concord_participant_manager_releases_on_token_expiry_and_cancel() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
    )
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    controller_token = await service._attach(
        contract,
        controller,
        "controller-session",
    )
    async with lifecycle.watch() as events:
        managed = (await lifecycle.reconcile(reason="test live"))[0]
        assert managed.validity.status == ContractValidityStatus.VALID
        await _receive_managed_event_type(
            events,
            ConcordManagedContractEventType.VALID,
        )

        await _delete_token_from_view(
            service,
            token_state,
            controller_token,
            operation="expire",
        )
        await lifecycle.reconcile(reason="test token expiry")
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
        assert lifecycle.managed_contracts == ()

    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-2",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(claim_id="claim-2"),
        created_by=controller,
    )
    await service._attach(contract, controller, "controller-session")
    await lifecycle.reconcile(reason="test live again")
    await service._cancel(contract, controller, reason="done")
    await lifecycle.reconcile(reason="test cancel")
    assert lifecycle.managed_contracts == ()


@pytest.mark.asyncio
async def test_concord_participant_manager_release_withdraws_owned_token_by_default() -> None:
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

    await lifecycle.release(contract)

    assert await token_state.get(manager_token.key) is None
    assert lifecycle.managed_contracts == ()


@pytest.mark.asyncio
async def test_concord_participant_manager_release_can_preserve_owned_token() -> None:
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

    await lifecycle.release(contract, withdraw=False)

    current = await token_state.get(manager_token.key)
    assert current is not None
    record = ParticipantTokenRecord.model_validate(current.value)
    assert record.token_id == manager_token.token_id
    assert lifecycle.managed_contracts == ()


@pytest.mark.asyncio
async def test_concord_participant_manager_policy_rejection_withdraws_owned_token() -> None:
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
    accepted = True

    def accept_contract(_contract, _record):
        return accepted

    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=accept_contract,
    )
    managed = (await lifecycle.reconcile(reason="test live"))[0]
    manager_token = managed.token
    assert manager_token is not None

    accepted = False
    assert await lifecycle.reconcile(reason="policy rejection") == ()

    assert await token_state.get(manager_token.key) is None
    assert lifecycle.managed_contracts == ()


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
async def test_concord_participant_manager_cancelled_contract_preserves_token() -> None:
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

    await service._cancel(contract, controller, reason="done")
    assert await lifecycle.reconcile(reason="test cancel") == ()

    current = await token_state.get(manager_token.key)
    assert current is not None
    record = ParticipantTokenRecord.model_validate(current.value)
    assert record.token_id == manager_token.token_id


@pytest.mark.asyncio
async def test_concord_participant_manager_session_mismatch_preserves_token() -> None:
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

    lifecycle.session_id = "manager-session-new"
    assert await lifecycle.reconcile(reason="session changed") == ()

    current = await token_state.get(manager_token.key)
    assert current is not None
    record = ParticipantTokenRecord.model_validate(current.value)
    assert record.token_id == manager_token.token_id


@pytest.mark.asyncio
async def test_concord_participant_manager_policy_rejection_does_not_cancel() -> None:
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
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: False,
    )

    assert await lifecycle.reconcile(reason="policy rejection") == ()
    validity = await service._validate(contract)
    assert validity.contract is not None
    assert validity.contract.state == ContractState.OPEN
    assert validity.status == ContractValidityStatus.NOT_YET_FULFILLED


@pytest.mark.asyncio
async def test_concord_participant_manager_policy_rejection_skips_validation_logs(
    caplog,
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
    token = await service._attach(contract, controller, "controller-session")
    await _delete_token_from_view(service, token_state, token)
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: False,
    )
    caplog.set_level("INFO", logger="deckr.concord")

    caplog.clear()
    assert await lifecycle.reconcile(reason="policy rejection") == ()

    assert "Concord contract invalid" not in caplog.text
    assert "Concord contract pending" not in caplog.text
    record = await service._contract_record(contract)
    assert record is not None
    assert record.state == ContractState.OPEN


@pytest.mark.asyncio
async def test_concord_watch_emits_single_event_stream(caplog) -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.concord")

    async with service.watch(
        HARDWARE_CLAIM_PROFILE_ID,
        replay_current=False,
    ) as events:
        caplog.clear()
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        event = await _receive_event_type(
            events,
            ConcordEventType.CONTRACT_PENDING,
        )

    assert event.contract is not None
    assert event.contract.contract_id == contract.contract_id
    assert "Concord contract contract_pending" in caplog.text


@pytest.mark.asyncio
async def test_concord_watch_replay_skips_pre_registration_publication() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    publish_entered = anyio.Event()
    release_publish = anyio.Event()
    publish_done = anyio.Event()
    original_publish = service._publish_events  # noqa: SLF001
    paused = False

    async def paused_publish(events, deliveries):
        nonlocal paused
        if not paused and any(
            event.event_type == ConcordEventType.CONTRACT_PROPOSED
            for event in events
        ):
            paused = True
            publish_entered.set()
            await release_publish.wait()
        await original_publish(events, deliveries)
        if paused:
            publish_done.set()

    service._publish_events = paused_publish  # noqa: SLF001

    async def create_contract() -> None:
        await service._create_contract(  # noqa: SLF001
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(create_contract)
        with anyio.fail_after(1):
            await publish_entered.wait()

        async with service.watch(HARDWARE_CLAIM_PROFILE_ID) as events:
            replay = await _receive_event_type(
                events,
                ConcordEventType.CONTRACT_PENDING,
            )
            release_publish.set()
            with anyio.fail_after(1):
                await publish_done.wait()
            with pytest.raises(anyio.WouldBlock):
                events.receive_nowait()

    assert replay.contract is not None
    assert replay.contract.contract_id == "hardware-contract-1"


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
async def test_concord_event_stream_does_not_validate_or_fetch_contracts() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async def fail_validate(*args, **kwargs):
        del args, kwargs
        raise AssertionError("validate must not be called")

    async def fail_get_contract(*args, **kwargs):
        del args, kwargs
        raise AssertionError("get_contract must not be called")

    service._coordinator.validate = fail_validate
    service.get_contract = fail_get_contract

    async with service.watch(
        HARDWARE_CLAIM_PROFILE_ID,
        replay_current=False,
    ) as events:
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        contract_event = await _receive_event_type(
            events,
            ConcordEventType.CONTRACT_PROPOSED,
        )
        await service._attach(contract, controller, "controller-session")
        token_event = await _receive_event_type(
            events,
            ConcordEventType.TOKEN_ATTACHED,
        )

    assert contract_event.change is not None
    assert contract_event.change.operation == "put"
    assert contract_event.contract == contract
    assert contract_event.profile == HARDWARE_CLAIM_PROFILE_ID
    assert token_event.change is not None
    assert token_event.change.operation == "put"
    assert token_event.contract is not None
    assert token_event.contract.contract_id == contract.contract_id
    assert token_event.contract.generation == contract.generation
    assert token_event.participant == controller


@pytest.mark.asyncio
async def test_concord_service_watch_preserves_caller_state_unavailable() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)

    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        async with service.watch(replay_current=False):
            raise ConcordUnavailable("broker unavailable")


@pytest.mark.asyncio
async def test_concord_service_watch_uses_cached_events_without_source_watch() -> None:
    contract_state = FailingWatchKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with service.watch(replay_current=False) as events:
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        event = await _receive_event_type(events, ConcordEventType.CONTRACT_PROPOSED)

    assert event.contract == contract


@pytest.mark.asyncio
async def test_concord_service_use_missing_token_logs_below_info(caplog) -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    service_endpoint = service_address("openhab-home")
    client = action_provider_address("python-dev.deckr.openhab")
    contract = await service._create_contract(
        (service_endpoint, client),
        contract_id="service-use:openhab",
        profile="dev.deckr.openhab.service_use.v1",
        created_by=client,
    )
    await service._attach(contract, service_endpoint, "service-session")
    client_token = await service._attach(contract, client, "client-session")
    await _delete_token_from_view(service, token_state, client_token)

    caplog.set_level("INFO", logger="deckr.concord")
    caplog.clear()
    validity = await service._validate(contract)

    assert validity.status == ContractValidityStatus.MISSING_TOKEN
    assert "Concord contract invalid" not in caplog.text


@pytest.mark.asyncio
async def test_concord_ensure_agreement_supersedes_stable_token_loss() -> None:
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
        stable_contract_id="service-use:openhab",
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

    successor = await service.propose(spec)

    assert successor.contract_id == agreement.contract_id
    assert successor.generation == 2
    assert successor.local_token is not None
    assert (await service._validate(agreement.contract)).status == (
        ContractValidityStatus.CANCELLED
    )
    assert (await successor.refresh()).status == (
        ContractValidityStatus.NOT_YET_FULFILLED
    )


@pytest.mark.asyncio
async def test_concord_ensure_agreement_cancels_stable_conflicting_generations() -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    controller = controller_address("controller-main")
    provider = action_provider_address("provider-main")
    stable_id = action_provider_session_contract_id(controller, provider)

    old = await service._create_contract(
        (controller, provider),
        contract_id=stable_id,
        generation=1,
        profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
        terms=ActionProviderSessionTerms(
            sessionId="old-provider-session",
            controllerEndpoint=controller,
            providerEndpoint=provider,
            providerInstanceId="provider-main",
            providerId="dev.deckr.clock",
        ),
        created_by=controller,
    )
    older_conflict = await service._create_contract(
        (controller, provider),
        contract_id=stable_id,
        generation=2,
        profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
        terms=ActionProviderSessionTerms(
            sessionId="older-provider-session",
            controllerEndpoint=controller,
            providerEndpoint=provider,
            providerInstanceId="provider-main",
            providerId="dev.deckr.clock",
        ),
        created_by=controller,
    )

    agreement = await service.propose(
        ConcordAgreementSpec(
            profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
            participants=(controller, provider),
            local_participant=controller,
            local_session_id="controller-session",
            stable_contract_id=stable_id,
            terms=ActionProviderSessionTerms(
                sessionId="current-provider-session",
                controllerEndpoint=controller,
                providerEndpoint=provider,
                providerInstanceId="provider-main",
                providerId="dev.deckr.clock",
            ),
            current_sessions={
                str(controller): "controller-session",
                str(provider): "current-provider-session",
            },
        )
    )

    assert agreement.contract_id == stable_id
    assert agreement.generation == 3
    assert (await service._validate(old)).status == ContractValidityStatus.CANCELLED
    assert (await service._validate(older_conflict)).status == (
        ContractValidityStatus.CANCELLED
    )
    record = await service.contract_record(agreement.contract)
    assert record is not None
    assert record.supersedes is not None
    assert record.supersedes.generation == 2


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
async def test_concord_ensure_agreement_generated_id_is_fresh() -> None:
    service = _concord(
        MemoryJsonKvBucket(bucket="contracts"),
        MemoryJsonKvBucket(bucket="tokens"),
    )
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    spec = ConcordAgreementSpec(
        profile=HARDWARE_CLAIM_PROFILE_ID,
        participants=(controller, manager),
        local_participant=controller,
        local_session_id="controller-session",
        terms=_hardware_claim_terms(),
    )

    first = await service.propose(spec)
    second = await service.propose(spec)

    assert first.contract_id != second.contract_id
    assert first.generation == 1
    assert second.generation == 1
    assert first.local_token is not None
    assert second.local_token is not None


@pytest.mark.asyncio
async def test_concord_agreement_cancel_preserves_token_until_cancel(
    monkeypatch,
) -> None:
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
        terms=_hardware_claim_terms(),
    )
    agreement = await service.propose(spec)
    token = agreement.local_token
    assert token is not None
    original_cancel = service._cancel
    observed: dict[str, bool] = {}

    async def wrapped_cancel(*args, **kwargs):
        observed["token_present"] = await token_state.get(token.key) is not None
        return await original_cancel(*args, **kwargs)

    monkeypatch.setattr(service, "_cancel", wrapped_cancel)

    assert await agreement.cancel("test cancellation")
    assert observed == {"token_present": True}
    assert await token_state.get(token.key) is None
    record = await service.contract_record(agreement.contract)
    assert record is not None
    assert record.state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_concord_participant_manager_factory_reconciles() -> None:
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
    await service._attach(contract, controller, "controller-session")
    lifecycle = service.participant(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        accept_contract=lambda _contract, _record: True,
        current_sessions=lambda _contract: {str(controller): "controller-session"},
    )

    managed = await lifecycle.reconcile()

    assert len(managed) == 1
    assert managed[0].contract.contract_id == contract.contract_id
    assert managed[0].contract.generation == contract.generation
    assert managed[0].token is not None
    assert managed[0].validity.status == ContractValidityStatus.VALID


@pytest.mark.asyncio
async def test_concord_service_lease_events_and_logs(caplog) -> None:
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state, token_ttl_seconds=1)
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
        adopted_manager_lease.adopt(valid.validity.tokens[str(manager)])
        assert (await adopted_manager_lease.attach_or_refresh()).refresh_seq == 1

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
async def test_concord_stable_agreement_lookup_uses_contract_id_prefix() -> None:
    contract_state = CountingItemsKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens")
    service = _concord(contract_state, token_state)
    service_endpoint = service_address("openhab-home")
    client = action_provider_address("python-dev.deckr.openhab")
    for index in range(5):
        await service._create_contract(
            (service_endpoint, client),
            contract_id=f"unrelated-{index}",
            profile="dev.deckr.openhab.service_use.v1",
            created_by=client,
        )
    contract = await service._create_contract(
        (service_endpoint, client),
        contract_id="service-use-openhab",
        profile="dev.deckr.openhab.service_use.v1",
        created_by=client,
    )
    spec = ConcordAgreementSpec(
        profile="dev.deckr.openhab.service_use.v1",
        participants=(service_endpoint, client),
        local_participant=client,
        local_session_id="client-session",
        stable_contract_id="service-use-openhab",
        current_sessions={
            str(service_endpoint): "service-session",
            str(client): "client-session",
        },
    )
    contract_state.items_prefixes.clear()

    agreement = await service.propose(spec)

    assert agreement.contract.key == contract.key
    assert contract_state.items_prefixes == []


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


def test_profile_payloads_terms_hashes_and_hardware_claim_conflicts() -> None:
    hardware_payload = _hardware_payload()
    hardware_advertisement = AdvertisementRecord(
        advertisementId="advertisement-1",
        featureId=HARDWARE_FEATURE_ID,
        advertiser=hardware_manager_address("manager-main"),
        endpoint=hardware_manager_address("manager-main"),
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=30,
        payload=hardware_payload.to_dict(),
    )
    assert hardware_payload_from_advertisement(hardware_advertisement) == hardware_payload

    actions_payload = ActionsBeaconPayload(
        providerInstanceId="provider-main",
        providerEndpoint=action_provider_address("provider-main"),
        providerId="dev.deckr.clock",
        sessionId="provider-session",
        actions={
            "dev.deckr.clock.time": {
                "actionId": "dev.deckr.clock.time",
                "name": "Clock",
            }
        },
    )
    actions_advertisement = AdvertisementRecord(
        advertisementId="actions-advertisement-1",
        featureId=ACTIONS_FEATURE_ID,
        advertiser=action_provider_address("provider-main"),
        endpoint=action_provider_address("provider-main"),
        sessionId="provider-session",
        refreshSeq=1,
        ttlSeconds=30,
        payload=actions_payload.to_dict(),
    )
    assert actions_payload_from_advertisement(actions_advertisement) == actions_payload

    with pytest.raises(ValidationError, match="providerEndpoint"):
        ActionsBeaconPayload(
            providerInstanceId="provider-main",
            providerEndpoint=action_provider_address("other"),
            providerId="dev.deckr.clock",
            sessionId="provider-session",
        )

    claim_terms = _hardware_claim_terms()
    session_terms = ActionProviderSessionTerms(
        sessionId="provider-session",
        controllerEndpoint=controller_address("controller-main"),
        providerEndpoint=action_provider_address("provider-main"),
        providerInstanceId="provider-main",
        providerId="dev.deckr.clock",
    )
    assert profile_terms_hash(claim_terms) == canonical_json_hash(claim_terms)
    assert profile_terms_hash(session_terms) == canonical_json_hash(session_terms)
    assert session_terms.profile == ACTION_PROVIDER_SESSION_PROFILE_ID

    conflicting = _hardware_claim_terms(claim_id="claim-2")
    non_conflicting = _hardware_claim_terms(
        claim_id="claim-3",
        device_id="stream-deck-xl",
    )
    assert hardware_claim_conflicts((conflicting, non_conflicting), claim_terms) == (
        conflicting,
    )


def test_action_provider_session_contract_id_is_endpoint_scoped() -> None:
    controller = controller_address("controller-main")
    provider = action_provider_address("provider-main")

    contract_id = action_provider_session_contract_id(controller, provider)

    assert contract_id == action_provider_session_contract_id(
        str(controller),
        str(provider),
    )
    assert contract_id != action_provider_session_contract_id(
        controller_address("other-controller"),
        provider,
    )
    assert contract_id != action_provider_session_contract_id(
        controller,
        action_provider_address("other-provider"),
    )
    with pytest.raises(ValueError, match="controllerEndpoint"):
        action_provider_session_contract_id(provider, provider)
    with pytest.raises(ValueError, match="providerEndpoint"):
        action_provider_session_contract_id(controller, controller)


def test_concord_contract_attached_participants_must_be_named() -> None:
    with pytest.raises(ValidationError, match="attachedParticipants"):
        ContractRecord(
            contractId="contract-1",
            generation=1,
            participants=(controller_address("controller-main"),),
            attachedParticipants=(hardware_manager_address("manager-main"),),
        )


def test_beacon_key_helper_uses_feature_namespace_prefix() -> None:
    assert beacon_advertisement_key(
        feature_id=HARDWARE_FEATURE_ID,
        advertisement_id="advertisement-1",
    ).startswith("advertisements.by_feature.")
