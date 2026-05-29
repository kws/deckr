from __future__ import annotations

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from memory_lane_substrate import MemoryStateStore
from pydantic import ValidationError

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import (
    AdvertisementRecord,
    BeaconDiscovery,
    BeaconFeatureEventType,
    BeaconService,
    CandidateStatus,
    beacon_advertisement_key,
)
from deckr.concord import (
    ConcordCoordinator,
    ConcordEventType,
    ConcordService,
    ContractRecord,
    ContractValidityStatus,
    ParticipantTokenRecord,
    canonical_json_hash,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware.descriptors import ControlRef, DeviceDescriptor, DeviceRef
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
    ACTION_BINDING_PROFILE_ID,
    ACTIONS_FEATURE_ID,
    ActionBindingTerms,
    ActionsBeaconPayload,
    actions_payload_from_advertisement,
    profile_terms_hash,
)
from deckr.state import StateConflict


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


async def _receive_event_type(stream, *event_types):
    with anyio.fail_after(1):
        while True:
            event = await stream.receive()
            if event.event_type in event_types:
                return event


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
        managerAdvertisementId="advertisement-1",
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
        advertiser = service.advertiser(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=endpoint,
            session_id="manager-session",
            advertisement_id="advertisement-1",
            labels={"room": "office"},
            payload=_hardware_payload().to_dict(),
            log_label="TestHardware",
        )
        handle = await advertiser.publish()
        advertised = await _receive(events)
        assert advertised.event_type == BeaconFeatureEventType.ADVERTISED
        assert advertised.candidate is not None
        assert advertised.candidate.advertisement.advertisement_id == (
            handle.advertisement_id
        )

        refreshed = await advertiser.publish(
            payload=_hardware_payload(session_id="manager-session").to_dict()
        )
        updated = await _receive(events)
        assert updated.event_type == BeaconFeatureEventType.UPDATED
        assert updated.candidate is not None
        assert updated.candidate.advertisement.refresh_seq == refreshed.refresh_seq

        assert await advertiser.withdraw()
        withdrawn = await _receive(events)
        assert withdrawn.event_type == BeaconFeatureEventType.WITHDRAWN
        assert withdrawn.previous is not None
        assert withdrawn.previous.advertisement.advertisement_id == "advertisement-1"

    assert "TestHardware Beacon advertisement announced" in caplog.text
    assert "Beacon advertisement withdrawn" in caplog.text


@pytest.mark.asyncio
async def test_beacon_service_feature_watch_reports_expiry(caplog) -> None:
    state = MemoryStateStore(name="beacon")
    service = BeaconService(BeaconDiscovery(state, default_ttl_seconds=30))
    endpoint = hardware_manager_address("manager-main")
    caplog.set_level("INFO", logger="deckr.beacon")

    async with service.watch_feature(HARDWARE_FEATURE_ID) as events:
        handle = await service.advertise(
            HARDWARE_FEATURE_ID,
            endpoint,
            "manager-session",
            advertisement_id="advertisement-1",
            labels={"room": "office"},
            payload=_hardware_payload().to_dict(),
            log_label="TestHardware",
        )
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

    contract = await service.create_contract(
        (manager, controller),
        contract_id="hardware-contract-1",
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller,
    )
    lease = service.participant_lease(
        contract=contract,
        participant=controller,
        session_id="controller-session",
    )
    await lease.attach_or_refresh()
    await service.cancel(contract, controller, reason="test complete")

    with pytest.raises(StateConflict, match="cancelled"):
        await lease.attach_or_refresh()
    with pytest.raises(StateConflict, match="closed"):
        await lease.attach_or_refresh()


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
        contract = await service.create_contract(
            (manager, controller),
            contract_id="hardware-contract-1",
            profile=HARDWARE_CLAIM_PROFILE_ID,
            terms=terms,
            created_by=controller,
            log_label="TestConcord",
        )
        pending = await _receive_event_type(events, ConcordEventType.PENDING)
        assert pending.contract == contract

        controller_lease = service.participant_lease(
            contract=contract,
            participant=controller,
            session_id="controller-session",
            log_label="TestConcord",
        )
        controller_token = await controller_lease.attach_or_refresh()
        refreshed_controller_token = await controller_lease.attach_or_refresh()
        assert refreshed_controller_token.refresh_seq == 2

        manager_lease = service.participant_lease(
            contract=contract,
            participant=manager,
            session_id="manager-session",
            log_label="TestConcord",
        )
        await manager_lease.attach_or_refresh()
        valid = await _receive_event_type(events, ConcordEventType.VALID)
        assert valid.validity is not None
        assert valid.validity.status == ContractValidityStatus.VALID

        await token_state.expire(controller_token.key)
        expired = await _receive_event_type(events, ConcordEventType.TOKEN_EXPIRED)
        assert expired.participant == controller
        assert expired.reason == "token_expired"
        with pytest.raises(StateConflict, match="missing"):
            await controller_lease.attach_or_refresh()
        with pytest.raises(StateConflict, match="closed"):
            await controller_lease.attach_or_refresh()

        assert await service.cancel(
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

    async with concord.watch_contracts() as changes:
        await concord.cancel(contract, controller, reason="done")
        change = await _receive(changes)
    assert change.key == contract.key


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
    binding_terms = ActionBindingTerms(
        bindingId="binding-1",
        controllerEndpoint=controller_address("controller-main"),
        providerEndpoint=action_provider_address("provider-main"),
        providerInstanceId="provider-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
        configId="config-1",
        contextId="context-1",
        hardwareClaimId="claim-1",
        deviceRef=claim_terms.devices[0].device_ref,
        controlRef=ControlRef(
            deviceRef=claim_terms.devices[0].device_ref,
            controlId="key.0.0",
        ),
    )
    assert profile_terms_hash(claim_terms) == canonical_json_hash(claim_terms)
    assert profile_terms_hash(binding_terms) == canonical_json_hash(binding_terms)
    assert binding_terms.profile == ACTION_BINDING_PROFILE_ID

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


def test_beacon_key_helper_uses_feature_namespace_prefix() -> None:
    assert beacon_advertisement_key(
        feature_id=HARDWARE_FEATURE_ID,
        advertisement_id="advertisement-1",
    ).startswith("advertisements.by_feature.")
