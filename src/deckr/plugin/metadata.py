"""Helpers for action registration metadata."""

from __future__ import annotations

from typing import Any


def build_action_metadata(action: Any) -> dict[str, Any]:
    """Build the action metadata payload used during host registration."""
    return {
        "uuid": action.uuid,
        "name": getattr(action, "name", None),
        "pluginUuid": getattr(action, "plugin_uuid", None),
    }
