from __future__ import annotations

import os
import re
import socket
import uuid

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
    fallback_to_hostname: bool,
    fallback_to_uuid: bool,
    label: str,
) -> str:
    candidates: list[str | None] = [cli_value, os.getenv(env_var)]
    if fallback_to_hostname:
        candidates.append(socket.gethostname())

    for candidate in candidates:
        if not candidate:
            continue
        normalized = _normalize_runtime_id(candidate)
        if normalized:
            return normalized

    if fallback_to_uuid:
        return _normalize_runtime_id(str(uuid.uuid4()))

    raise ValueError(
        f"{label} is required. Provide via CLI or environment variable {env_var}."
    )


def resolve_host_id(
    *,
    cli_value: str | None = None,
    env_var: str = "HOST_ID",
    fallback_to_hostname: bool = True,
    fallback_to_uuid: bool = False,
) -> str:
    """Resolve a stable host ID for action qualification and message routing.

    When CLI and env are unset, defaults to the system hostname (normalized).
    """
    return _resolve_runtime_id(
        cli_value=cli_value,
        env_var=env_var,
        fallback_to_hostname=fallback_to_hostname,
        fallback_to_uuid=fallback_to_uuid,
        label="Host ID",
    )


def resolve_controller_id(
    *,
    cli_value: str | None = None,
    env_var: str = "CONTROLLER_ID",
    fallback_to_hostname: bool = False,
    fallback_to_uuid: bool = True,
) -> str:
    """Resolve a stable controller ID for message routing and context ownership.

    When CLI and env are unset, defaults to a new random UUID each process start.
    For a stable deployment ID across restarts, set CONTROLLER_ID or pass --controller-id.
    """
    return _resolve_runtime_id(
        cli_value=cli_value,
        env_var=env_var,
        fallback_to_hostname=fallback_to_hostname,
        fallback_to_uuid=fallback_to_uuid,
        label="Controller ID",
    )
