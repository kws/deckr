from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from deckr.actions.state import parse_action_provider_catalog_key
from deckr.state import (
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
)


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    revision: int
    value: dict[str, Any]


async def main() -> int:
    args = _parse_args()
    try:
        import nats
    except ModuleNotFoundError as exc:
        raise SystemExit("Install the deckr[nats] extra to use this script.") from exc

    nc = await nats.connect(args.url)
    try:
        js = nc.jetstream()
        try:
            kv = await js.key_value(args.bucket)
        except Exception as exc:
            raise SystemExit(
                f"Could not open NATS KV bucket {args.bucket!r}: {exc}"
            ) from exc
        rows = await _load_rows(kv)
    finally:
        await nc.close()

    _print_report(rows)
    return 0


async def _load_rows(kv) -> list[Row]:
    try:
        keys = await kv.keys()
    except Exception:
        keys = ()
    rows: list[Row] = []
    for key in sorted(str(item) for item in keys):
        try:
            entry = await kv.get(key)
        except Exception:
            continue
        try:
            value = json.loads(entry.value.decode("utf-8"))
        except Exception:
            value = {"_decodeError": True, "_rawBytes": len(entry.value)}
        rows.append(Row(key=key, revision=int(entry.revision), value=value))
    return rows


def _print_report(rows: list[Row]) -> None:
    presence: list[tuple[str, str, Row]] = []
    inventories: list[tuple[str, Row]] = []
    catalogs: list[tuple[str, Row]] = []
    claims: list[tuple[str, str, Row]] = []
    unknown: list[Row] = []

    for row in rows:
        parsed_presence = parse_presence_endpoint_key(row.key)
        if parsed_presence is not None:
            lane, endpoint = parsed_presence
            presence.append((lane, str(endpoint), row))
            continue
        manager_id = parse_hardware_inventory_key(row.key)
        if manager_id is not None:
            inventories.append((manager_id, row))
            continue
        provider_instance_id = parse_action_provider_catalog_key(row.key)
        if provider_instance_id is not None:
            catalogs.append((provider_instance_id, row))
            continue
        parsed_claim = parse_device_claim_key(row.key)
        if parsed_claim is not None:
            manager_id, device_id = parsed_claim
            claims.append((manager_id, device_id, row))
            continue
        unknown.append(row)

    print("Deckr NATS Current State")
    print("========================")
    print(f"keys: {len(rows)}")
    print()
    _print_presence(presence)
    _print_inventories(inventories)
    _print_catalogs(catalogs)
    _print_claims(claims)
    _print_unknown(unknown)


def _print_presence(rows: list[tuple[str, str, Row]]) -> None:
    print("Endpoint Presence")
    if not rows:
        print("  none")
        print()
        return
    for lane, endpoint, row in rows:
        print(
            "  "
            f"{endpoint} lane={lane} session={row.value.get('sessionId')} "
            f"ttl={row.value.get('ttlSeconds')}s rev={row.revision}"
        )
    print()


def _print_inventories(rows: list[tuple[str, Row]]) -> None:
    print("Hardware Inventory")
    if not rows:
        print("  none")
        print()
        return
    for manager_id, row in rows:
        devices = row.value.get("devices")
        count = len(devices) if isinstance(devices, dict) else 0
        print(
            "  "
            f"{manager_id} endpoint={row.value.get('managerEndpoint')} "
            f"session={row.value.get('sessionId')} devices={count} rev={row.revision}"
        )
    print()


def _print_catalogs(rows: list[tuple[str, Row]]) -> None:
    print("Action Provider Catalogs")
    if not rows:
        print("  none")
        print()
        return
    for provider_instance_id, row in rows:
        actions = _catalog_action_count(row.value)
        print(
            "  "
            f"{provider_instance_id} endpoint={row.value.get('providerEndpoint')} "
            f"provider={row.value.get('providerId')} "
            f"session={row.value.get('sessionId')} actions={actions} rev={row.revision}"
        )
    print()


def _catalog_action_count(value: dict[str, Any]) -> int:
    actions = value.get("actions")
    return len(actions) if isinstance(actions, dict) else 0


def _print_claims(rows: list[tuple[str, str, Row]]) -> None:
    print("Device Claims")
    if not rows:
        print("  none")
        print()
        return
    by_manager: dict[str, list[tuple[str, Row]]] = defaultdict(list)
    for manager_id, device_id, row in rows:
        by_manager[manager_id].append((device_id, row))
    for manager_id, entries in sorted(by_manager.items()):
        for device_id, row in sorted(entries):
            print(
                "  "
                f"{manager_id}/{device_id} claimedBy={row.value.get('claimedByEndpoint')} "
                f"session={row.value.get('claimedBySessionId')} rev={row.revision}"
            )
    print()


def _print_unknown(rows: list[Row]) -> None:
    if not rows:
        return
    print("Other Keys")
    for row in rows:
        print(f"  {row.key} rev={row.revision}")
    print()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="nats://127.0.0.1:4222")
    parser.add_argument("--bucket", default="deckr_state_v1")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
