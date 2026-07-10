from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json


def concord_contract_key(*, contract_id: str, generation: int) -> str:
    return ".".join(
        ("contracts", encode_key_token(contract_id), str(generation), "meta")
    )


def parse_concord_contract_key(key: str) -> tuple[str, int] | None:
    parts = key.split(".")
    if len(parts) != 4 or parts[0] != "contracts" or parts[3] != "meta":
        return None
    try:
        generation = int(parts[2])
        contract_id = decode_key_token(parts[1])
    except (TypeError, ValueError):
        return None
    if generation < 1:
        return None
    return contract_id, generation


def concord_participant_token_key(
    *,
    contract_id: str,
    generation: int,
    participant: str | EndpointAddress,
) -> str:
    parsed = parse_endpoint_address(participant)
    return ".".join(
        (
            "contracts",
            encode_key_token(contract_id),
            str(generation),
            "participants",
            encode_key_token(str(parsed)),
        )
    )


def parse_concord_participant_token_key(
    key: str,
) -> tuple[str, int, EndpointAddress] | None:
    parts = key.split(".")
    if len(parts) != 5 or parts[0] != "contracts" or parts[3] != "participants":
        return None
    try:
        generation = int(parts[2])
        contract_id = decode_key_token(parts[1])
        participant = parse_endpoint_address(decode_key_token(parts[4]))
    except (TypeError, ValueError):
        return None
    if generation < 1:
        return None
    return contract_id, generation, participant


def concord_contract_prefix(*, contract_id: str, generation: int) -> str:
    return ".".join(("contracts", encode_key_token(contract_id), str(generation), ""))


def concord_contracts_prefix() -> str:
    return "contracts."


def concord_stale_observation_key(*, contract_id: str, generation: int) -> str:
    return ".".join(("stale", encode_key_token(contract_id), str(generation)))


def parse_concord_stale_observation_key(key: str) -> tuple[str, int] | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != "stale":
        return None
    try:
        contract_id = decode_key_token(parts[1])
        generation = int(parts[2])
    except (TypeError, ValueError):
        return None
    if generation < 1:
        return None
    return contract_id, generation


def canonical_json_bytes(value: Mapping[str, Any] | DeckrModel) -> bytes:
    if isinstance(value, DeckrModel):
        payload = value.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        payload = thaw_json(freeze_json(value))
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_hash(value: Mapping[str, Any] | DeckrModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
