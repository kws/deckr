from __future__ import annotations

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from memory_lane_substrate import MemoryStateStore
from pydantic import ValidationError

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import (
    AdvertisementRecord,
    BeaconAdvertisementSpec,
    BeaconDiscovery,
    BeaconFeatureEventType,
    BeaconService,
    CandidateStatus,
    beacon_advertisement_key,
)
from deckr.concord import (
    ConcordAgreementSpec,
    ConcordCoordinator,
    ConcordEventType,
    ConcordManagedContractEventType,
    ConcordParticipantManager,
    ConcordService,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
    canonical_json_hash,
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
from deckr.state import StateConflict, StateUnavailable


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


class RacingUpdateStateStore:
    def __init__(self, inner: MemoryStateStore) -> None:
        self._inner = inner
        self.raced = False

    async def get(self, *args, **kwargs):
        return await self._inner.get(*args, **kwargs)

    async def items(self, *args, **kwargs):
        return await self._inner.items(*args, **kwargs)

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
        raise StateUnavailable("watch unavailable")

    async def __aexit__(self, *args):
        return None


class FailingWatchStateStore(MemoryStateStore):
    def watch(self, prefix: str = ""):
        del prefix
        return UnavailableWatch()


class CountingItemsStateStore(MemoryStateStore):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.items_prefixes: list[str] = []

    async def items(self, prefix: str = ""):
        self.items_prefixes.append(prefix)
        return await super().items(prefix)


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


def test_beacon_service_exposes_only_managed_advertisement_lifecycle() -> None:
    for name in ("advertise", "refresh", "withdraw", "advertiser"):
        assert not hasattr(BeaconService, name)


@pytest.mark.asyncio
async def test_beacon_create_refresh_withdraw_find_watch_and_validate() -> None:
    state = MemoryStateStore(name="beacon")
    beacon = BeaconDiscovery(state, default_ttl_seconds=30)
    endpoint = hardware_manager_address("manager-main")

    async with beacon.watch(HARDWARE_FEATURE_ID) as changes:
        handle = await beacon.advertise(
            HARDWARE_FEATURE_ID,
            endpoint,
            "manager-session",
            advertisement_id="advertisement-1",
            labels={"room": "office"},
            payload=_hardware_payload().to_dict(),
        )
        change = await _receive(changes)

    assert change.key == handle.key
    candidates = await beacon.find(HARDWARE_FEATURE_ID)
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

    refreshed = await beacon.refresh(handle, hints={"load": "light"})
    assert refreshed.refresh_seq == 2
    assert (await beacon.find(HARDWARE_FEATURE_ID))[0].advertisement.hints == {
        "load": "light"
    }

    with pytest.raises(StateConflict):
        await beacon.advertise(
            HARDWARE_FEATURE_ID,
            endpoint,
            "manager-session",
            advertisement_id="advertisement-1",
        )

    assert await beacon.withdraw(refreshed)
    assert await beacon.validate(candidates[0]) == CandidateStatus.MISSING

    invalid = await beacon.advertise(
        HARDWARE_FEATURE_ID,
        endpoint,
        "manager-session",
        advertisement_id="advertisement-2",
    )
    invalid_candidate = (await beacon.find(HARDWARE_FEATURE_ID))[0]
    await state.put(
        invalid.key,
        {
            "schema": "dev.deckr.beacon.advertisement.v1",
            "advertisementId": "advertisement-2",
        },
    )
    assert await beacon.validate(invalid_candidate) == CandidateStatus.SCHEMA_INVALID


@pytest.mark.asyncio
async def test_beacon_service_advertiser_emits_semantic_events_and_logs(caplog) -> None:
    state = MemoryStateStore(name="beacon")
    service = BeaconService(BeaconDiscovery(state, default_ttl_seconds=30))
    endpoint = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.beacon")

    async with service.watch_feature(HARDWARE_FEATURE_ID) as events:
        advertisement = await service.ensure_advertisement(
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
        handle = await advertisement.publish()
        advertised = await _receive(events)
        assert advertised.event_type == BeaconFeatureEventType.ADVERTISED
        assert advertised.candidate is not None
        assert advertised.candidate.advertisement.advertisement_id == (
            handle.advertisement_id
        )

        refreshed = await advertisement.publish(
            payload=_hardware_payload(session_id="manager-session").to_dict()
        )
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
async def test_beacon_service_managed_advertisement_reuses_cached_lifecycle() -> None:
    service = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    spec = BeaconAdvertisementSpec(
        feature_id=HARDWARE_FEATURE_ID,
        endpoint=hardware_manager_address("manager-main"),
        session_id="manager-session",
        advertisement_id="advertisement-1",
        payload=_hardware_payload().to_dict(),
    )

    first = await service.ensure_advertisement(spec)
    assert await service.ensure_advertisement(spec) is first

    dynamic_spec = BeaconAdvertisementSpec(
        feature_id=HARDWARE_FEATURE_ID,
        endpoint=hardware_manager_address("manager-main"),
        session_id="manager-session",
        payload=_hardware_payload().to_dict(),
    )
    dynamic = await service.ensure_advertisement(dynamic_spec)
    assert await service.ensure_advertisement(dynamic_spec) is dynamic

    await first.aclose()
    replacement = await service.ensure_advertisement(spec)
    assert replacement is not first


@pytest.mark.asyncio
async def test_beacon_managed_publish_serializes_concurrent_refreshes() -> None:
    service = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    advertisement = await service.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="manager-session",
            advertisement_id="advertisement-1",
            payload=_hardware_payload().to_dict(),
        )
    )
    await advertisement.publish()
    refreshes = []

    async def publish(hint: str) -> None:
        refreshes.append(await advertisement.publish(hints={"publish": hint}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(publish, "a")
        tg.start_soon(publish, "b")

    candidate = (await service.find(HARDWARE_FEATURE_ID))[0]
    assert sorted(handle.refresh_seq for handle in refreshes) == [2, 3]
    assert candidate.advertisement.refresh_seq == 3


@pytest.mark.asyncio
async def test_beacon_find_returns_newest_revision_first() -> None:
    service = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    old = await service.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="old-session",
            advertisement_id="a-old",
            payload=_hardware_payload(session_id="old-session").to_dict(),
        )
    )
    new = await service.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="new-session",
            advertisement_id="z-new",
            payload=_hardware_payload(session_id="new-session").to_dict(),
        )
    )

    await old.publish()
    await new.publish()

    candidates = await service.find(HARDWARE_FEATURE_ID)

    assert [candidate.advertisement.advertisement_id for candidate in candidates] == [
        "z-new",
        "a-old",
    ]
    assert [candidate.revision for candidate in candidates] == [2, 1]


@pytest.mark.asyncio
async def test_beacon_find_treats_refresh_as_newest_write() -> None:
    service = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))
    old = await service.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="old-session",
            advertisement_id="a-old",
            payload=_hardware_payload(session_id="old-session").to_dict(),
        )
    )
    new = await service.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address("manager-main"),
            session_id="new-session",
            advertisement_id="z-new",
            payload=_hardware_payload(session_id="new-session").to_dict(),
        )
    )

    await old.publish()
    await new.publish()
    await old.publish(hints={"refreshed": "true"})

    candidates = await service.find(HARDWARE_FEATURE_ID)

    assert [candidate.advertisement.advertisement_id for candidate in candidates] == [
        "a-old",
        "z-new",
    ]
    assert [candidate.revision for candidate in candidates] == [3, 2]
    assert candidates[0].advertisement.refresh_seq == 2


@pytest.mark.asyncio
async def test_beacon_managed_close_before_publish_prevents_heartbeat_advertisement() -> None:
    service = BeaconService(BeaconDiscovery(MemoryStateStore(name="beacon")))

    async with anyio.create_task_group() as tg:
        advertisement = await service.ensure_advertisement(
            BeaconAdvertisementSpec(
                feature_id=HARDWARE_FEATURE_ID,
                endpoint=hardware_manager_address("manager-main"),
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload=_hardware_payload().to_dict(),
                refresh_interval=0.01,
            ),
            start_soon=tg.start_soon,
        )
        await advertisement.aclose()
        await anyio.sleep(0.03)
        tg.cancel_scope.cancel()

    assert await service.find(HARDWARE_FEATURE_ID) == ()


@pytest.mark.asyncio
async def test_beacon_service_feature_watch_preserves_caller_state_unavailable() -> None:
    state = MemoryStateStore(name="beacon")
    service = BeaconService(BeaconDiscovery(state, default_ttl_seconds=30))

    with pytest.raises(StateUnavailable, match="broker unavailable"):
        async with service.watch_feature(HARDWARE_FEATURE_ID):
            raise StateUnavailable("broker unavailable")


@pytest.mark.asyncio
async def test_beacon_service_feature_watch_preserves_source_state_unavailable() -> None:
    state = FailingWatchStateStore(name="beacon")
    service = BeaconService(BeaconDiscovery(state, default_ttl_seconds=30))

    with pytest.raises(StateUnavailable, match="watch unavailable"):
        async with service.watch_feature(HARDWARE_FEATURE_ID) as events:
            await events.receive()


@pytest.mark.asyncio
async def test_beacon_service_feature_watch_reports_expiry(caplog) -> None:
    state = MemoryStateStore(name="beacon")
    service = BeaconService(BeaconDiscovery(state, default_ttl_seconds=30))
    endpoint = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.beacon")

    async with service.watch_feature(HARDWARE_FEATURE_ID) as events:
        advertisement = await service.ensure_advertisement(
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
        handle = await advertisement.publish()
        await _receive(events)
        await state.expire(handle.key)
        expired = await _receive(events)

    assert expired.event_type == BeaconFeatureEventType.EXPIRED
    assert expired.reason == "expire"
    assert "Beacon advertisement expired" in caplog.text


@pytest.mark.asyncio
async def test_concord_create_attach_refresh_validate_cancel_and_token_loss() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    concord = ConcordCoordinator(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord.create_contract(
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

    controller_token = await concord.attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )
    assert (await concord.validate(contract)).status == (
        ContractValidityStatus.NOT_YET_FULFILLED
    )

    manager_token = await concord.attach(
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

    refreshed = await concord.refresh(controller_token)
    assert refreshed.refresh_seq == 2
    assert (
        await concord.validate(
            contract,
            current_sessions={str(controller): "old-controller-session"},
        )
    ).status == ContractValidityStatus.SESSION_MISMATCH

    await token_state.delete(manager_token.key, revision=manager_token.revision)
    missing = await concord.validate(contract)
    assert missing.status == ContractValidityStatus.MISSING_TOKEN
    with pytest.raises(StateConflict, match="already attached"):
        await concord.attach(
            contract,
            manager,
            "manager-session",
            token_id="manager-token-2",
        )

    async with concord.watch(contract) as changes:
        assert await concord.cancel(contract, controller, reason="test complete")
        change = await _receive(changes)
    assert change.key == contract.key
    assert (await concord.validate(contract)).status == ContractValidityStatus.CANCELLED
    with pytest.raises(StateConflict, match="cancelled"):
        await concord.attach(contract, controller, "new-session")


@pytest.mark.asyncio
async def test_concord_refresh_returns_latest_token_after_revision_race() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = RacingUpdateStateStore(MemoryStateStore(name="tokens"))
    concord = ConcordCoordinator(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord.create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    controller_token = await concord.attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )

    refreshed = await concord.refresh(controller_token)

    assert token_state.raced
    assert refreshed.refresh_seq == 2
    assert refreshed.revision != controller_token.revision


@pytest.mark.asyncio
async def test_concord_participant_lease_closes_after_cancelled_contract() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    with pytest.raises(StateConflict, match="cancelled"):
        await lease.attach_or_refresh()
    with pytest.raises(StateConflict, match="closed"):
        await lease.attach_or_refresh()


@pytest.mark.asyncio
async def test_concord_participant_lease_rate_limits_token_writes() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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
    refreshed = await lease.attach_or_refresh()

    assert refreshed.refresh_seq == first.refresh_seq + 1
    assert refreshed.revision != first.revision


@pytest.mark.asyncio
async def test_concord_participant_lease_adopts_without_immediate_refresh() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    await anyio.sleep(0.02)
    refreshed = await lease.attach_or_refresh()

    assert refreshed.refresh_seq == manager_token.refresh_seq + 1
    assert refreshed.revision != manager_token.revision


@pytest.mark.asyncio
async def test_concord_participant_manager_attaches_adopts_and_filters() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    manager_lifecycle = service.participant_manager(
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

    adopted_lifecycle = service.participant_manager(
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
async def test_concord_participant_manager_steady_reconcile_uses_watch_index() -> None:
    contract_state = CountingItemsStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(
        ConcordCoordinator(contract_state, token_state, token_ttl_seconds=30)
    )
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
    lifecycle = service.participant_manager(
        participant=manager,
        session_id="manager-session",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        refresh_interval=30.0,
        reconcile_interval=0.05,
        accept_contract=lambda _contract, _record: True,
    )

    async with anyio.create_task_group() as task_group:
        lifecycle.start(task_group)
        with anyio.fail_after(1):
            while True:
                managed = lifecycle.managed_contract(contract)
                if managed is not None and managed.token is not None:
                    break
                await anyio.sleep(0.01)

        assert contract_state.items_prefixes
        contract_state.items_prefixes.clear()
        refresh_seq = managed.token.refresh_seq

        await lifecycle.reconcile(reason="steady reconcile")
        await lifecycle.reconcile(reason="steady reconcile again")
        task_group.cancel_scope.cancel()

    managed = lifecycle.managed_contract(contract)
    assert managed is not None
    assert managed.token is not None
    assert managed.token.refresh_seq == refresh_seq
    assert contract_state.items_prefixes == []


@pytest.mark.asyncio
async def test_concord_participant_manager_watch_periodic_and_valid_dedupe() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(
        ConcordCoordinator(contract_state, token_state, token_ttl_seconds=30)
    )
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = service.participant_manager(
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

        with anyio.fail_after(1):
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
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = ConcordParticipantManager(
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

        await token_state.expire(controller_token.key)
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
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    lifecycle = service.participant_manager(
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

        await token_state.expire(controller_token.key)
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
async def test_concord_participant_manager_policy_rejection_does_not_cancel() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    contract = await service._create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=_hardware_claim_terms(),
        created_by=controller,
    )
    lifecycle = service.participant_manager(
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
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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
    await token_state.delete(token.key, revision=token.revision)
    lifecycle = service.participant_manager(
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
async def test_concord_watch_can_suppress_lifecycle_logging(caplog) -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.concord")

    async with service.watch_contracts(
        HARDWARE_CLAIM_PROFILE_ID,
        log_events=False,
    ) as events:
        caplog.clear()
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        event = await _receive_event_type(events, ConcordEventType.PENDING)

    assert event.contract is not None
    assert event.contract.contract_id == contract.contract_id
    assert "Concord contract pending" not in caplog.text


@pytest.mark.asyncio
async def test_concord_contract_notifications_do_not_validate_or_fetch_contracts() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    async with service.watch_contract_notifications(
        HARDWARE_CLAIM_PROFILE_ID,
    ) as notifications:
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=_hardware_claim_terms(),
            created_by=controller,
        )
        contract_notification = await _receive_notification_source(
            notifications,
            "contract",
        )
        await service._attach(contract, controller, "controller-session")
        token_notification = await _receive_notification_source(
            notifications,
            "token",
        )

    assert contract_notification.operation == "put"
    assert contract_notification.contract == contract
    assert contract_notification.profile == HARDWARE_CLAIM_PROFILE_ID
    assert token_notification.operation == "put"
    assert token_notification.contract_id == contract.contract_id
    assert token_notification.generation == contract.generation
    assert token_notification.participant == controller


@pytest.mark.asyncio
async def test_concord_service_watch_preserves_caller_state_unavailable() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))

    with pytest.raises(StateUnavailable, match="broker unavailable"):
        async with service.watch_contracts():
            raise StateUnavailable("broker unavailable")


@pytest.mark.asyncio
async def test_concord_service_watch_preserves_source_state_unavailable() -> None:
    contract_state = FailingWatchStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))

    with pytest.raises(StateUnavailable, match="watch unavailable"):
        async with service.watch_contracts() as events:
            await events.receive()


@pytest.mark.asyncio
async def test_concord_service_use_missing_token_logs_below_info(caplog) -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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
    await token_state.delete(client_token.key, revision=client_token.revision)

    caplog.set_level("INFO", logger="deckr.concord")
    caplog.clear()
    validity = await service._validate(contract)

    assert validity.status == ContractValidityStatus.MISSING_TOKEN
    assert "Concord contract invalid" not in caplog.text


@pytest.mark.asyncio
async def test_concord_ensure_agreement_supersedes_stable_token_loss() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    agreement = await service.ensure_agreement(spec)
    await service._attach(agreement.contract, service_endpoint, "service-session")
    assert (await agreement.refresh()).status == ContractValidityStatus.VALID
    assert agreement.local_token is not None
    await token_state.delete(
        agreement.local_token.key,
        revision=agreement.local_token.revision,
    )

    successor = await service.ensure_agreement(spec)

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
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    agreement = await service.ensure_agreement(
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
    service = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
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

    assert await service.find_contracts(
        HARDWARE_CLAIM_PROFILE_ID,
        contract_id="hardware-contract-1",
    ) == (contract,)
    assert await service.contract_record(contract) == await service._contract_record(contract)
    with pytest.raises(ValueError, match="Concord contract id"):
        await service.find_contracts(contract_id="")
    with pytest.raises(ValueError, match="participant"):
        await service.cancel_contract(
            contract,
            service_address("not-a-participant"),
            reason="test",
        )

    assert await service.cancel_contract(contract, controller, reason="test")
    record = await service.contract_record(contract)
    assert record is not None
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == "test"


@pytest.mark.asyncio
async def test_concord_ensure_agreement_generated_id_is_fresh() -> None:
    service = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
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

    first = await service.ensure_agreement(spec)
    second = await service.ensure_agreement(spec)

    assert first.contract_id != second.contract_id
    assert first.generation == 1
    assert second.generation == 1
    assert first.local_token is not None
    assert second.local_token is not None


@pytest.mark.asyncio
async def test_concord_participant_manager_factory_reconciles() -> None:
    service = ConcordService(
        ConcordCoordinator(
            MemoryStateStore(name="contracts"),
            MemoryStateStore(name="tokens"),
        )
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
    lifecycle = service.participant_manager(
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
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()
    caplog.set_level("INFO", logger="deckr.concord")

    async with service.watch_contracts(HARDWARE_CLAIM_PROFILE_ID) as events:
        contract = await service._create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=terms,
            created_by=controller,
            log_label="TestConcord",
        )
        pending = await _receive_event_type(events, ConcordEventType.PENDING)
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
        await anyio.sleep(0.02)
        refreshed_controller_token = await controller_lease.attach_or_refresh()
        assert refreshed_controller_token.refresh_seq == 2

        manager_lease = service._participant_lease(
            contract=contract,
            participant=manager,
            session_id="manager-session",
            log_label="TestConcord",
        )
        await manager_lease.attach_or_refresh()
        valid = await _receive_event_type(events, ConcordEventType.VALID)
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

        await token_state.expire(controller_token.key)
        expired = await _receive_event_type(events, ConcordEventType.TOKEN_EXPIRED)
        assert expired.participant == controller
        assert expired.reason == "token_expired"
        with pytest.raises(StateConflict, match="missing"):
            await controller_lease.attach_or_refresh()
        with pytest.raises(StateConflict, match="closed"):
            await controller_lease.attach_or_refresh()

        assert await service._cancel(
            contract,
            controller,
            reason="test complete",
            log_label="TestConcord",
        )
        cancelled = await _receive_event_type(events, ConcordEventType.CANCELLED)
        assert cancelled.reason is None

    assert "TestConcord Concord contract opened" in caplog.text
    assert "TestConcord Concord participant token attached" in caplog.text
    assert "Concord participant token expired" in caplog.text
    assert "TestConcord Concord contract cancelled" in caplog.text


@pytest.mark.asyncio
async def test_concord_find_and_watch_contracts() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    concord = ConcordCoordinator(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()

    contract = await concord.create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    other = await concord.create_contract(
        (manager, controller),
        contract_id="other-contract-1",
        profile="dev.deckr.profile.other.v1",
        created_by=controller,
    )
    await contract_state.put(
        "contracts.not-a-contract",
        {"schema": "dev.deckr.concord.contract.v1"},
    )

    assert await concord.find_contracts() == (contract, other)
    assert await concord.find_contracts(HARDWARE_CLAIM_PROFILE_ID) == (contract,)
    assert await concord.find_contracts(contract_id="hardware-contract-1") == (contract,)
    assert await concord.find_contracts(contract_id="missing") == ()

    async with concord.watch_contracts() as changes:
        await concord.cancel(contract, controller, reason="done")
        change = await _receive(changes)
    assert change.key == contract.key


@pytest.mark.asyncio
async def test_concord_stable_agreement_lookup_uses_contract_id_prefix() -> None:
    contract_state = CountingItemsStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    service = ConcordService(ConcordCoordinator(contract_state, token_state))
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

    agreement = await service.ensure_agreement(spec)

    assert agreement.contract.key == contract.key
    assert contract_state.items_prefixes == ["contracts.service-use-openhab."]


@pytest.mark.asyncio
async def test_concord_duplicate_contract_and_generation_mismatch_are_rejected() -> None:
    contract_state = MemoryStateStore(name="contracts")
    token_state = MemoryStateStore(name="tokens")
    concord = ConcordCoordinator(contract_state, token_state)
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    terms = _hardware_claim_terms()
    contract = await concord.create_contract(
        (controller, manager),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
    )
    await concord.attach(contract, controller, "controller-session")
    manager_token = await concord.attach(contract, manager, "manager-session")

    with pytest.raises(StateConflict):
        await concord.create_contract(
            (controller, manager),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=terms,
        )

    token_entry = await token_state.get(manager_token.key)
    assert token_entry is not None
    token = ParticipantTokenRecord.model_validate(token_entry.value)
    mutated = token.model_copy(update={"generation": 2})
    await token_state.put(manager_token.key, mutated)

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
