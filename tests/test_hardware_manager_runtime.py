from __future__ import annotations

import anyio
import pytest
from descriptor_fixtures import stream_deck_bitmap_grid
from message_bus_mocks import mock_deckr

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
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    HardwareManagerRuntime,
)

pytestmark = pytest.mark.asyncio


def _descriptor(
    device_id: str = "stream-deck-mini",
    *,
    fingerprint: str | None = None,
) -> DeviceDescriptor:
    payload = stream_deck_bitmap_grid()
    payload["deviceId"] = device_id
    if fingerprint is not None:
        payload["fingerprint"] = fingerprint
    return DeviceDescriptor.model_validate(payload)


def _beacon(deckr) -> Beacon:
    beacon = getattr(deckr, "_test_beacon", None)
    if beacon is None:
        beacon = Beacon(deckr._message_bus.kv_bucket(BEACON_ADVERTISEMENT_STORE_POLICY))
        deckr._test_beacon = beacon
    return beacon


def _concord(deckr) -> Concord:
    concord = getattr(deckr, "_test_concord", None)
    if concord is None:
        concord = Concord(
            deckr._message_bus.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
            deckr._message_bus.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
            deckr._message_bus.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
        )
        deckr._test_concord = concord
    return concord


def _advertised_payload(deckr) -> HardwareBeaconPayload:
    candidates = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)
    assert len(candidates) == 1
    return HardwareBeaconPayload.model_validate(candidates[0].advertisement.payload)


def _last_reply_body(deckr) -> hw_messages.HardwareMessageBody:
    reply = deckr._message_bus.publish_reply.call_args.args[0]
    return hw_messages.hardware_body_from_message(reply)


async def _wait_until(predicate, *, message: str) -> None:
    for _ in range(100):
        if predicate():
            return
        await anyio.sleep(0.01)
    raise AssertionError(message)


async def _send_to_runtime_subscription(deckr, message) -> None:
    for _ in range(100):
        if deckr._message_bus.subscriptions:
            context = deckr._message_bus.subscriptions[-1]
            if context.entered:
                await context._send.send(message)
                return
        await anyio.sleep(0.01)
    raise AssertionError("runtime command subscription did not start")


def _capability_state_request_message(
    *,
    sender_session_id: str,
    controller_id: str = "controller-main",
    manager_id: str = "manager-main",
    device_id: str = "stream-deck-mini",
    capability_id: str = "raster.bitmap",
    control_id: str | None = "0,0",
    state_type: str | None = "bitmap",
    recipient_session_id: str | None = None,
) -> hw_messages.DeckrMessage:
    device_ref = DeviceRef(managerId=manager_id, deviceId=device_id)
    body = hw_messages.CapabilityStateRequestMessage(
        deviceRef=device_ref,
        controlId=control_id,
        capabilityId=capability_id,
        stateType=state_type,
    )
    return hw_messages.hardware_message(
        sender=controller_address(controller_id),
        sender_session_id=sender_session_id,
        recipient=hardware_manager_address(manager_id),
        recipient_session_id=recipient_session_id,
        message_type=hw_messages.CAPABILITY_STATE_REQUEST,
        body=body,
        subject=hw_messages.hardware_subject_for_capability(
            CapabilityRef(
                deviceRef=device_ref,
                controlId=control_id,
                capabilityId=capability_id,
            )
        ),
    )


async def _runtime(
    *,
    labels: dict[str, str] | None = None,
    command_handler=None,
    reset_handler=None,
):
    deckr = mock_deckr()
    endpoint_cm = deckr.endpoint(hardware_manager_address("manager-main"))
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
        await runtime._publish_advertisement()
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
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )

        await runtime._reconcile_claims(reason="test")
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

        deckr._message_bus.publish.reset_mock()
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
        routed = deckr._message_bus.publish.call_args.args[0]
        assert routed.recipient.endpoint == controller_endpoint.address
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
        assert await runtime._handle_command(command)
        assert delivered_commands == [command]
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_command_authorization_reconciles_fresh_claim_before_rejecting() -> None:
    delivered_commands = []

    async def command_handler(message):
        delivered_commands.append(message)
        return True

    deckr, endpoint_cm, runtime = await _runtime(command_handler=command_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        assert runtime.live_claims == ()

        command = hw_messages.control_command_message(
            controller_id="controller-main",
            sender_session_id=controller_endpoint.session_id,
            manager_id="manager-main",
            device_id="stream-deck-mini",
            control_id="0,0",
            capability_id="raster.bitmap",
            command_type="clear",
        )
        deckr._message_bus.publish_reply.reset_mock()

        assert await runtime._handle_command(command)
        assert delivered_commands == [command]
        assert len(runtime.live_claims) == 1
        deckr._message_bus.publish_reply.assert_not_called()
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_start_subscription_and_stop_withdraw_beacon_and_authority() -> None:
    delivered_commands = []

    async def command_handler(message):
        delivered_commands.append(message)
        return True

    deckr, endpoint_cm, runtime = await _runtime(command_handler=command_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    stopped = False
    try:
        async with anyio.create_task_group() as task_group:
            try:
                await runtime.start(task_group)
                await _wait_until(
                    lambda: bool(deckr._message_bus.subscriptions)
                    and deckr._message_bus.subscriptions[-1].entered,
                    message="runtime command subscription did not start",
                )
                assert len(_beacon(deckr).candidates(HARDWARE_FEATURE_ID)) == 1

                await _add_device(runtime, _descriptor())
                contract = await _claim(runtime, concord)
                await concord._attach(
                    contract,
                    controller_endpoint.address,
                    controller_endpoint.session_id,
                )

                command = hw_messages.control_command_message(
                    controller_id="controller-main",
                    sender_session_id=controller_endpoint.session_id,
                    manager_id="manager-main",
                    device_id="stream-deck-mini",
                    control_id="0,0",
                    capability_id="raster.bitmap",
                    command_type="clear",
                )
                deckr._message_bus.publish_reply.reset_mock()
                await _send_to_runtime_subscription(deckr, command)
                await _wait_until(
                    lambda: len(delivered_commands) == 1,
                    message="runtime did not deliver subscribed command",
                )
                assert delivered_commands == [command]
                deckr._message_bus.publish_reply.assert_not_called()
                assert len(runtime.live_claims) == 1

                await runtime.stop()
                stopped = True
                assert _beacon(deckr).candidates(HARDWARE_FEATURE_ID) == ()
                assert runtime.live_claims == ()

                deckr._message_bus.publish_reply.reset_mock()
                await _send_to_runtime_subscription(deckr, command)
                await _wait_until(
                    lambda: deckr._message_bus.publish_reply.called,
                    message="runtime did not reject command after stop",
                )
                assert delivered_commands == [command]
                rejected_body = _last_reply_body(deckr)
                assert isinstance(rejected_body, hw_messages.CommandRejectedMessage)
                assert rejected_body.reason == "unauthorized"
                assert rejected_body.message == "Hardware command unauthorized"
            finally:
                task_group.cancel_scope.cancel()
    finally:
        if not stopped:
            await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_noop_claim_reconcile_does_not_refresh_hardware_beacon() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    try:
        await _add_device(runtime, _descriptor())
        beacon = _beacon(deckr)
        first = beacon.candidates(HARDWARE_FEATURE_ID)[0]

        await runtime._reconcile_claims(reason="test noop")
        second = beacon.candidates(HARDWARE_FEATURE_ID)[0]

        assert second.advertisement.refresh_seq == first.advertisement.refresh_seq
        assert second.revision == first.revision
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)


async def test_runtime_matches_live_claim_without_beacon_advertisement() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await _add_device(runtime, _descriptor())
        await runtime._withdraw_advertisement()
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )

        await runtime._reconcile_claims(reason="test direct claim")

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
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    try:
        await runtime._publish_advertisement()
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
        deckr._message_bus.publish_reply.reset_mock()
        assert not await runtime._handle_command(command)
        rejected = deckr._message_bus.publish_reply.call_args.args[0]
        body = hw_messages.hardware_body_from_message(rejected)
        assert isinstance(body, hw_messages.CommandRejectedMessage)
        assert body.reason == "unauthorized"
        assert body.message == "Hardware command unauthorized"
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_command_rejection_wire_replies_match_runtime_contract() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")

        unsupported_command = hw_messages.control_command_message(
            controller_id="controller-main",
            sender_session_id=controller_endpoint.session_id,
            manager_id="manager-main",
            device_id="stream-deck-mini",
            control_id="0,0",
            capability_id="raster.bitmap",
            command_type="unsupported",
        )
        deckr._message_bus.publish_reply.reset_mock()
        assert not await runtime._handle_command(unsupported_command)
        unsupported_body = _last_reply_body(deckr)
        assert isinstance(unsupported_body, hw_messages.CommandRejectedMessage)
        assert unsupported_body.reason == "unsupported"
        assert unsupported_body.message == "Hardware command unsupported"

        unsupported_state = _capability_state_request_message(
            sender_session_id=controller_endpoint.session_id
        )
        deckr._message_bus.publish_reply.reset_mock()
        assert not await runtime._handle_command(unsupported_state)
        unsupported_state_body = _last_reply_body(deckr)
        assert isinstance(
            unsupported_state_body,
            hw_messages.CapabilityStateReplyMessage,
        )
        assert unsupported_state_body.status == "unsupported"
        assert unsupported_state_body.error == "Hardware state request unsupported"

        stale_state = _capability_state_request_message(
            sender_session_id=controller_endpoint.session_id,
            device_id="missing-device",
        )
        deckr._message_bus.publish_reply.reset_mock()
        assert not await runtime._handle_command(stale_state)
        stale_state_body = _last_reply_body(deckr)
        assert isinstance(stale_state_body, hw_messages.CapabilityStateReplyMessage)
        assert stale_state_body.status == "rejected"
        assert stale_state_body.error == "Hardware state request stale"
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_cancelled_claim_resets_device_and_releases_capacity() -> None:
    reset_devices: list[str] = []

    async def reset_handler(device_id: str) -> None:
        reset_devices.append(device_id)

    deckr, endpoint_cm, runtime = await _runtime(reset_handler=reset_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert len(runtime.live_claims) == 1

        await concord._cancel(contract, controller_endpoint.address, reason="test")
        await runtime._reconcile_claims(reason="test cancel")
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


async def test_set_device_replacement_cancels_live_claim_and_resets() -> None:
    reset_devices: list[str] = []

    async def reset_handler(device_id: str) -> None:
        reset_devices.append(device_id)

    deckr, endpoint_cm, runtime = await _runtime(reset_handler=reset_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        replacement = _descriptor(
            fingerprint="usb:0fd9:0063:stream-deck-mini-replacement"
        )
        await runtime.set_device(replacement)

        record = await concord._contract_record(contract)
        assert record is not None
        assert record.cancel_reason == "hardware device stream-deck-mini replaced"
        assert (await concord._validate(contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert reset_devices == ["stream-deck-mini"]
        assert runtime.live_claims == ()

        payload = _advertised_payload(deckr)
        advertised = payload.devices["stream-deck-mini"]
        assert advertised.descriptor == replacement
        assert advertised.capacity.claimed_instances == 0
        assert advertised.capacity.available_instances == 1
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_replace_devices_replacement_cancels_live_claim_and_resets() -> None:
    reset_devices: list[str] = []

    async def reset_handler(device_id: str) -> None:
        reset_devices.append(device_id)

    deckr, endpoint_cm, runtime = await _runtime(reset_handler=reset_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        replacement = _descriptor(
            fingerprint="usb:0fd9:0063:stream-deck-mini-replacement"
        )
        await runtime.replace_devices({"stream-deck-mini": replacement})

        record = await concord._contract_record(contract)
        assert record is not None
        assert record.cancel_reason == "hardware device stream-deck-mini replaced"
        assert (await concord._validate(contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert reset_devices == ["stream-deck-mini"]
        assert runtime.live_claims == ()

        payload = _advertised_payload(deckr)
        advertised = payload.devices["stream-deck-mini"]
        assert advertised.descriptor == replacement
        assert advertised.capacity.claimed_instances == 0
        assert advertised.capacity.available_instances == 1
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_set_device_same_fingerprint_does_not_refresh_or_reset_claim() -> None:
    reset_devices: list[str] = []

    async def reset_handler(device_id: str) -> None:
        reset_devices.append(device_id)

    deckr, endpoint_cm, runtime = await _runtime(reset_handler=reset_handler)
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert len(runtime.live_claims) == 1
        first = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)[0]

        await runtime.set_device(_descriptor())

        second = _beacon(deckr).candidates(HARDWARE_FEATURE_ID)[0]
        assert second.advertisement.refresh_seq == first.advertisement.refresh_seq
        assert second.revision == first.revision
        assert reset_devices == []
        assert [claim.terms.claim_id for claim in runtime.live_claims] == ["claim-a"]
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_cm.__aexit__(None, None, None)


async def test_remove_device_cancels_live_claim_contract_without_lane_event() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        deckr._message_bus.publish.reset_mock()
        deckr._message_bus.publish_reply.reset_mock()
        await runtime.remove_device("stream-deck-mini", reason="disconnected")
        assert not deckr._message_bus.publish.called
        assert not deckr._message_bus.publish_reply.called

        record = await concord._contract_record(contract)
        assert record is not None
        assert record.cancel_reason == "hardware device stream-deck-mini disconnected"
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
    controller_cm = deckr.endpoint(controller_address("controller-main"))
    controller_endpoint = await controller_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        contract = await _claim(runtime, concord)
        await concord._attach(
            contract,
            controller_endpoint.address,
            controller_endpoint.session_id,
        )
        await runtime._reconcile_claims(reason="test live")
        assert (
            await concord._validate(contract)
        ).status == ContractValidityStatus.VALID

        await runtime.replace_devices({}, removed_reason="removed")

        record = await concord._contract_record(contract)
        assert record is not None
        assert record.cancel_reason == "hardware device stream-deck-mini removed"
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
    controller_a_cm = deckr.endpoint(controller_address("controller-a"))
    controller_b_cm = deckr.endpoint(controller_address("controller-b"))
    controller_a = await controller_a_cm.__aenter__()
    controller_b = await controller_b_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
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
        await concord._attach(claim_b, controller_b.address, controller_b.session_id)
        await concord._attach(claim_a, controller_a.address, controller_a.session_id)

        await runtime._reconcile_claims(reason="test competing")
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


async def test_competing_claims_keep_existing_live_claim_before_lower_key() -> None:
    deckr, endpoint_cm, runtime = await _runtime()
    controller_a_cm = deckr.endpoint(controller_address("controller-a"))
    controller_b_cm = deckr.endpoint(controller_address("controller-b"))
    controller_a = await controller_a_cm.__aenter__()
    controller_b = await controller_b_cm.__aenter__()
    concord = _concord(deckr)
    try:
        await runtime._publish_advertisement()
        await _add_device(runtime, _descriptor())
        claim_b = await _claim(
            runtime,
            concord,
            contract_id="claim-b",
            controller_id="controller-b",
        )
        await concord._attach(claim_b, controller_b.address, controller_b.session_id)

        await runtime._reconcile_claims(reason="test first live claim")
        assert [claim.terms.claim_id for claim in runtime.live_claims] == ["claim-b"]
        assert (await concord._validate(claim_b)).status == (
            ContractValidityStatus.VALID
        )

        claim_a = await _claim(
            runtime,
            concord,
            contract_id="claim-a",
            controller_id="controller-a",
        )
        await concord._attach(claim_a, controller_a.address, controller_a.session_id)

        await runtime._reconcile_claims(reason="test existing claim priority")
        assert [claim.terms.claim_id for claim in runtime.live_claims] == ["claim-b"]
        assert (await concord._validate(claim_b)).status == (
            ContractValidityStatus.VALID
        )
        assert (await concord._validate(claim_a)).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )
    finally:
        await runtime.stop()
        await endpoint_cm.__aexit__(None, None, None)
        await controller_a_cm.__aexit__(None, None, None)
        await controller_b_cm.__aexit__(None, None, None)
