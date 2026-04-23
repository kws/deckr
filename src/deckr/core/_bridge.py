from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any

BRIDGE_METADATA_KEY = "x-bridge-instance-ids"


class BridgeEnvelopeError(ValueError):
    """Raised when a bridge transport message envelope is invalid."""


def build_bridge_envelope(
    bridge_id: str,
    lane: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    return {"_g": bridge_id, "lane": lane, "m": message}


def parse_bridge_envelope(payload: Any) -> tuple[str | None, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise BridgeEnvelopeError("Bridge payload must be a JSON object")

    bridge_id = payload.get("_g")
    if bridge_id is not None and not isinstance(bridge_id, str):
        raise BridgeEnvelopeError("Bridge payload '_g' must be a string")

    lane = payload.get("lane")
    if not isinstance(lane, str) or not lane.strip():
        raise BridgeEnvelopeError("Bridge payload 'lane' must be a non-empty string")

    message = payload.get("m")
    if not isinstance(message, dict):
        raise BridgeEnvelopeError("Bridge payload 'm' must be an object")

    return bridge_id, lane, message


def _bridge_ids_from_metadata(metadata: Any) -> tuple[str, ...]:
    if not isinstance(metadata, dict):
        return ()

    raw_ids = metadata.get(BRIDGE_METADATA_KEY, ())
    if isinstance(raw_ids, str):
        return (raw_ids,)
    if isinstance(raw_ids, (list, tuple, set, frozenset)):
        return tuple(value for value in raw_ids if isinstance(value, str))
    return ()


def event_originated_from_bridge(event: Any, bridge_id: str) -> bool:
    return bridge_id in _bridge_ids_from_metadata(
        getattr(event, "internal_metadata", None)
    )


def event_has_bridge_metadata(event: Any) -> bool:
    return bool(_bridge_ids_from_metadata(getattr(event, "internal_metadata", None)))


def mark_event_from_bridge(event: Any, bridge_id: str) -> Any:
    marker = getattr(event, "internal_metadata", None)
    if not hasattr(event, "internal_metadata") or marker is not None and not isinstance(
        marker, dict
    ):
        return event

    ids = set(_bridge_ids_from_metadata(marker))
    if bridge_id in ids:
        return event

    metadata = dict(marker or {})
    ids.add(bridge_id)
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
