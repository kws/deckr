from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from deckr.beacon import (
    DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    parse_beacon_advertisement_key,
)
from deckr.concord import (
    DEFAULT_CONCORD_CONTRACT_STORE_NAME,
    DEFAULT_CONCORD_TOKEN_STORE_NAME,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)


@dataclass(frozen=True, slots=True)
class Row:
    bucket: str
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
        rows = []
        for bucket in _buckets(args):
            try:
                kv = await js.key_value(bucket)
            except Exception as exc:
                raise SystemExit(
                    f"Could not open NATS KV bucket {bucket!r}: {exc}"
                ) from exc
            rows.extend(await _load_rows(kv, bucket=bucket))
    finally:
        await nc.close()

    _print_report(rows)
    return 0


async def _load_rows(kv, *, bucket: str) -> list[Row]:
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
        rows.append(
            Row(bucket=bucket, key=key, revision=int(entry.revision), value=value)
        )
    return rows


def _print_report(rows: list[Row]) -> None:
    advertisements: list[tuple[str, str, Row]] = []
    contracts: list[tuple[str, int, Row]] = []
    tokens: list[tuple[str, int, str, Row]] = []
    unknown: list[Row] = []

    for row in rows:
        parsed_advertisement = parse_beacon_advertisement_key(row.key)
        if parsed_advertisement is not None:
            feature_id, advertisement_id = parsed_advertisement
            advertisements.append((feature_id, advertisement_id, row))
            continue
        parsed_contract = parse_concord_contract_key(row.key)
        if parsed_contract is not None:
            contract_id, generation = parsed_contract
            contracts.append((contract_id, generation, row))
            continue
        parsed_token = parse_concord_participant_token_key(row.key)
        if parsed_token is not None:
            contract_id, generation, participant = parsed_token
            tokens.append((contract_id, generation, str(participant), row))
            continue
        unknown.append(row)

    print("Deckr NATS Beacon/Concord State")
    print("================================")
    print(f"buckets: {', '.join(sorted({row.bucket for row in rows})) or 'none'}")
    print(f"keys: {len(rows)}")
    print()
    _print_advertisements(advertisements)
    _print_contracts(contracts)
    _print_tokens(tokens)
    _print_unknown(unknown)


def _print_advertisements(rows: list[tuple[str, str, Row]]) -> None:
    print("Beacon Advertisements")
    if not rows:
        print("  none")
        print()
        return
    for feature_id, advertisement_id, row in sorted(rows):
        print(
            "  "
            f"{feature_id}/{advertisement_id} endpoint={row.value.get('endpoint')} "
            f"session={row.value.get('sessionId')} refresh={row.value.get('refreshSeq')} "
            f"ttl={row.value.get('ttlSeconds')}s rev={row.revision} "
            f"bucket={row.bucket}"
        )
    print()


def _print_contracts(rows: list[tuple[str, int, Row]]) -> None:
    print("Concord Contracts")
    if not rows:
        print("  none")
        print()
        return
    for contract_id, generation, row in sorted(rows):
        participants = row.value.get("participants")
        count = len(participants) if isinstance(participants, list) else 0
        print(
            "  "
            f"{contract_id} gen={generation} state={row.value.get('state')} "
            f"profile={row.value.get('profile')} participants={count} "
            f"rev={row.revision} bucket={row.bucket}"
        )
    print()


def _print_tokens(rows: list[tuple[str, int, str, Row]]) -> None:
    print("Concord Participant Tokens")
    if not rows:
        print("  none")
        print()
        return
    for contract_id, generation, participant, row in sorted(rows):
        print(
            "  "
            f"{contract_id} gen={generation} participant={participant} "
            f"session={row.value.get('sessionId')} refresh={row.value.get('refreshSeq')} "
            f"ttl={row.value.get('ttlSeconds')}s rev={row.revision} "
            f"bucket={row.bucket}"
        )
    print()


def _print_unknown(rows: list[Row]) -> None:
    if not rows:
        return
    print("Other Keys")
    for row in rows:
        print(f"  {row.key} rev={row.revision} bucket={row.bucket}")
    print()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="nats://127.0.0.1:4222")
    parser.add_argument(
        "--bucket",
        action="append",
        default=None,
        help="NATS KV bucket to inspect. May be repeated.",
    )
    return parser.parse_args()


def _buckets(args: argparse.Namespace) -> tuple[str, ...]:
    if args.bucket is not None:
        return tuple(args.bucket)
    return (
        DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
        DEFAULT_CONCORD_CONTRACT_STORE_NAME,
        DEFAULT_CONCORD_TOKEN_STORE_NAME,
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
