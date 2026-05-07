"""Action provider current-state key helpers."""

from __future__ import annotations

from deckr.state import decode_key_token, encode_key_token


def action_provider_catalog_key(provider_instance_id: str) -> str:
    return ".".join(
        (
            "catalog",
            "actions",
            "providers",
            encode_key_token(provider_instance_id),
        )
    )


def parse_action_provider_catalog_key(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 4 or parts[:3] != ["catalog", "actions", "providers"]:
        return None
    return decode_key_token(parts[3])
