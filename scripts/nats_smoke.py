from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anyio

from deckr.contracts.lanes import CORE_LANE_CONTRACTS, LaneContractRegistry
from deckr.contracts.messages import (
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
from deckr.runtime import Deckr
from deckr.state import (
    DeviceClaim,
    EndpointPresence,
    HardwareInventory,
    HardwareInventoryDevice,
    device_claim_key,
    hardware_inventory_key,
    presence_endpoint_key,
)
from deckr.substrates.nats import NatsSubstrate


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
    run_id = args.run_id or uuid.uuid4().hex[:10]
    script = Path(__file__).resolve()
    common = [
        sys.executable,
        str(script),
        "--url",
        args.url,
        "--bucket",
        args.bucket,
        "--run-id",
        run_id,
    ]
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
    print(f"NATS smoke passed run_id={run_id}")
    return 0


async def _run_manager(args: argparse.Namespace) -> None:
    manager_id = f"smoke_manager_{args.run_id}"
    device_id = f"deck_{args.run_id}"
    endpoint = hardware_manager_address(manager_id)
    descriptor = _device_descriptor(device_id, fingerprint=f"smoke:{args.run_id}")
    async with _deckr(args.url) as deckr:
        state = deckr.state(args.bucket)
        lane = deckr.lane("hardware_messages").endpoint(endpoint)
        async with lane.subscribe() as messages:
            await state.put(
                presence_endpoint_key(lane="hardware_messages", endpoint=endpoint),
                EndpointPresence(
                    endpoint=endpoint,
                    lane="hardware_messages",
                    sessionId=args.run_id,
                    timestamp=datetime.now(UTC),
                    ttlSeconds=15,
                    metadata={"runtime": "deckr-nats-smoke"},
                ),
            )
            await state.put(
                hardware_inventory_key(manager_id),
                HardwareInventory(
                    managerId=manager_id,
                    managerEndpoint=endpoint,
                    sessionId=args.run_id,
                    timestamp=datetime.now(UTC),
                    ttlSeconds=15,
                    devices={
                        device_id: HardwareInventoryDevice(
                            deviceRef=DeviceRef(
                                managerId=manager_id,
                                deviceId=device_id,
                                fingerprint=descriptor.fingerprint,
                            ),
                            descriptor=descriptor,
                        )
                    },
                ),
            )
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


async def _run_controller(args: argparse.Namespace) -> None:
    manager_id = f"smoke_manager_{args.run_id}"
    device_id = f"deck_{args.run_id}"
    controller = controller_address(f"smoke_controller_{args.run_id}")
    manager = hardware_manager_address(manager_id)
    async with _deckr(args.url) as deckr:
        state = deckr.state(args.bucket)
        lane = deckr.lane("hardware_messages").endpoint(controller)
        async with state.watch(hardware_inventory_key(manager_id)) as changes:
            with anyio.fail_after(15):
                change = await changes.receive()
            if change.entry is None:
                raise RuntimeError("inventory watch did not yield current state")
            await state.create(
                device_claim_key(manager_id=manager_id, device_id=device_id),
                DeviceClaim(
                    claimedByEndpoint=controller,
                    claimedBySessionId=args.run_id,
                    timestamp=datetime.now(UTC),
                    ttlSeconds=15,
                ),
            )
            async with lane.subscribe() as controller_messages:
                device_ref = DeviceRef(managerId=manager_id, deviceId=device_id)
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
                                "commandType": "set_frame",
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


async def _wait_for_ttl_cleanup(args: argparse.Namespace, *, run_id: str) -> None:
    manager_id = f"smoke_manager_{run_id}"
    device_id = f"deck_{run_id}"
    manager = hardware_manager_address(manager_id)
    keys = (
        presence_endpoint_key(lane="hardware_messages", endpoint=manager),
        hardware_inventory_key(manager_id),
        device_claim_key(manager_id=manager_id, device_id=device_id),
    )
    async with _deckr(args.url) as deckr:
        state = deckr.state(args.bucket)
        with anyio.fail_after(args.ttl_wait):
            while True:
                entries = [await state.get(key) for key in keys]
                if all(entry is None for entry in entries):
                    return
                await anyio.sleep(0.5)


def _deckr(url: str) -> Deckr:
    registry = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
    return Deckr(
        lane_contracts=registry,
        substrate=NatsSubstrate(url=url, lane_contracts=registry),
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
    parser.add_argument("--bucket", default="deckr_state_v1_smoke")
    parser.add_argument("--run-id")
    parser.add_argument("--role", choices=("manager", "controller"))
    parser.add_argument(
        "--check-ttl",
        action="store_true",
        help="Wait for smoke presence, inventory, and claim keys to expire.",
    )
    parser.add_argument("--ttl-wait", type=float, default=25.0)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
