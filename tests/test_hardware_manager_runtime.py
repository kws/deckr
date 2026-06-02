from __future__ import annotations

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from memory_lane_substrate import memory_deckr

import deckr.hardware.messages as hw_messages
from deckr.beacon import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    Beacon,
)
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    Concord,
    ContractValidityStatus,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    DeviceDescriptor,
    DeviceRef,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    HardwareManagerRuntime,
)

pytestmark = pytest.mark.asyncio


def _descriptor(device_id: str = "stream-deck-mini") -> DeviceDescriptor:
    payload = stream_deck_bitmap_grid()
    payload["deviceId"] = device_id
    return DeviceDescriptor.model_validate(payload)


def _beacon(deckr) -> Beacon:
    beacon = getattr(deckr, "_test_beacon", None)
    if beacon is None:
        beacon = Beacon(deckr._substrate.kv_bucket(BEACON_ADVERTISEMENT_STORE_POLICY))
        deckr._test_beacon = beacon
    return beacon


def _concord(deckr) -> Concord:
    concord = getattr(deckr, "_test_concord", None)
    if concord is None:
        concord = Concord(
            deckr._substrate.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
            deckr._substrate.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
            deckr._substrate.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
        )
        deckr._test_concord = concord
    return concord


async def _runtime(
    *,
    labels: dict[str, str] | None = None,
    command_handler=None,
    reset_handler=None,
):
    deckr = memory_deckr()
    lane = deckr.lane("hardware_messages")
    endpoint_cm = lane.register_endpoint(hardware_manager_address("manager-main"))
    endpoint = await endpoint_cm.__aenter__()
    runtime = HardwareManagerRuntime(
        endpoint=endpoint,
        beacon=_beacon(deckr),
        concord=_concord(deckr),
        manager_id="manager-main",
        labels=labels,
        command_handler=command_handler,
        reset_handler=reset_handler,
    )
    return deckr, endpoint_cm, runtime


async def _add_device(runtime: HardwareManagerRuntime, descriptor: DeviceDescriptor):
    await runtime.set_device(descriptor)


async def _claim(
    runtime: HardwareManagerRuntime,
    concord: Concord,
    *,
    contract_id: str = "claim-a",
    controller_id: str = "controller-main",
    device_id: str = "stream-deck-mini",
):
    terms = HardwareClaimTerms(
        claimId=contract_id,
        controllerEndpoint=controller_address(controller_id),
        managerEndpoint=hardware_manager_address("manager-main"),
        devices=(
            HardwareClaimDevice(
                deviceRef=DeviceRef(
                    managerId="manager-main",
                    deviceId=device_id,
                    fingerprint=_descriptor(device_id).fingerprint,
                ),
                instanceCount=1,
            ),
        ),
    )
    return await concord._create_contract(
        (controller_address(controller_id), hardware_manager_address("manager-main")),
        contract_id=contract_id,
        profile=HARDWARE_CLAIM_PROFILE_ID,
        terms=terms,
        created_by=controller_address(controller_id),
    )


async def test_runtime_publishes_hardware_beacon_payload_and_capacity() -> None:
    deckr, endpoint_cm, runtime = await _runtime(labels={"room": "office"})
    try:
        await runtime.publish_advertisement()
        descriptor = _descriptor()
        await _add_device(runtime, descriptor)

        candidates = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)
        assert len(candidates) == 1
        payload = HardwareBeaconPayload.model_validate(
            candidates[0].advertisement.payload
        )
        assert payload.manager_id == "manager-main"
        assert payload.manager_endpoint == hardware_manager_address("manager-main")
        assert payload.session_id == runtime.endpoint.session_id
        assert payload.labels == {"room": "office"}
        assert payload.devices[descriptor.device_id].descriptor == descriptor
        assert payload.devices[descriptor.device_id].capacity.claimed_instances == 0
        assert payload.devices[descriptor.device_id].capacity.available_instances == 1
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)


async def test_runtime_attaches_manager_token_and_routes_live_claim_input() -> None:
    delivered_commands = []

    async def command_handler(message):
        delivered_commands.append(message)
        return True

    deckr, endpoint_cm, runtime = await _runtime(command_handler=command_handler)
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.endpoint,
            controller_endpoint.session_id,
        )

        await runtime.reconcile_claims(reason="test")
        validity = await concord._validate(contract)
        assert validity.status == ContractValidityStatus.VALID
        assert len(runtime.live_claims) == 1

        candidates = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)
        payload = HardwareBeaconPayload.model_validate(
            candidates[0].advertisement.payload
        )
        capacity = payload.devices["stream-deck-mini"].capacity
        assert capacity.claimed_instances == 1
        assert capacity.available_instances == 0

        async with controller_endpoint.subscribe() as stream:
            await runtime.handle_hardware_message(
                hw_messages.control_input_message(
                    manager_id="manager-main",
                    sender_session_id=runtime.endpoint.session_id,
                    device_id="stream-deck-mini",
                    fingerprint=_descriptor().fingerprint,
                    control_id="0,0",
                    capability_id="raster.bitmap",
                    event_type="press",
                    value={"eventType": "press"},
                )
            )
            with anyio.fail_after(1):
                routed = await stream.receive()
        assert routed.recipient.endpoint == controller_endpoint.endpoint
        assert routed.recipient_session_id == controller_endpoint.session_id

        command = hw_messages.control_command_message(
            controller_id="controller-main",
            sender_session_id=controller_endpoint.session_id,
            manager_id="manager-main",
            device_id="stream-deck-mini",
            control_id="0,0",
            capability_id="raster.bitmap",
            command_type="clear",
        )
        assert await runtime.handle_command(command)
        assert delivered_commands == [command]
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_noop_claim_reconcile_does_not_refresh_hardware_beacon() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    try:
        await _add_device(runtime, _descriptor())
        beacon = _beacon(deckr)
        first = beacon.candidates(HARDWARE_FEATURE_ID)[0]

        await runtime.reconcile_claims(reason="test noop")
        second = beacon.candidates(HARDWARE_FEATURE_ID)[0]

        assert second.advertisement.refresh_seq == first.advertisement.refresh_seq
        assert second.revision == first.revision
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)


async def test_runtime_matches_live_claim_without_beacon_advertisement() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await _add_device(runtime, _descriptor())
        await runtime.withdraw_advertisement()
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.endpoint,
            controller_endpoint.session_id,
        )

        await runtime.reconcile_claims(reason="test direct claim")

        assert len(runtime.live_claims) == 1
        assert runtime.live_claims[0].terms.claim_id == "claim-a"
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_unclaimed_commands_are_rejected() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        command = hw_messages.control_command_message(
            controller_id="controller-main",
            sender_session_id=controller_endpoint.session_id,
            manager_id="manager-main",
            device_id="stream-deck-mini",
            control_id="0,0",
            capability_id="raster.bitmap",
            command_type="clear",
        )
        async with controller_endpoint.subscribe() as stream:
            assert not await runtime.handle_command(command)
            with anyio.fail_after(1):
                rejected = await stream.receive()
        body = hw_messages.hardware_body_from_message(rejected)
        assert isinstance(body, hw_messages.CommandRejectedMessage)
        assert body.reason == "unauthorized"
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_cancelled_claim_resets_device_and_releases_capacity() -> None:
    reset_devices: list[str] = []

    async def reset_handler(device_id: str) -> None:
        reset_devices.append(device_id)

    deckr, endpoint_cm, runtime = await _runtime(reset_handler=reset_handler)
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.endpoint,
            controller_endpoint.session_id,
        )
        await runtime.reconcile_claims(reason="test live")
        assert len(runtime.live_claims) == 1

        await concord._cancel(contract, controller_endpoint.endpoint, reason="test")
        await runtime.reconcile_claims(reason="test cancel")
        assert reset_devices == ["stream-deck-mini"]
        assert runtime.live_claims == ()

        candidates = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)
        payload = HardwareBeaconPayload.model_validate(
            candidates[0].advertisement.payload
        )
        assert payload.devices["stream-deck-mini"].capacity.claimed_instances == 0
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_remove_device_cancels_live_claim_contract_without_lane_event() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.endpoint,
            controller_endpoint.session_id,
        )
        await runtime.reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        async with controller_endpoint.subscribe() as stream:
            await runtime.remove_device("stream-deck-mini", reason="disconnected")
            with anyio.move_on_after(0.05) as scope:
                await stream.receive()
            assert scope.cancel_called

        assert (await concord._validate(contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert runtime.live_claims == ()
        candidates = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)
        payload = HardwareBeaconPayload.model_validate(
            candidates[0].advertisement.payload
        )
        assert "stream-deck-mini" not in payload.devices
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_replace_devices_cancels_live_claim_contract_for_removed_device() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.lane("hardware_messages").register_endpoint(
        controller_address("controller-main")
    )
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.endpoint,
            controller_endpoint.session_id,
        )
        await runtime.reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        await runtime.replace_devices({}, removed_reason="removed")

        assert (await concord._validate(contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert runtime.live_claims == ()
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_competing_claims_choose_existing_or_lowest_contract_key() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    lane = deckr.lane("hardware_messages")
    controller_a_cm = lane.register_endpoint(controller_address("controller-a"))
    controller_b_cm = lane.register_endpoint(controller_address("controller-b"))
    controller_a = await controller_a_cm.__aenter__()
    controller_b = await controller_b_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime.publish_advertisement()
        await _add_device(runtime, _descriptor())
        claim_b = await _claim(
            runtime,
            concord,
            contract_id="claim-b",
            controller_id="controller-b",
        )
        claim_a = await _claim(
            runtime,
            concord,
            contract_id="claim-a",
            controller_id="controller-a",
        )
        await concord._attach(claim_b, controller_b.endpoint, controller_b.session_id)
        await concord._attach(claim_a, controller_a.endpoint, controller_a.session_id)

        await runtime.reconcile_claims(reason="test competing")
        assert [claim.terms.claim_id for claim in runtime.live_claims] == ["claim-a"]
        assert (await concord._validate(claim_a)).status == ContractValidityStatus.VALID
        assert (await concord._validate(claim_b)).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_a_cm.__aexit__(None, None, None)
        await controller_b_cm.__aexit__(None, None, None)
