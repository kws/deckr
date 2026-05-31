from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import uuid
from pathlib import Path

import anyio

from deckr.beacon import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    BeaconDiscovery,
    BeaconService,
    beacon_advertisement_key,
)
from deckr.concord import (
    CONCORD_CONTRACT_STORE_POLICY,
    CONCORD_TOKEN_STORE_POLICY,
    DEFAULT_CONCORD_CONTRACT_STORE_NAME,
    DEFAULT_CONCORD_TOKEN_STORE_NAME,
    ConcordAgreement,
    ConcordAgreementSpec,
    ConcordCoordinator,
    ConcordService,
    ContractValidityStatus,
    concord_participant_token_key,
)
from deckr.contracts.lanes import CORE_LANE_CONTRACTS, LaneContractRegistry
from deckr.contracts.messages import (
    EndpointAddress,
    controller_address,
    hardware_manager_address,
)
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    CapabilityRef,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    ProfileCapacity,
    hardware_payload_from_advertisement,
)
from deckr.runtime import Deckr
from deckr.state import StateStore
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.supervised_nats import NatsServerSupervisor


async def main() -> int:
    args = _parse_args()
    if args.role == "manager":
        await _run_manager(args)
        return 0
    if args.role == "controller":
        await _run_controller(args)
        return 0
    return await _run_orchestrator(args)


async def _run_orchestrator(args: argparse.Namespace) -> int:
    if args.supervised:
        supervisor = NatsServerSupervisor(
            server_path=args.server_path,
            runtime_dir=args.runtime_dir,
            store_dir=args.store_dir,
            startup_timeout=args.startup_timeout,
        )
        handle = await supervisor.start()
        try:
            args.url = handle.url
            args.auth_token = handle.auth_token
            args.supervised = False
            return await _run_orchestrator(args)
        finally:
            await supervisor.stop()

    run_id = args.run_id or uuid.uuid4().hex[:10]
    script = Path(__file__).resolve()
    common = [
        sys.executable,
        str(script),
        "--url",
        args.url,
        "--beacon-bucket",
        args.beacon_bucket,
        "--contract-bucket",
        args.contract_bucket,
        "--token-bucket",
        args.token_bucket,
        "--run-id",
        run_id,
    ]
    if args.auth_token is not None:
        common.extend(["--auth-token", args.auth_token])
    manager = await asyncio.create_subprocess_exec(
        *common,
        "--role",
        "manager",
    )
    await asyncio.sleep(0.5)
    controller = await asyncio.create_subprocess_exec(
        *common,
        "--role",
        "controller",
    )
    try:
        controller_status = await asyncio.wait_for(controller.wait(), timeout=20)
        manager_status = await asyncio.wait_for(manager.wait(), timeout=20)
    finally:
        if manager.returncode is None:
            manager.terminate()
            await manager.wait()
    if controller_status != 0 or manager_status != 0:
        return 1
    if args.check_ttl:
        await _wait_for_ttl_cleanup(args, run_id=run_id)
    print(f"NATS Beacon/Concord smoke passed run_id={run_id}")
    return 0


async def _run_manager(args: argparse.Namespace) -> None:
    manager_id = f"smoke_manager_{args.run_id}"
    device_id = f"deck_{args.run_id}"
    endpoint = hardware_manager_address(manager_id)
    descriptor = _device_descriptor(device_id, fingerprint=f"smoke:{args.run_id}")
    async with _deckr(args.url, auth_token=args.auth_token) as deckr:
        beacon_state, contract_state, token_state = _protocol_states(deckr, args)
        beacon = BeaconService(BeaconDiscovery(beacon_state))
        concord = ConcordService(ConcordCoordinator(contract_state, token_state))
        async with (
            deckr.lane("hardware_messages").register_endpoint(
                endpoint,
                metadata={"runtime": "deckr-nats-smoke"},
            ) as lane,
            lane.subscribe() as messages,
            anyio.create_task_group() as tg,
        ):
            payload = _hardware_payload(
                manager_id=manager_id,
                endpoint=endpoint,
                session_id=lane.session_id,
                device_id=device_id,
                descriptor=descriptor,
            )
            advertisement = await beacon.advertise(
                HARDWARE_FEATURE_ID,
                endpoint,
                lane.session_id,
                advertisement_id=manager_id,
                payload=payload.to_dict(),
            )

            def accept_contract(contract, _record) -> bool:
                return endpoint in contract.participants

            claim_manager = concord.participant_manager(
                participant=endpoint,
                session_id=lane.session_id,
                profile=HARDWARE_CLAIM_PROFILE_ID,
                log_label="NatsSmokeManager",
                accept_contract=accept_contract,
            )
            claim_manager.start(tg)
            try:
                with anyio.fail_after(15):
                    request = await messages.receive()
                if request.recipient.endpoint != endpoint:
                    raise RuntimeError("manager received a message for the wrong endpoint")
                device_ref = DeviceRef(managerId=manager_id, deviceId=device_id)
                await lane.reply_to(
                    request,
                    message_type=hw_messages.COMMAND_REPLY,
                    body=hw_messages.hardware_body_to_dict(
                        hw_messages.CommandReplyMessage(
                            deviceRef=device_ref,
                            controlId="0,0",
                            capabilityId="raster.bitmap",
                            commandType="set_frame",
                            result={"ok": True, "runId": args.run_id},
                        )
                    ),
                )
            finally:
                await beacon.withdraw(advertisement)
                tg.cancel_scope.cancel()


async def _run_controller(args: argparse.Namespace) -> None:
    manager_id = f"smoke_manager_{args.run_id}"
    device_id = f"deck_{args.run_id}"
    controller = controller_address(f"smoke_controller_{args.run_id}")
    manager = hardware_manager_address(manager_id)
    async with _deckr(args.url, auth_token=args.auth_token) as deckr:
        beacon_state, contract_state, token_state = _protocol_states(deckr, args)
        beacon = BeaconService(BeaconDiscovery(beacon_state))
        concord = ConcordService(ConcordCoordinator(contract_state, token_state))
        async with deckr.lane("hardware_messages").register_endpoint(controller) as lane:
            candidate = await _wait_for_hardware_advertisement(beacon, manager)
            payload = hardware_payload_from_advertisement(candidate.advertisement)
            if device_id not in payload.devices:
                raise RuntimeError("hardware advertisement did not include smoke device")
            device_ref = DeviceRef(
                managerId=manager_id,
                deviceId=device_id,
                fingerprint=payload.devices[device_id].descriptor.fingerprint,
            )
            terms = HardwareClaimTerms(
                claimId=f"smoke-claim-{args.run_id}",
                controllerEndpoint=controller,
                managerEndpoint=manager,
                devices=(HardwareClaimDevice(deviceRef=device_ref, instanceCount=1),),
            )
            agreement = await concord.ensure_agreement(
                ConcordAgreementSpec(
                    profile=HARDWARE_CLAIM_PROFILE_ID,
                    participants=(controller, manager),
                    local_participant=controller,
                    local_session_id=lane.session_id,
                    terms=terms,
                    current_sessions={
                        str(controller): lane.session_id,
                        str(manager): payload.session_id,
                    },
                    log_label="NatsSmokeController",
                )
            )
            await _wait_for_valid_contract(agreement)
            async with lane.subscribe() as controller_messages:
                capability_ref = CapabilityRef(
                    deviceRef=device_ref,
                    controlId="0,0",
                    capabilityId="raster.bitmap",
                )
                reply = await lane.request(
                    recipient=manager,
                    subject=hw_messages.hardware_subject_for_capability(
                        capability_ref
                    ),
                    message_type=hw_messages.CONTROL_COMMAND,
                    body=hw_messages.hardware_body_to_dict(
                        hw_messages.ControlCommandMessage(
                            deviceRef=device_ref,
                            controlId="0,0",
                            capabilityId="raster.bitmap",
                            commandType="set_frame",
                            params={
                                "image": base64.b64encode(b"smoke").decode("ascii"),
                                "encoding": "jpeg",
                            },
                        )
                    ),
                    timeout=10,
                )
                with anyio.move_on_after(0.25) as scope:
                    await controller_messages.receive()
            if reply.in_reply_to is None or reply.sender != manager:
                raise RuntimeError("request/reply correlation failed")
            if not scope.cancel_called:
                raise RuntimeError("controller received a message not addressed to it")


async def _wait_for_hardware_advertisement(
    beacon: BeaconService,
    manager: EndpointAddress,
):
    with anyio.fail_after(15):
        while True:
            candidates = await beacon.find(
                HARDWARE_FEATURE_ID,
                selector=lambda advertisement: advertisement.endpoint == manager,
            )
            if candidates:
                return candidates[0]
            await anyio.sleep(0.1)


async def _wait_for_valid_contract(
    agreement: ConcordAgreement,
) -> None:
    with anyio.fail_after(15):
        while True:
            validity = await agreement.refresh()
            if validity.status == ContractValidityStatus.VALID:
                return
            await anyio.sleep(0.1)


async def _wait_for_ttl_cleanup(args: argparse.Namespace, *, run_id: str) -> None:
    manager_id = f"smoke_manager_{run_id}"
    controller = controller_address(f"smoke_controller_{run_id}")
    manager = hardware_manager_address(manager_id)
    advertisement_key = beacon_advertisement_key(
        feature_id=HARDWARE_FEATURE_ID,
        advertisement_id=manager_id,
    )
    controller_token_key = concord_participant_token_key(
        contract_id=f"smoke-hardware-{run_id}",
        generation=1,
        participant=controller,
    )
    manager_token_key = concord_participant_token_key(
        contract_id=f"smoke-hardware-{run_id}",
        generation=1,
        participant=manager,
    )
    async with _deckr(args.url, auth_token=args.auth_token) as deckr:
        beacon_state, _contract_state, token_state = _protocol_states(deckr, args)
        with anyio.fail_after(args.ttl_wait):
            while True:
                entries = [
                    await beacon_state.get(advertisement_key),
                    await token_state.get(controller_token_key),
                    await token_state.get(manager_token_key),
                ]
                if all(entry is None for entry in entries):
                    return
                await anyio.sleep(0.5)


def _protocol_states(
    deckr: Deckr,
    args: argparse.Namespace,
) -> tuple[StateStore, StateStore, StateStore]:
    return (
        deckr.state(args.beacon_bucket, policy=BEACON_ADVERTISEMENT_STORE_POLICY),
        deckr.state(args.contract_bucket, policy=CONCORD_CONTRACT_STORE_POLICY),
        deckr.state(args.token_bucket, policy=CONCORD_TOKEN_STORE_POLICY),
    )


def _deckr(
    url: str,
    *,
    auth_token: str | None,
) -> Deckr:
    registry = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
    return Deckr(
        lane_contracts=registry,
        substrate=NatsSubstrate(
            url=url,
            auth_token=auth_token,
            lane_contracts=registry,
        ),
    )


def _hardware_payload(
    *,
    manager_id: str,
    endpoint: EndpointAddress,
    session_id: str,
    device_id: str,
    descriptor: DeviceDescriptor,
) -> HardwareBeaconPayload:
    return HardwareBeaconPayload(
        managerId=manager_id,
        managerEndpoint=endpoint,
        sessionId=session_id,
        devices={
            device_id: HardwareAdvertisementDevice(
                capacity=ProfileCapacity(
                    totalInstances=1,
                    claimedInstances=0,
                    availableInstances=1,
                ),
                deviceRef=DeviceRef(
                    managerId=manager_id,
                    deviceId=device_id,
                    fingerprint=descriptor.fingerprint,
                ),
                descriptor=descriptor,
            )
        },
    )


def _device_descriptor(device_id: str, *, fingerprint: str) -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Smoke Deck",
        fingerprint=fingerprint,
        controls=(
            ControlDescriptor(
                controlId="0,0",
                kind="key",
                geometry=ControlGeometry(x=0, y=0, width=1, height=1, unit="grid"),
                inputCapabilities=(
                    CapabilityDescriptor(
                        capabilityId="button.momentary",
                        family=DECKR_INPUT_BUTTON,
                        type="momentary",
                        direction="input",
                        access=("emits",),
                        eventTypes=("down", "up"),
                    ),
                ),
                outputCapabilities=(
                    CapabilityDescriptor.model_validate(
                        {
                            "capabilityId": "raster.bitmap",
                            "family": DECKR_OUTPUT_RASTER,
                            "type": "bitmap",
                            "direction": "output",
                            "access": ["settable"],
                            "commandTypes": ["set_frame", "clear"],
                            "constraints": [
                                {"type": "fixed", "subject": "width", "value": 72},
                                {"type": "fixed", "subject": "height", "value": 72},
                            ],
                        }
                    ),
                ),
            ),
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="nats://127.0.0.1:4222")
    parser.add_argument("--auth-token")
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="Start a local supervised nats-server for the smoke run.",
    )
    parser.add_argument(
        "--server-path",
        type=Path,
        help="Absolute nats-server path to use for --supervised.",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Runtime directory to use for --supervised.",
    )
    parser.add_argument(
        "--store-dir",
        type=Path,
        help="JetStream store directory to use for --supervised.",
    )
    parser.add_argument("--startup-timeout", type=float, default=10.0)
    parser.add_argument(
        "--beacon-bucket",
        default=f"{DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME}_smoke",
    )
    parser.add_argument(
        "--contract-bucket",
        default=f"{DEFAULT_CONCORD_CONTRACT_STORE_NAME}_smoke",
    )
    parser.add_argument(
        "--token-bucket",
        default=f"{DEFAULT_CONCORD_TOKEN_STORE_NAME}_smoke",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--role", choices=("manager", "controller"))
    parser.add_argument(
        "--check-ttl",
        action="store_true",
        help="Wait for Beacon advertisements and Concord tokens to expire or withdraw.",
    )
    parser.add_argument("--ttl-wait", type=float, default=45.0)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
