"""Thin constructors for common canonical plugin capability requirements."""

from __future__ import annotations

from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
)
from deckr.pluginhost.messages import (
    CapabilityRequirement,
    CapabilityRequirementSelector,
)


def button_input_requirement(
    name: str = "button",
    *,
    event_types: tuple[str, ...] = ("press", "up"),
) -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name,
        preferences=(
            CapabilityRequirementSelector(
                family=DECKR_INPUT_BUTTON,
                direction="input",
                eventTypes=event_types,
            ),
        ),
        eventTypes=event_types,
        views=("native",),
    )


def encoder_input_requirement(
    name: str = "encoder",
) -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name,
        preferences=(
            CapabilityRequirementSelector(
                family=DECKR_INPUT_ENCODER,
                direction="input",
                eventTypes=("rotate",),
            ),
        ),
        eventTypes=("rotate",),
        views=("native",),
    )


def touch_input_requirement(
    name: str = "touch",
    *,
    event_types: tuple[str, ...] = ("tap", "swipe"),
) -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name,
        required=False,
        preferences=(
            CapabilityRequirementSelector(
                family=DECKR_INPUT_TOUCH,
                direction="input",
                eventTypes=event_types,
            ),
        ),
        eventTypes=event_types,
        views=("native",),
    )


def raster_output_requirement(name: str = "raster") -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name,
        preferences=(
            CapabilityRequirementSelector(
                family=DECKR_OUTPUT_RASTER,
                type="bitmap",
                direction="output",
                commandTypes=("set_frame", "clear"),
            ),
        ),
        commandTypes=("set_frame", "clear"),
        views=("native",),
    )


def button_raster_requirements() -> tuple[CapabilityRequirement, ...]:
    return (button_input_requirement(), raster_output_requirement())
