from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from descriptor_fixtures import descriptor_payloads
from pydantic import ValidationError

from deckr.hardware.descriptors import (
    CAPABILITY_DESCRIPTOR_SCHEMA_ID,
    CONTROL_ACTIVATION_EVENTS,
    CONTROL_DESCRIPTOR_SCHEMA_ID,
    DECKR_DEVICE_POWER,
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
    DEVICE_DESCRIPTOR_SCHEMA_ID,
    CapabilityDescriptor,
    CapabilitySchema,
    ControlGeometry,
    DeviceDescriptor,
    descriptor_schema_artifacts,
)


def test_control_activation_events_cover_activation_equivalent_inputs() -> None:
    assert CONTROL_ACTIVATION_EVENTS == ("up", "press", "tap")


@pytest.mark.parametrize("fixture_name,payload", descriptor_payloads().items())
def test_representative_descriptor_fixtures_validate(
    fixture_name: str,
    payload: dict[str, object],
) -> None:
    descriptor = DeviceDescriptor.model_validate(payload)
    wire = descriptor.model_dump(by_alias=True, exclude_none=True, mode="json")
    encoded = json.dumps(wire)

    assert descriptor.device_id == payload["deviceId"]
    assert descriptor.controls
    assert "slot" not in encoded.lower(), fixture_name
    assert "hid" not in wire
    assert "connections" in wire


def test_stream_deck_grid_has_explicit_default_status_output() -> None:
    descriptor = DeviceDescriptor.model_validate(
        descriptor_payloads()["stream_deck_bitmap_grid"]
    )

    assert descriptor.default_status_indicator is not None
    default_control = next(
        control
        for control in descriptor.controls
        if control.control_id == descriptor.default_status_indicator.control_id
    )
    default_output = next(
        capability
        for capability in default_control.output_capabilities
        if capability.capability_id
        == descriptor.default_status_indicator.capability_id
    )

    assert default_output.family == DECKR_OUTPUT_RASTER
    assert default_output.direction == "output"


def test_momentary_button_projection_is_manager_authored_and_explicit() -> None:
    descriptor = DeviceDescriptor.model_validate(
        descriptor_payloads()["momentary_button_with_press_projection"]
    )
    control = descriptor.controls[0]
    press = next(
        capability
        for capability in control.input_capabilities
        if capability.capability_type == "activation"
    )

    assert press.event_types == ("press",)
    assert press.projection is not None
    assert press.projection.owner == "hardware_manager"
    assert press.projection.source.control_id == "button.1"
    assert press.projection.source.capability_id == "button.momentary"


def test_press_only_button_does_not_claim_momentary_lifecycle() -> None:
    descriptor = DeviceDescriptor.model_validate(
        descriptor_payloads()["press_only_button_control"]
    )
    capabilities = descriptor.controls[0].input_capabilities

    assert [capability.capability_type for capability in capabilities] == ["activation"]
    assert capabilities[0].event_types == ("press",)


def test_extension_capability_family_is_first_class_with_standard_projection() -> None:
    descriptor = DeviceDescriptor.model_validate(
        descriptor_payloads()["extension_capability_with_standard_projection"]
    )
    control = descriptor.controls[0]
    native = control.input_capabilities[0]
    projected = control.input_capabilities[1]

    assert native.family == "com.example.input.smart_button"
    assert projected.family == DECKR_INPUT_BUTTON
    assert projected.projection is not None
    assert projected.projection.source.capability_id == native.capability_id


def test_descriptor_rejects_duplicate_control_ids() -> None:
    payload = copy.deepcopy(descriptor_payloads()["plain_button_device"])
    payload["controls"].append(copy.deepcopy(payload["controls"][0]))

    with pytest.raises(ValidationError, match="control ids must be unique"):
        DeviceDescriptor.model_validate(payload)


def test_descriptor_rejects_duplicate_capability_ids_within_control() -> None:
    payload = copy.deepcopy(descriptor_payloads()["plain_button_device"])
    control = payload["controls"][0]
    control["inputCapabilities"].append(copy.deepcopy(control["inputCapabilities"][0]))

    with pytest.raises(ValidationError, match="capability ids on control"):
        DeviceDescriptor.model_validate(payload)


def test_default_status_indicator_must_reference_real_output_capability() -> None:
    payload = copy.deepcopy(descriptor_payloads()["stream_deck_bitmap_grid"])
    payload["defaultStatusIndicator"] = {
        "controlId": "key.0.0",
        "capabilityId": "button.press",
    }

    with pytest.raises(ValidationError, match="must reference an output capability"):
        DeviceDescriptor.model_validate(payload)


def test_control_relations_must_reference_existing_controls() -> None:
    payload = copy.deepcopy(descriptor_payloads()["touch_surface_with_tap_and_swipe"])
    payload["controls"][0]["parentControlId"] = "missing"

    with pytest.raises(ValidationError, match="unknown parent"):
        DeviceDescriptor.model_validate(payload)


def test_projection_sources_must_reference_real_capabilities() -> None:
    payload = copy.deepcopy(descriptor_payloads()["momentary_button_with_press_projection"])
    projected = payload["controls"][0]["inputCapabilities"][1]
    projected["projection"]["source"]["capabilityId"] = "missing"

    with pytest.raises(ValidationError, match="projects from unknown capability"):
        DeviceDescriptor.model_validate(payload)


def test_descriptor_local_capability_refs_reject_foreign_device_refs() -> None:
    payload = copy.deepcopy(descriptor_payloads()["stream_deck_bitmap_grid"])
    payload["defaultStatusIndicator"] = {
        "deviceRef": {"managerId": "manager-main", "deviceId": "stream-deck-mini"},
        "controlId": "key.0.0",
        "capabilityId": "raster.bitmap",
    }

    with pytest.raises(ValidationError, match="deviceRef"):
        DeviceDescriptor.model_validate(payload)


def test_projection_source_refs_are_descriptor_local() -> None:
    payload = copy.deepcopy(descriptor_payloads()["momentary_button_with_press_projection"])
    projected = payload["controls"][0]["inputCapabilities"][1]
    projected["projection"]["source"]["deviceRef"] = {
        "managerId": "manager-main",
        "deviceId": "momentary-button",
    }

    with pytest.raises(ValidationError, match="deviceRef"):
        DeviceDescriptor.model_validate(payload)


def test_source_references_must_reference_existing_connections() -> None:
    payload = copy.deepcopy(descriptor_payloads()["mqtt_zigbee_button"])
    payload["sources"][0]["connectionId"] = "missing"

    with pytest.raises(ValidationError, match="references unknown connection"):
        DeviceDescriptor.model_validate(payload)


def test_capability_rejects_unsupported_deckr_owned_family() -> None:
    payload = {
        "capabilityId": "indicator",
        "family": "deckr.output.indicator",
        "type": "led",
        "direction": "output",
        "access": ["settable"],
    }

    with pytest.raises(ValidationError, match="unsupported Deckr core capability family"):
        CapabilityDescriptor.model_validate(payload)


def test_capability_rejects_unqualified_extension_family() -> None:
    payload = {
        "capabilityId": "smart.button",
        "family": "smart_button",
        "type": "action",
        "direction": "input",
        "access": ["emits"],
    }

    with pytest.raises(ValidationError, match="globally namespaced"):
        CapabilityDescriptor.model_validate(payload)


def test_capability_direction_must_match_access_and_schema_fields() -> None:
    payload = {
        "capabilityId": "button.press",
        "family": DECKR_INPUT_BUTTON,
        "type": "activation",
        "direction": "input",
        "access": ["readable"],
        "eventTypes": ["press"],
    }

    with pytest.raises(ValidationError, match="input capability access"):
        CapabilityDescriptor.model_validate(payload)


def test_core_button_types_have_distinct_event_semantics() -> None:
    payload = {
        "capabilityId": "button.press",
        "family": DECKR_INPUT_BUTTON,
        "type": "activation",
        "direction": "input",
        "access": ["emits"],
        "eventTypes": ["down", "up"],
    }

    with pytest.raises(ValidationError, match="activation capabilities emit press only"):
        CapabilityDescriptor.model_validate(payload)


def test_core_touch_raster_and_power_capabilities_require_full_known_type_lists() -> None:
    touch_payload = {
        "capabilityId": "touch.gesture",
        "family": DECKR_INPUT_TOUCH,
        "type": "gesture",
        "direction": "input",
        "access": ["emits"],
        "eventTypes": ["tap"],
    }
    with pytest.raises(ValidationError, match="emit tap and swipe"):
        CapabilityDescriptor.model_validate(touch_payload)

    raster_payload = {
        "capabilityId": "raster.bitmap",
        "family": DECKR_OUTPUT_RASTER,
        "type": "bitmap",
        "direction": "output",
        "access": ["settable"],
    }
    with pytest.raises(ValidationError, match="support set_frame and clear"):
        CapabilityDescriptor.model_validate(raster_payload)

    power_payload = {
        "capabilityId": "device.power",
        "family": DECKR_DEVICE_POWER,
        "type": "screen",
        "direction": "command",
        "access": ["invokable"],
        "commandTypes": ["sleep"],
    }
    with pytest.raises(ValidationError, match="support sleep and wake"):
        CapabilityDescriptor.model_validate(power_payload)


def test_geometry_and_embedded_schemas_reject_non_wire_safe_values() -> None:
    with pytest.raises(ValidationError, match="geometry value must be finite"):
        ControlGeometry.model_validate({"x": float("inf"), "y": 0})

    with pytest.raises(ValidationError, match="NaN or Infinity"):
        CapabilitySchema.model_validate(
            {
                "schemaId": "com.example.number.v1",
                "schema": {"type": "number", "maximum": float("nan")},
            }
        )

    with pytest.raises(ValidationError, match="unsupported JSON value type"):
        CapabilitySchema.model_validate(
            {
                "schemaId": "com.example.object.v1",
                "schema": {"type": object()},
            }
        )


def test_capability_schema_requires_contract_keywords() -> None:
    with pytest.raises(ValidationError, match="contract keyword"):
        CapabilitySchema.model_validate(
            {
                "schemaId": "com.example.empty.v1",
                "schema": {"description": "not enough"},
            }
        )


def test_descriptor_schema_artifacts_match_checked_in_files() -> None:
    schema_dir = Path(__file__).resolve().parents[1] / "schemas" / "hardware"
    expected_files = {
        DEVICE_DESCRIPTOR_SCHEMA_ID: "device-descriptor.v1.schema.json",
        CONTROL_DESCRIPTOR_SCHEMA_ID: "control-descriptor.v1.schema.json",
        CAPABILITY_DESCRIPTOR_SCHEMA_ID: "capability-descriptor.v1.schema.json",
    }
    artifacts = descriptor_schema_artifacts()

    assert set(artifacts) == set(expected_files)
    for schema_id, filename in expected_files.items():
        checked_in = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
        assert checked_in == artifacts[schema_id]


def test_device_descriptor_schema_shape_is_interoperable() -> None:
    artifacts = descriptor_schema_artifacts()
    schema = artifacts[DEVICE_DESCRIPTOR_SCHEMA_ID]

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == DEVICE_DESCRIPTOR_SCHEMA_ID
    assert schema["additionalProperties"] is False
    assert {"deviceId", "fingerprint", "displayName", "controls"}.issubset(
        schema["properties"]
    )
    assert "slots" not in schema["properties"]
    assert "hid" not in schema["properties"]

    capability_schema = artifacts[CAPABILITY_DESCRIPTOR_SCHEMA_ID]
    family_schema = capability_schema["properties"]["family"]
    assert family_schema["anyOf"][0]["enum"] == [
        "deckr.device.power",
        "deckr.input.button",
        "deckr.input.encoder",
        "deckr.input.touch",
        "deckr.output.raster",
    ]
    assert family_schema["anyOf"][1]["pattern"].startswith("^(?!deckr")


def test_projection_owner_schema_uses_v1_component_vocabulary() -> None:
    artifacts = descriptor_schema_artifacts()
    capability_schema = artifacts[CAPABILITY_DESCRIPTOR_SCHEMA_ID]
    projection_schema = capability_schema["$defs"]["CapabilityProjection"]

    assert projection_schema["properties"]["owner"]["enum"] == [
        "hardware_manager",
        "adapter",
        "component",
    ]
