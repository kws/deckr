from __future__ import annotations

import os
import re

_INVALID_RUNTIME_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


def _normalize_runtime_id(value: str) -> str:
    value = value.strip()
    value = value.replace("::", "-")
    value = _INVALID_RUNTIME_ID_CHARS.sub("-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def _resolve_runtime_id(
    *,
    cli_value: str | None,
    env_var: str,
    label: str,
) -> str:
    candidates: list[str | None] = [cli_value, os.getenv(env_var)]

    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize_runtime_id(candidate)
        if normalized:
            return normalized

    raise ValueError(
        f"{label} is required. Provide via CLI or environment variable {env_var}."
    )


def resolve_host_id(
    *,
    cli_value: str | None = None,
    env_var: str = "HOST_ID",
) -> str:
    """Resolve a stable host ID for action qualification and message routing.

    Requires an explicit value from CLI/config or environment.
    """
    return _resolve_runtime_id(
        cli_value=cli_value,
        env_var=env_var,
        label="Host ID",
    )


def resolve_controller_id(
    *,
    cli_value: str | None = None,
    env_var: str = "CONTROLLER_ID",
) -> str:
    """Resolve a stable controller ID for message routing and context ownership.

    Requires an explicit value from CLI/config or environment.
    """
    return _resolve_runtime_id(
        cli_value=cli_value,
        env_var=env_var,
        label="Controller ID",
    )
