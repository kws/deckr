from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from deckr.hardware.capabilities import (
    BUTTON_ACTIVATION_VALUE_SCHEMA_ID,
    BUTTON_MOMENTARY_VALUE_SCHEMA_ID,
    DEVICE_POWER_COMMAND_SCHEMA_ID,
    RASTER_BITMAP_COMMAND_SCHEMA_ID,
    TOUCH_GESTURE_VALUE_SCHEMA_ID,
    button_activation_input_value,
    button_activation_value_schema,
    button_momentary_input_value,
    button_momentary_value_schema,
    device_power_command_params,
    device_power_command_schema,
    encoder_relative_input_value,
    raster_bitmap_command_params,
    raster_bitmap_command_schema,
    touch_gesture_input_value,
    touch_gesture_value_schema,
)


def _json_schema(schema):
    return schema.model_dump(by_alias=True, mode="json")["schema"]


def test_button_values_and_schemas_are_canonical_deckr_contracts() -> None:
    activation = button_activation_input_value(MappingProxyType({"eventType": "press"}))
    momentary = button_momentary_input_value(MappingProxyType({"eventType": "up"}))

    assert activation.event_type == "press"
    assert momentary.event_type == "up"

    with pytest.raises(ValidationError):
        button_activation_input_value({"eventType": "up"})

    activation_schema = button_activation_value_schema()
    momentary_schema = button_momentary_value_schema()

    assert activation_schema.schema_id == BUTTON_ACTIVATION_VALUE_SCHEMA_ID
    assert _json_schema(activation_schema)["properties"]["eventType"] == {
        "const": "press"
    }
    assert momentary_schema.schema_id == BUTTON_MOMENTARY_VALUE_SCHEMA_ID
    assert _json_schema(momentary_schema)["properties"]["eventType"] == {
        "enum": ["down", "up"]
    }


def test_encoder_relative_value_accepts_signed_delta_and_direction() -> None:
    clockwise = encoder_relative_input_value(
        {"delta": 1, "direction": "clockwise"}
    )
    counterclockwise = encoder_relative_input_value(
        {"delta": -1, "direction": "counterclockwise"}
    )

    assert clockwise.delta == 1
    assert clockwise.direction == "clockwise"
    assert counterclockwise.delta == -1
    assert counterclockwise.direction == "counterclockwise"


def test_encoder_relative_value_rejects_zero_and_mismatched_direction() -> None:
    with pytest.raises(ValidationError, match="delta must not be zero"):
        encoder_relative_input_value({"delta": 0})

    with pytest.raises(ValidationError, match="must have negative delta"):
        encoder_relative_input_value(
            {"delta": 1, "direction": "counterclockwise"}
        )


def test_touch_gesture_values_and_schema_are_canonical_deckr_contracts() -> None:
    tap = touch_gesture_input_value(MappingProxyType({"eventType": "tap"}))
    swipe = touch_gesture_input_value(
        MappingProxyType({"eventType": "swipe", "direction": "left"})
    )

    assert tap.event_type == "tap"
    assert tap.direction is None
    assert swipe.event_type == "swipe"
    assert swipe.direction == "left"

    with pytest.raises(ValidationError, match="swipe gesture must include direction"):
        touch_gesture_input_value({"eventType": "swipe"})

    schema = touch_gesture_value_schema()
    json_schema = _json_schema(schema)

    assert schema.schema_id == TOUCH_GESTURE_VALUE_SCHEMA_ID
    assert json_schema["oneOf"][0]["properties"]["eventType"] == {"const": "tap"}
    assert json_schema["oneOf"][1]["required"] == ["eventType", "direction"]


def test_raster_bitmap_command_params_and_schema_are_canonical_deckr_contracts() -> None:
    set_frame = raster_bitmap_command_params(
        "set_frame",
        MappingProxyType({"image": "ZnJhbWU=", "encoding": "jpeg"}),
    )
    clear = raster_bitmap_command_params("clear", MappingProxyType({}))

    assert set_frame.image == "ZnJhbWU="
    assert set_frame.encoding == "jpeg"
    assert clear.model_dump() == {}

    with pytest.raises(ValidationError):
        raster_bitmap_command_params("clear", {"image": "ZnJhbWU=", "encoding": "jpeg"})
    with pytest.raises(ValueError, match="unsupported raster bitmap command"):
        raster_bitmap_command_params("flash", {})

    schema = raster_bitmap_command_schema(width=72, height=72)
    json_schema = _json_schema(schema)

    assert schema.schema_id == RASTER_BITMAP_COMMAND_SCHEMA_ID
    assert json_schema["oneOf"][0]["required"] == ["image", "encoding"]
    assert json_schema["oneOf"][0]["properties"]["width"] == {"const": 72}
    assert json_schema["oneOf"][1] == {"type": "object", "maxProperties": 0}


def test_device_power_command_params_and_schema_are_canonical_deckr_contracts() -> None:
    params = device_power_command_params(MappingProxyType({}))
    schema = device_power_command_schema()

    assert params.model_dump() == {}
    assert schema.schema_id == DEVICE_POWER_COMMAND_SCHEMA_ID
    assert _json_schema(schema) == {"type": "object", "maxProperties": 0}

    with pytest.raises(ValidationError):
        device_power_command_params({"reason": "please"})
