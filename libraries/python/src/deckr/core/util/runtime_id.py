from __future__ import annotations

import re

_INVALID_RUNTIME_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


def normalize_runtime_id(value: str) -> str:
    value = value.strip()
    value = value.replace("::", "-")
    value = _INVALID_RUNTIME_ID_CHARS.sub("-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def require_runtime_id(
    value: str | None,
    *,
    label: str,
    source_hint: str,
) -> str:
    if value:
        normalized = normalize_runtime_id(value)
        if normalized:
            return normalized

    raise ValueError(f"{label} is required. {source_hint}")
