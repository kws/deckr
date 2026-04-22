from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any

BRIDGE_METADATA_KEY = "x-bridge-instance-ids"


class BridgeEnvelopeError(ValueError):
    """Raised when a bridge transport message envelope is invalid."""


def build_bridge_envelope(gateway_id: str, message: dict[str, Any]) -> dict[str, Any]:
    return {"_g": gateway_id, "m": message}


def parse_bridge_envelope(payload: Any) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise BridgeEnvelopeError("Bridge payload must be a JSON object")

    gateway_id = payload.get("_g")
    if gateway_id is not None and not isinstance(gateway_id, str):
        raise BridgeEnvelopeError("Bridge payload '_g' must be a string")

    message = payload.get("m")
    if not isinstance(message, dict):
        raise BridgeEnvelopeError("Bridge payload 'm' must be an object")

    return gateway_id, message


def event_originated_from_bridge(event: Any, gateway_id: str) -> bool:
    metadata = getattr(event, "internal_metadata", None)
    if not isinstance(metadata, dict):
        return False

    raw_ids = metadata.get(BRIDGE_METADATA_KEY, ())
    if isinstance(raw_ids, str):
        return raw_ids == gateway_id
    if not isinstance(raw_ids, (list, tuple, set, frozenset)):
        return False

    return gateway_id in raw_ids


def mark_event_from_bridge(event: Any, gateway_id: str) -> Any:
    marker = getattr(event, "internal_metadata", None)
    if not hasattr(event, "internal_metadata") or marker is not None and not isinstance(
        marker, dict
    ):
        return event

    metadata = dict(marker or {})
    bridge_ids = metadata.get(BRIDGE_METADATA_KEY, ())

    if isinstance(bridge_ids, str):
        ids = {bridge_ids}
    elif isinstance(bridge_ids, (list, tuple, set, frozenset)):
        ids = {value for value in bridge_ids if isinstance(value, str)}
    else:
        ids = set()

    if gateway_id in ids:
        return event

    ids.add(gateway_id)
    metadata[BRIDGE_METADATA_KEY] = tuple(sorted(ids))

    try:
        event.internal_metadata = metadata
        return event
    except Exception:
        pass

    if is_dataclass(event):
        try:
            return replace(event, internal_metadata=metadata)
        except Exception:
            return event

    return event
