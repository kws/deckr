"""Action provider endpoint helpers."""

from __future__ import annotations

from deckr.contracts.messages import (
    _RESERVED_ACTION_PROVIDER_INSTANCE_IDS,
    BroadcastTarget,
    EndpointAddress,
    _require_provider_instance_id,
    broadcast_target,
    endpoint_address,
    parse_endpoint_address,
)

BUILTIN_ACTION_PROVIDER_ID = "deckr.controller.builtin"
RESERVED_BUILTIN_PROVIDER_IDS = _RESERVED_ACTION_PROVIDER_INSTANCE_IDS


def require_provider_instance_id(value: str, *, field_name: str) -> str:
    return _require_provider_instance_id(value, field_name=field_name)


def action_provider_address(provider_instance_id: str) -> EndpointAddress:
    return endpoint_address("action_provider", provider_instance_id)


def parse_action_provider_address(address: str | EndpointAddress) -> str | None:
    try:
        parsed = parse_endpoint_address(address)
    except ValueError:
        return None
    if parsed.family != "action_provider":
        return None
    return parsed.endpoint_id


def action_providers_broadcast(
    *,
    domain: str | None = None,
    hop_limit: int | None = None,
) -> BroadcastTarget:
    return broadcast_target(
        scope="action_providers",
        endpoint_family="action_provider",
        domain=domain,
        hop_limit=hop_limit,
    )
