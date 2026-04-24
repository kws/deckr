"""Shared rendering types for Deckr plugin/controller APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TitleOptions:
    """Font and styling options for controller-rendered titles."""

    font_family: str | None = None
    font_size: int | str | None = None
    font_style: str | None = None
    title_color: str | None = None
    title_alignment: str | None = None


def title_options_to_wire(title_options: TitleOptions | None) -> dict[str, Any] | None:
    """Serialize title options for protocol payloads."""
    if title_options is None:
        return None
    return {
        "fontFamily": title_options.font_family,
        "fontSize": title_options.font_size,
        "fontStyle": title_options.font_style,
        "titleColor": title_options.title_color,
        "titleAlignment": title_options.title_alignment,
    }


def title_options_from_wire(payload: dict[str, Any] | None) -> TitleOptions | None:
    """Parse title options from protocol payloads."""
    if not isinstance(payload, dict):
        return None
    values = {
        "font_family": payload.get("fontFamily", payload.get("font_family")),
        "font_size": payload.get("fontSize", payload.get("font_size")),
        "font_style": payload.get("fontStyle", payload.get("font_style")),
        "title_color": payload.get("titleColor", payload.get("title_color")),
        "title_alignment": payload.get(
            "titleAlignment",
            payload.get("title_alignment"),
        ),
    }
    if all(value is None for value in values.values()):
        return None
    return TitleOptions(**values)
