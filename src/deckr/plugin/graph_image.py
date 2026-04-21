"""Helpers for carrying serialized graph images through `setImage`."""

from __future__ import annotations

import base64
import json
from typing import Any

GRAPH_IMAGE_MEDIA_TYPE = "application/vnd.deckr.graph+json"
GRAPH_IMAGE_DATA_URI_PREFIX = f"data:{GRAPH_IMAGE_MEDIA_TYPE};base64,"


def wire_to_graph_image_data_uri(wire: dict[str, Any]) -> str:
    """Encode a graph wire payload as a data URI suitable for `setImage`."""
    payload = json.dumps(wire, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"{GRAPH_IMAGE_DATA_URI_PREFIX}{encoded}"


def graph_image_data_uri_to_wire(image: str) -> dict[str, Any] | None:
    """Decode a Deckr graph-image data URI back into a wire payload."""
    if not image.startswith(GRAPH_IMAGE_DATA_URI_PREFIX):
        return None
    encoded = image[len(GRAPH_IMAGE_DATA_URI_PREFIX) :]
    try:
        payload = base64.b64decode(encoded, validate=True)
        decoded = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None
