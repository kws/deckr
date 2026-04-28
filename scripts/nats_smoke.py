from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anyio

from deckr.contracts.lanes import CORE_LANE_CONTRACTS, LaneContractRegistry
from deckr.contracts.messages import (
    controller_address,
    entity_subject,
    hardware_manager_address,
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
    print(f"NATS smoke passed run_id={run_id}")
    return 0


async def _run_manager(args: argparse.Namespace) -> None:
    manager_id = f"smoke_manager_{args.run_id}"
    device_id = f"deck_{args.run_id}"
    endpoint = hardware_manager_address(manager_id)
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
                    ttlSeconds=30,
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
                    ttlSeconds=30,
                    devices={
                        device_id: HardwareInventoryDevice(
                            deviceId=device_id,
                            hardwareType="smoke_deck",
                            fingerprint=f"smoke:{args.run_id}",
                        )
                    },
                ),
            )
            with anyio.fail_after(15):
                request = await messages.receive()
            if request.recipient.endpoint != endpoint:
                raise RuntimeError("manager received a message for the wrong endpoint")
            await lane.reply_to(
                request,
                message_type="wakeScreen",
                body={"ok": True, "runId": args.run_id},
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
                    ttlSeconds=30,
                ),
            )
            async with lane.subscribe() as controller_messages:
                reply = await lane.request(
                    recipient=manager,
                    subject=entity_subject(
                        "hardwareControl",
                        managerId=manager_id,
                        deviceId=device_id,
                    ),
                    message_type="setImage",
                    body={"slot": 0, "image": "smoke"},
                    timeout=10,
                )
                with anyio.move_on_after(0.25) as scope:
                    await controller_messages.receive()
            if reply.in_reply_to is None or reply.sender != manager:
                raise RuntimeError("request/reply correlation failed")
            if not scope.cancel_called:
                raise RuntimeError("controller received a message not addressed to it")


def _deckr(url: str) -> Deckr:
    registry = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
    return Deckr(
        lane_contracts=registry,
        substrate=NatsSubstrate(url=url, lane_contracts=registry),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="nats://127.0.0.1:4222")
    parser.add_argument("--bucket", default="deckr_state_v1_smoke")
    parser.add_argument("--run-id")
    parser.add_argument("--role", choices=("manager", "controller"))
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
