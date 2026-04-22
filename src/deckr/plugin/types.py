"""Types for plugin key image graphs and dynamic pages."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from deckr.plugin.rendering import TitleOptions


@dataclass(frozen=True)
class SlotBinding:
    """One slot bound to an action (static or itemised dynamic page)."""

    slot_id: str
    action_uuid: str
    settings: dict
    title_options: TitleOptions | None = None


@dataclass(frozen=True)
class DynamicPageDescriptor:
    """Plugin-generated page with slots. Dynamic pages replace the current page."""

    page_id: str
    slots: list[SlotBinding]  # required


def make_dynamic_page_id() -> str:
    """Generate a unique page ID for dynamic pages."""
    return str(uuid.uuid4())
