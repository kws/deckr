"""Plugin manifest models.

Core Elgato-aligned types:
- `State`
- `Action`
- `CoreManifest`

Deckr extensions:
- `TitleOptions`
- `StateOverride`
- `Manifest.deckr_requires`

Use `Manifest.model_validate_json(json_str)` to parse Elgato-style JSON
(PascalCase). Use `manifest.model_dump_json(by_alias=True)` to emit
Elgato-compatible JSON.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field

from deckr.core.util.pydantic import CamelModel, to_pascal

_PASCAL_CONFIG = ConfigDict(alias_generator=to_pascal, populate_by_name=True)


@dataclass(frozen=True)
class TitleOptions:
    """Font and styling options for title rendering. Aligned with Elgato manifest State."""

    font_family: str | None = None  # e.g. "Inter", "Roboto Mono"
    font_size: int | str | None = None  # pixels
    font_style: str | None = None  # "Regular", "Bold", "Italic", "Bold Italic"
    title_color: str | None = None  # hex e.g. "#FFFFFF"
    title_alignment: str | None = None  # "top", "middle", "bottom"


@dataclass
class StateOverride:
    """Per-state render override: title or image reference."""

    title: str | None = None
    image: str | None = None  # URL, image data URI, or Deckr graph-image data URI
    title_options: TitleOptions | None = None


class State(CamelModel):
    """Per-state defaults. Aligned with Elgato manifest State."""

    model_config = _PASCAL_CONFIG

    font_family: str | None = None
    font_size: int | str | None = None  # int, "85vw", "1rem", etc.
    font_style: Literal["", "Bold Italic", "Bold", "Italic", "Regular"] | None = None
    font_underline: bool | None = None
    image: str | None = None  # Optional for title-only actions
    name: str | None = None
    show_title: bool | None = None
    title: str | None = None
    title_alignment: Literal["bottom", "middle", "top"] | None = None
    title_color: str | None = None


class Action(CamelModel):
    """Action metadata. Aligned with Elgato manifest Action."""

    model_config = _PASCAL_CONFIG

    name: str
    uuid: str = Field(
        validation_alias=AliasChoices("UUID", "uuid")
    )  # Elgato "UUID", Python "uuid"
    icon: str | None = None
    tooltip: str | None = None
    controllers: list[Literal["Keypad", "Encoder"]]
    states: list[State]


class CoreManifest(CamelModel):
    """Elgato-aligned runtime subset of a plugin manifest."""

    model_config = _PASCAL_CONFIG
    actions: list[Action] = Field(alias="Actions")


class Manifest(CoreManifest):
    """Deckr manifest = core manifest plus Deckr-specific extension fields."""

    deckr_requires: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("DeckrRequires", "deckr_requires"),
    )


def manifest_state_to_override(state: State) -> StateOverride:
    """Convert manifest State to StateOverride for render resolution."""
    has_font_opts = any(
        [
            state.font_family,
            state.font_size is not None,
            state.font_style,
            state.title_color,
            state.title_alignment,
        ]
    )
    title_options = (
        TitleOptions(
            font_family=state.font_family,
            font_size=state.font_size,
            font_style=state.font_style,
            title_color=state.title_color,
            title_alignment=state.title_alignment,
        )
        if has_font_opts
        else None
    )
    return StateOverride(
        title=state.title or None,
        image=state.image if state.image else None,
        title_options=title_options,
    )


def get_manifest_defaults_for_action(
    manifest: Manifest, action_uuid: str
) -> dict[int, StateOverride] | None:
    """Find action by UUID and map states to StateOverride dict."""

    for action in manifest.actions:
        if action.uuid == action_uuid:
            result: dict[int, StateOverride] = {}
            for i, state in enumerate(action.states):
                result[i] = manifest_state_to_override(state)
            return result if result else None
    return None


def _state_override_to_wire(override: StateOverride) -> dict[str, Any]:
    """Convert StateOverride to wire format (dict) for manifest_defaults.
    Compatible with _parse_manifest_defaults in context.py."""
    d: dict[str, Any] = {"title": override.title, "image": override.image}
    if override.title_options:
        d["title_options"] = dataclasses.asdict(override.title_options)
    return d


def serialize_manifest_defaults(
    manifest_defaults: dict[int, StateOverride] | None,
) -> dict[str, dict] | None:
    """Serialize manifest_state_defaults for wire/protocol use.
    Produces dict[str, dict] compatible with _parse_manifest_defaults."""
    if not manifest_defaults:
        return None
    return {
        str(k): (
            v.model_dump() if hasattr(v, "model_dump") else _state_override_to_wire(v)
        )
        for k, v in manifest_defaults.items()
    }


def build_action_metadata(action: Any) -> dict[str, Any]:
    """Build action metadata dict (uuid, manifestDefaults) for wire protocol."""
    manifest_defaults = getattr(action, "manifest_state_defaults", None)
    return {
        "uuid": action.uuid,
        "manifestDefaults": serialize_manifest_defaults(manifest_defaults),
    }
