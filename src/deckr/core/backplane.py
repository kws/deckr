from __future__ import annotations

from dataclasses import dataclass, field

from deckr.core.messaging import EventBus


@dataclass(slots=True)
class DeckrBackplane:
    hardware_events: EventBus = field(default_factory=EventBus)
    plugin_messages: EventBus = field(default_factory=EventBus)

    def topic(self, name: str) -> EventBus:
        if name == "hardware_events":
            return self.hardware_events
        if name == "plugin_messages":
            return self.plugin_messages
        raise KeyError(f"Unknown backplane topic: {name}")
