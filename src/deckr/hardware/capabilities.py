"""Canonical Deckr core hardware capability value and command contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from deckr.contracts.models import DeckrModel, thaw_json
from deckr.hardware.descriptors import CapabilitySchema

ButtonActivationEvent = Literal["press"]
ButtonMomentaryEvent = Literal["down", "up"]
EncoderRelativeDirection = Literal["clockwise", "counterclockwise"]
RasterBitmapEncoding = Literal["jpeg", "png"]
RasterBitmapCommandType = Literal["set_frame", "clear"]
TouchGestureDirection = Literal["left", "right"]
TouchGestureEvent = Literal["tap", "swipe"]

BUTTON_ACTIVATION_VALUE_SCHEMA_ID = "deckr.value.input.button.activation.v1"
BUTTON_MOMENTARY_VALUE_SCHEMA_ID = "deckr.value.input.button.momentary.v1"
DEVICE_POWER_COMMAND_SCHEMA_ID = "deckr.command.device.power.screen.v1"
ENCODER_RELATIVE_DIRECTIONS: tuple[EncoderRelativeDirection, ...] = (
    "clockwise",
    "counterclockwise",
)
ENCODER_RELATIVE_VALUE_SCHEMA_ID = "deckr.value.input.encoder.relative.v1"
RASTER_BITMAP_COMMAND_SCHEMA_ID = "deckr.command.output.raster.bitmap.v1"
TOUCH_GESTURE_DIRECTIONS: tuple[TouchGestureDirection, ...] = ("left", "right")
TOUCH_GESTURE_VALUE_SCHEMA_ID = "deckr.value.input.touch.gesture.v1"


class ButtonActivationInputValue(DeckrModel):
    """Value payload for a ``deckr.input.button`` activation event."""

    event_type: ButtonActivationEvent = Field(alias="eventType")


class ButtonMomentaryInputValue(DeckrModel):
    """Value payload for a ``deckr.input.button`` momentary event."""

    event_type: ButtonMomentaryEvent = Field(alias="eventType")


class EncoderRelativeInputValue(DeckrModel):
    """Value payload for a ``deckr.input.encoder`` relative rotate event."""

    delta: int
    direction: EncoderRelativeDirection | None = None

    @field_validator("delta")
    @classmethod
    def _validate_delta(cls, value: int) -> int:
        if value == 0:
            raise ValueError("encoder relative delta must not be zero")
        return value

    @model_validator(mode="after")
    def _validate_direction_matches_delta(self) -> EncoderRelativeInputValue:
        if self.direction == "clockwise" and self.delta < 0:
            raise ValueError("clockwise encoder rotation must have positive delta")
        if self.direction == "counterclockwise" and self.delta > 0:
            raise ValueError(
                "counterclockwise encoder rotation must have negative delta"
            )
        return self


class TouchGestureInputValue(DeckrModel):
    """Value payload for a ``deckr.input.touch`` gesture event."""

    event_type: TouchGestureEvent = Field(alias="eventType")
    direction: TouchGestureDirection | None = None

    @model_validator(mode="after")
    def _validate_direction(self) -> TouchGestureInputValue:
        if self.event_type == "tap" and self.direction is not None:
            raise ValueError("tap gesture must not include direction")
        if self.event_type == "swipe" and self.direction is None:
            raise ValueError("swipe gesture must include direction")
        return self


class RasterBitmapSetFrameParams(DeckrModel):
    """Command params for ``deckr.output.raster`` bitmap ``set_frame``."""

    image: str
    encoding: RasterBitmapEncoding
    width: int | None = None
    height: int | None = None

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str) -> str:
        if not value:
            raise ValueError("raster bitmap image must not be empty")
        return value

    @field_validator("width", "height")
    @classmethod
    def _validate_positive_dimensions(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("raster bitmap dimensions must be positive")
        return value


class RasterBitmapClearParams(DeckrModel):
    """Command params for ``deckr.output.raster`` bitmap ``clear``."""


class DevicePowerCommandParams(DeckrModel):
    """Command params for ``deckr.device.power`` screen commands."""


def button_activation_input_value(value: object) -> ButtonActivationInputValue:
    """Parse the value for a button activation input event."""

    return ButtonActivationInputValue.model_validate(thaw_json(value))


def button_momentary_input_value(value: object) -> ButtonMomentaryInputValue:
    """Parse the value for a button momentary input event."""

    return ButtonMomentaryInputValue.model_validate(thaw_json(value))


def encoder_relative_input_value(value: object) -> EncoderRelativeInputValue:
    """Parse the value for a relative encoder input event."""

    return EncoderRelativeInputValue.model_validate(thaw_json(value))


def touch_gesture_input_value(value: object) -> TouchGestureInputValue:
    """Parse the value for a touch gesture input event."""

    return TouchGestureInputValue.model_validate(thaw_json(value))


def raster_bitmap_command_params(
    command_type: RasterBitmapCommandType,
    params: object,
) -> RasterBitmapSetFrameParams | RasterBitmapClearParams:
    """Parse params for a raster bitmap command."""

    if command_type == "set_frame":
        return RasterBitmapSetFrameParams.model_validate(thaw_json(params))
    if command_type == "clear":
        return RasterBitmapClearParams.model_validate(thaw_json(params))
    raise ValueError(f"unsupported raster bitmap command: {command_type}")


def device_power_command_params(params: object) -> DevicePowerCommandParams:
    """Parse params for a device power command."""

    return DevicePowerCommandParams.model_validate(thaw_json(params))


def _button_value_schema(
    *,
    schema_id: str,
    events: tuple[str, ...],
) -> CapabilitySchema:
    event_schema: dict[str, Any] = (
        {"const": events[0]} if len(events) == 1 else {"enum": list(events)}
    )
    return CapabilitySchema.model_validate(
        {
            "schemaId": schema_id,
            "schema": {
                "type": "object",
                "required": ["eventType"],
                "properties": {"eventType": event_schema},
                "additionalProperties": False,
            },
        }
    )


def button_activation_value_schema() -> CapabilitySchema:
    """Return the canonical schema for ``deckr.input.button`` activation input."""

    return _button_value_schema(
        schema_id=BUTTON_ACTIVATION_VALUE_SCHEMA_ID,
        events=("press",),
    )


def button_momentary_value_schema() -> CapabilitySchema:
    """Return the canonical schema for ``deckr.input.button`` momentary input."""

    return _button_value_schema(
        schema_id=BUTTON_MOMENTARY_VALUE_SCHEMA_ID,
        events=("down", "up"),
    )


def encoder_relative_value_schema() -> CapabilitySchema:
    """Return the canonical schema for ``deckr.input.encoder`` relative input."""

    return CapabilitySchema.model_validate(
        {
            "schemaId": ENCODER_RELATIVE_VALUE_SCHEMA_ID,
            "schema": {
                "type": "object",
                "required": ["delta"],
                "properties": {
                    "delta": {
                        "type": "integer",
                        "not": {"const": 0},
                    },
                    "direction": {"enum": list(ENCODER_RELATIVE_DIRECTIONS)},
                },
                "additionalProperties": False,
            },
        }
    )


def touch_gesture_value_schema() -> CapabilitySchema:
    """Return the canonical schema for ``deckr.input.touch`` gesture input."""

    return CapabilitySchema.model_validate(
        {
            "schemaId": TOUCH_GESTURE_VALUE_SCHEMA_ID,
            "schema": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["eventType"],
                        "properties": {"eventType": {"const": "tap"}},
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "required": ["eventType", "direction"],
                        "properties": {
                            "eventType": {"const": "swipe"},
                            "direction": {
                                "enum": list(TOUCH_GESTURE_DIRECTIONS),
                            },
                        },
                        "additionalProperties": False,
                    },
                ],
            },
        }
    )


def raster_bitmap_command_schema(
    *,
    width: int | None = None,
    height: int | None = None,
) -> CapabilitySchema:
    """Return the canonical command params schema for ``deckr.output.raster``."""

    set_frame_properties: dict[str, Any] = {
        "image": {"type": "string", "contentEncoding": "base64"},
        "encoding": {"enum": ["jpeg", "png"]},
    }
    if width is not None:
        set_frame_properties["width"] = {"const": width}
    if height is not None:
        set_frame_properties["height"] = {"const": height}
    return CapabilitySchema.model_validate(
        {
            "schemaId": RASTER_BITMAP_COMMAND_SCHEMA_ID,
            "schema": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["image", "encoding"],
                        "properties": set_frame_properties,
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "maxProperties": 0,
                    },
                ],
            },
        }
    )


def device_power_command_schema() -> CapabilitySchema:
    """Return the canonical command params schema for ``deckr.device.power``."""

    return CapabilitySchema.model_validate(
        {
            "schemaId": DEVICE_POWER_COMMAND_SCHEMA_ID,
            "schema": {
                "type": "object",
                "maxProperties": 0,
            },
        }
    )


__all__ = [
    "BUTTON_ACTIVATION_VALUE_SCHEMA_ID",
    "BUTTON_MOMENTARY_VALUE_SCHEMA_ID",
    "DEVICE_POWER_COMMAND_SCHEMA_ID",
    "ENCODER_RELATIVE_DIRECTIONS",
    "ENCODER_RELATIVE_VALUE_SCHEMA_ID",
    "RASTER_BITMAP_COMMAND_SCHEMA_ID",
    "TOUCH_GESTURE_DIRECTIONS",
    "TOUCH_GESTURE_VALUE_SCHEMA_ID",
    "ButtonActivationEvent",
    "ButtonActivationInputValue",
    "ButtonMomentaryEvent",
    "ButtonMomentaryInputValue",
    "DevicePowerCommandParams",
    "EncoderRelativeDirection",
    "EncoderRelativeInputValue",
    "RasterBitmapClearParams",
    "RasterBitmapCommandType",
    "RasterBitmapEncoding",
    "RasterBitmapSetFrameParams",
    "TouchGestureDirection",
    "TouchGestureEvent",
    "TouchGestureInputValue",
    "button_activation_input_value",
    "button_activation_value_schema",
    "button_momentary_input_value",
    "button_momentary_value_schema",
    "device_power_command_params",
    "device_power_command_schema",
    "encoder_relative_input_value",
    "encoder_relative_value_schema",
    "raster_bitmap_command_params",
    "raster_bitmap_command_schema",
    "touch_gesture_input_value",
    "touch_gesture_value_schema",
]
