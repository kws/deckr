from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from pydantic import ValidationError

from deckr.beacon import (
    AdvertisementRecord,
    Beacon,
    BeaconAdvertisementSpec,
    BeaconWatchChange,
    BeaconWatchSnapshot,
    CandidateStatus,
    beacon_advertisement_key,
)
from deckr.concord import (
    Concord,
    ConcordParticipantChange,
    ConcordParticipantSnapshot,
    ConcordWatchChange,
    ConcordWatchSnapshot,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import (
    HARDWARE_FEATURE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    ProfileCapacity,
    hardware_claim_conflicts,
    hardware_payload_from_advertisement,
)
from deckr.substrates.nats_kv import (
    KvChange,
    KvEntry,
    KvWatchBarrier,
)
from deckr.testing import MemoryJsonKvBucket

TEST_PROFILE = "dev.deckr.test.contract.v1"


async def _receive(stream):
    with anyio.fail_after(2):
        return await anext(stream)


def _beacon() -> tuple[Beacon, MemoryJsonKvBucket]:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    return Beacon(raw), raw


def _advertisement(advertisement_id: str) -> AdvertisementRecord:
    endpoint = hardware_manager_address("manager-main")
    return AdvertisementRecord(
        advertisementId=advertisement_id,
        featureId=HARDWARE_FEATURE_ID,
        advertiser=endpoint,
        endpoint=endpoint,
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=300,
        labels={"group": "all"},
        payload={"advertisement": advertisement_id},
    )


def _contract_record(
    contract_id: str,
    *,
    profile: str = TEST_PROFILE,
    participants: tuple | None = None,
    attached: tuple = (),
) -> ContractRecord:
    if participants is None:
        participants = (controller_address("controller-main"),)
    return ContractRecord(
        contractId=contract_id,
        generation=1,
        participants=participants,
        attachedParticipants=attached,
        state=ContractState.OPEN,
        profile=profile,
    )


def _concord(
    contract_store: MemoryJsonKvBucket | None = None,
    token_store: MemoryJsonKvBucket | None = None,
) -> tuple[Concord, MemoryJsonKvBucket, MemoryJsonKvBucket]:
    contracts = contract_store or MemoryJsonKvBucket(bucket="contracts")
    tokens = token_store or MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    if token_store is not None:
        tokens._ttl_seconds = 120  # noqa: SLF001
    return Concord(contracts, tokens), contracts, tokens


@pytest.mark.asyncio
async def test_beacon_watch_is_current_first_and_converges_after_overflow() -> None:
    beacon, raw = _beacon()
    for index in range(300):
        record = _advertisement(f"bootstrap-{index}")
        await raw.put(
            beacon_advertisement_key(
                feature_id=record.feature_id,
                advertisement_id=record.advertisement_id,
            ),
            record,
        )

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_current()
        async with beacon.watch(HARDWARE_FEATURE_ID) as snapshots:
            initial = await _receive(snapshots)
            assert isinstance(initial, BeaconWatchSnapshot)
            assert initial.current
            assert len(initial.candidates) == 300

            for index in range(257):
                record = _advertisement(f"live-{index}")
                await raw.put(
                    beacon_advertisement_key(
                        feature_id=record.feature_id,
                        advertisement_id=record.advertisement_id,
                    ),
                    record,
                )
            with anyio.fail_after(2):
                while len(beacon.candidates(HARDWARE_FEATURE_ID)) != 557:
                    await anyio.sleep(0)

            change = await _receive(snapshots)
            assert isinstance(change, BeaconWatchChange)
            assert change.resnapshot_required
            assert change.changed_keys == frozenset()
            assert len(change.candidates) == 557
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_lifecycle_invalid_replacement_and_selector_converge() -> None:
    beacon, raw = _beacon()
    endpoint = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_current()
        async with beacon.watch(
            HARDWARE_FEATURE_ID,
            selector=lambda record: record.labels.get("room") == "office",
        ) as snapshots:
            initial = await _receive(snapshots)
            assert initial.candidates == ()

            lease = await beacon.advertise(
                BeaconAdvertisementSpec(
                    feature_id=HARDWARE_FEATURE_ID,
                    endpoint=endpoint,
                    session_id="manager-session",
                    advertisement_id="advertisement-1",
                    labels={"room": "lab"},
                    payload={"ok": True},
                )
            )
            await lease.update(labels={"room": "office"})
            entered = await _receive(snapshots)
            assert [item.key for item in entered.candidates] == [lease.handle.key]

            candidate = entered.candidates[0]
            await raw.put(
                lease.handle.key,
                {
                    "schema": "dev.deckr.beacon.advertisement.v1",
                    "advertisementId": "advertisement-1",
                },
            )
            invalid = await _receive(snapshots)
            assert invalid.candidates == ()
            assert await beacon.validate(candidate) == CandidateStatus.SCHEMA_INVALID

            await raw.expire(lease.handle.key)
            with anyio.fail_after(1):
                while await beacon.validate(candidate) != CandidateStatus.MISSING:
                    await anyio.sleep(0)
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_reconnect_synthesizes_missing_key_removal() -> None:
    raw = _RecoveringBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_current()
        lease = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=hardware_manager_address("manager-main"),
                session_id="manager-session",
                advertisement_id="advertisement-1",
            )
        )
        candidate = beacon.get(
            feature_id=HARDWARE_FEATURE_ID,
            advertisement_id="advertisement-1",
        )
        assert candidate is not None

        raw.pause_next_watch()
        raw.close_current_watch()
        await raw.wait_next_watch_paused()
        await raw.remove_without_publish(lease.handle.key)
        assert await beacon.validate(candidate) == CandidateStatus.UNAVAILABLE

        raw.resume_next_watch()
        await beacon.wait_current()
        with anyio.fail_after(1):
            while await beacon.validate(candidate) != CandidateStatus.MISSING:
                await anyio.sleep(0)
        assert beacon.candidates(HARDWARE_FEATURE_ID) == ()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_watch_tracks_contract_token_and_cancellation_state() -> None:
    concord, _contracts, _tokens = _concord()
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        async with concord.watch(TEST_PROFILE, participant=manager) as snapshots:
            initial = await _receive(snapshots)
            assert isinstance(initial, ConcordWatchSnapshot)
            assert initial.current
            assert initial.contracts == ()

            contract = await concord._create_contract(
                (controller, manager),
                contract_id="contract-1",
                profile=TEST_PROFILE,
                created_by=controller,
            )
            pending = await _receive(snapshots)
            assert pending.contracts[0].validity.status == (
                ContractValidityStatus.NOT_YET_FULFILLED
            )

            await concord._attach(
                contract,
                controller,
                "controller-session",
                token_id="controller-token",
            )
            await concord._attach(
                contract,
                manager,
                "manager-session",
                token_id="manager-token",
            )
            valid = await _receive_until_status(
                snapshots,
                ContractValidityStatus.VALID,
            )
            assert valid.contracts[0].validity.valid

            assert await concord._cancel(contract, controller, reason="done")
            cancelled = await _receive_until_status(
                snapshots,
                ContractValidityStatus.CANCELLED,
            )
            assert isinstance(cancelled, ConcordWatchChange)
            assert cancelled.changed_pointers == {contract.pointer}
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_token_before_contract_bootstrap_is_atomic() -> None:
    concord, contracts, tokens = _concord()
    controller = controller_address("controller-main")
    record = _contract_record(
        "contract-1",
        participants=(controller,),
        attached=(controller,),
    )
    token = ParticipantTokenRecord(
        contractId="contract-1",
        generation=1,
        participant=controller,
        sessionId="controller-session",
        tokenId="controller-token",
        refreshSeq=1,
        ttlSeconds=120,
        termsHash=record.terms_hash,
    )
    await tokens.put(
        concord_participant_token_key(
            contract_id="contract-1",
            generation=1,
            participant=controller,
        ),
        token,
    )
    await contracts.put(
        concord_contract_key(contract_id="contract-1", generation=1),
        record,
    )

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        async with concord.watch(TEST_PROFILE) as snapshots:
            initial = await _receive(snapshots)
            assert len(initial.contracts) == 1
            assert initial.contracts[0].validity.status == ContractValidityStatus.VALID
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_filtered_snapshot_validates_only_selected_contract(monkeypatch) -> None:
    concord, _contracts, _tokens = _concord()
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        selected = await concord._create_contract(
            (controller, manager),
            contract_id="selected",
            profile="profile-selected",
        )
        await concord._create_contract(
            (controller, manager),
            contract_id="unrelated",
            profile="profile-unrelated",
        )

        validated: list[str] = []
        original = concord._view.validate  # noqa: SLF001

        def recording_validate(contract, **kwargs):
            validated.append(contract.contract_id)
            return original(contract, **kwargs)

        monkeypatch.setattr(concord._view, "validate", recording_validate)  # noqa: SLF001
        async with concord.watch("profile-selected", participant=manager) as snapshots:
            initial = await _receive(snapshots)

        assert [state.contract for state in initial.contracts] == [selected]
        assert validated == ["selected"]
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_malformed_replacement_removes_indexes_and_wakes_filter() -> None:
    concord, contracts, _tokens = _concord()
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        contract = await concord._create_contract(
            (controller, manager),
            contract_id="contract-1",
            profile=TEST_PROFILE,
        )
        async with concord.watch(TEST_PROFILE) as snapshots:
            initial = await _receive(snapshots)
            assert len(initial.contracts) == 1
            await contracts.put(contract.key, {"schema": "invalid"})
            change = await _receive(snapshots)

        assert isinstance(change, ConcordWatchChange)
        assert change.contracts == ()
        assert change.changed_pointers == {contract.pointer}
        assert await concord.contracts(TEST_PROFILE) == ()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_concord_watch_oversized_snapshot_and_overflow_converge() -> None:
    concord, contracts, _tokens = _concord()
    for index in range(300):
        record = _contract_record(f"bootstrap-{index}")
        await contracts.put(
            concord_contract_key(contract_id=record.contract_id, generation=1),
            record,
        )

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        async with concord.watch(TEST_PROFILE) as snapshots:
            initial = await _receive(snapshots)
            assert len(initial.contracts) == 300

            for index in range(257):
                record = _contract_record(f"live-{index}")
                await contracts.put(
                    concord_contract_key(contract_id=record.contract_id, generation=1),
                    record,
                )
            with anyio.fail_after(2):
                while len(await concord.contracts(TEST_PROFILE)) != 557:
                    await anyio.sleep(0)

            change = await _receive(snapshots)
            assert change.resnapshot_required
            assert change.changed_pointers == frozenset()
            assert len(change.contracts) == 557
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_participant_watch_converges_before_repair_interval() -> None:
    concord, contracts, tokens = _concord()
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        contract = await concord._create_contract(
            (controller, manager),
            contract_id="contract-1",
            profile=TEST_PROFILE,
        )
        participant = concord.participant(
            participant=manager,
            session_id="manager-session",
            profile=TEST_PROFILE,
            accept_contract=lambda _contract, _record: True,
            reconcile_interval=60,
            cancel_terminal_statuses=(),
        )
        participant.start(task_group)

        async with participant.watch() as snapshots:
            initial = await _receive(snapshots)
            assert isinstance(initial, ConcordParticipantSnapshot)
            assert len(initial.contracts) == 1
            assert initial.contracts[0].validity.status == (
                ContractValidityStatus.NOT_YET_FULFILLED
            )

            await concord._attach(
                contract,
                controller,
                "controller-session",
                token_id="controller-token",
            )
            valid = await _receive_participant_until_status(
                snapshots,
                ContractValidityStatus.VALID,
            )
            assert isinstance(valid, ConcordParticipantChange)
            assert valid.changed_pointers == {contract.pointer}

        assert contracts.watch_count == 1
        assert tokens.watch_count == 1
        await participant.aclose()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_participant_discovers_post_subscription_contract_and_token_loss_without_repair() -> None:
    contracts = _RecoveringBucket(bucket="contracts")
    tokens = _RecoveringBucket(bucket="tokens", ttl_seconds=120)
    concord, _contracts, _tokens = _concord(contracts, tokens)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        participant = concord.participant(
            participant=manager,
            session_id="manager-session",
            profile=TEST_PROFILE,
            accept_contract=lambda _contract, _record: True,
            reconcile_interval=3_600,
            cancel_terminal_statuses=(),
        )
        participant.start(task_group)

        async with participant.watch() as snapshots:
            initial = await _receive(snapshots)
            assert isinstance(initial, ConcordParticipantSnapshot)
            assert initial.contracts == ()

            contract = await concord._create_contract(
                (controller, manager),
                contract_id="post-subscription",
                profile=TEST_PROFILE,
            )
            pending = await _receive_participant_until_status(
                snapshots,
                ContractValidityStatus.NOT_YET_FULFILLED,
            )
            assert {item.contract.key for item in pending.contracts} == {contract.key}

            controller_token = await concord._attach(
                contract,
                controller,
                "controller-session",
                token_id="controller-token",
            )
            valid = await _receive_participant_until_status(
                snapshots,
                ContractValidityStatus.VALID,
            )
            assert {item.contract.key for item in valid.contracts} == {contract.key}

            tokens.pause_next_watch()
            tokens.close_current_watch()
            await tokens.wait_next_watch_paused()
            await tokens.remove_without_publish(controller_token.key)
            tokens.resume_next_watch()

            with anyio.fail_after(2):
                while participant.managed_contract(contract) is not None:
                    await anyio.sleep(0)
            assert await tokens.get(controller_token.key) is None

        await participant.aclose()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_participant_filtered_view_ignores_unrelated_authority_without_repair() -> None:
    concord, _contracts, _tokens = _concord()
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    accepted: list[str] = []

    def accept_contract(contract, _record) -> bool:
        accepted.append(contract.contract_id)
        return True

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        participant = concord.participant(
            participant=manager,
            session_id="manager-session",
            profile=TEST_PROFILE,
            accept_contract=accept_contract,
            reconcile_interval=3_600,
            cancel_terminal_statuses=(),
        )
        participant.start(task_group)
        async with participant.watch() as snapshots:
            assert (await _receive(snapshots)).contracts == ()

            unrelated = await concord._create_contract(
                (controller, manager),
                contract_id="unrelated",
                profile="dev.deckr.test.unrelated.v1",
            )
            await concord._attach(
                unrelated,
                controller,
                "controller-session",
                token_id="unrelated-controller-token",
            )

            first = await concord._create_contract(
                (controller, manager),
                contract_id="selected-1",
                profile=TEST_PROFILE,
            )
            await _receive_participant_until_contracts(snapshots, {first.key})

            await concord._attach(
                unrelated,
                manager,
                "other-manager-session",
                token_id="unrelated-manager-token",
            )
            second = await concord._create_contract(
                (controller, manager),
                contract_id="selected-2",
                profile=TEST_PROFILE,
            )
            await _receive_participant_until_contracts(
                snapshots,
                {first.key, second.key},
            )

        assert set(accepted) == {"selected-1", "selected-2"}
        assert "unrelated" not in accepted
        await participant.aclose()
        task_group.cancel_scope.cancel()


async def _receive_until_status(stream, status: ContractValidityStatus):
    with anyio.fail_after(2):
        while True:
            snapshot = await anext(stream)
            if any(state.validity.status == status for state in snapshot.contracts):
                return snapshot


async def _receive_participant_until_status(stream, status: ContractValidityStatus):
    with anyio.fail_after(2):
        while True:
            snapshot = await anext(stream)
            if any(managed.validity.status == status for managed in snapshot.contracts):
                return snapshot


async def _receive_participant_until_contracts(stream, keys: set[str]):
    with anyio.fail_after(2):
        while True:
            snapshot = await anext(stream)
            if {managed.contract.key for managed in snapshot.contracts} == keys:
                return snapshot


class _RecoveringBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str, ttl_seconds: float | None = None) -> None:
        super().__init__(bucket=bucket, ttl_seconds=ttl_seconds)
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

    async def remove_without_publish(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)
            self._advance_mutation()

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[
        anyio.abc.ObjectReceiveStream[KvChange | KvWatchBarrier]
    ]:
        if self._pause_next_watch:
            self._pause_next_watch = False
            self._watch_paused.set()
            await self._resume_watch.wait()
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[
            KvChange | KvWatchBarrier
        ](max_buffer_size=self._buffer_size)
        async with self._lock:
            self._watchers[send] = prefix
            snapshot: tuple[KvEntry, ...] = tuple(
                entry
                for key, entry in sorted(self._entries.items())
                if key.startswith(prefix)
            )
            barrier = KvWatchBarrier(self._revision)

        async def run() -> None:
            for entry in snapshot:
                await send.send(
                    KvChange(self.bucket, entry.key, entry.revision, "put", entry)
                )
            await send.send(barrier)
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


def _hardware_payload() -> HardwareBeaconPayload:
    descriptor = DeviceDescriptor.model_validate(stream_deck_bitmap_grid())
    return HardwareBeaconPayload(
        managerId="manager-main",
        managerEndpoint=hardware_manager_address("manager-main"),
        sessionId="manager-session",
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
