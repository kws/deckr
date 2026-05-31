from __future__ import annotations

import importlib
import sys

import pytest
from pydantic import ValidationError

from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import CapabilityRef, DeviceRef


def test_legacy_hardware_events_module_is_not_importable():
    sys.modules.pop("deckr.hardware.events", None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("deckr.hardware.events")


def test_control_input_targets_exact_capability():
    message = hw_messages.control_input_message(
        manager_id="manager-main",
        sender_session_id="manager-session",
        device_id="deck",
        fingerprint="fingerprint:deck",
        control_id="key.0.0",
        capability_id="button.press",
        event_type="press",
        value={"eventType": "press"},
        sequence=1,
    )
    wire = message.to_dict()

    assert wire["messageType"] == "controlInput"
    assert wire["subject"]["kind"] == "hardware_capability"
    assert wire["subject"]["identifiers"] == {
        "managerId": "manager-main",
        "deviceId": "deck",
        "controlId": "key.0.0",
        "capabilityId": "button.press",
    }
    assert wire["body"]["deviceRef"] == {
        "managerId": "manager-main",
        "deviceId": "deck",
        "fingerprint": "fingerprint:deck",
    }
    assert wire["body"]["eventType"] == "press"
    assert wire["body"]["value"] == {"eventType": "press"}

    parsed = hw_messages.hardware_body_from_message(type(message).from_dict(wire))
    assert parsed == hw_messages.ControlInputMessage(
        deviceRef=DeviceRef(
            managerId="manager-main",
            deviceId="deck",
            fingerprint="fingerprint:deck",
        ),
        controlId="key.0.0",
        capabilityId="button.press",
        eventType="press",
        value={"eventType": "press"},
        sequence=1,
        occurredAt=parsed.occurred_at,
    )


def test_control_command_round_trips_schema_validated_params():
    message = hw_messages.control_command_message(
        controller_id="controller-main",
        sender_session_id="controller-session",
        manager_id="manager-main",
        device_id="deck",
        control_id="key.0.0",
        capability_id="raster.bitmap",
        command_type="set_frame",
        params={
            "image": "AP8Q",
            "encoding": "jpeg",
        },
    )
    wire = message.to_dict()

    assert wire["messageType"] == "controlCommand"
    assert wire["senderSessionId"] == "controller-session"
    assert wire["recipient"]["endpoint"] == "hardware_manager:manager-main"
    assert wire["subject"]["identifiers"]["deviceId"] == "deck"
    assert wire["subject"]["identifiers"]["controlId"] == "key.0.0"
    assert wire["subject"]["identifiers"]["capabilityId"] == "raster.bitmap"
    assert hw_messages.hardware_capability_ref_from_subject(message.subject) == (
        CapabilityRef(
            deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
            controlId="key.0.0",
            capabilityId="raster.bitmap",
        )
    )

    parsed = hw_messages.hardware_body_from_message(type(message).from_dict(wire))
    assert parsed == hw_messages.ControlCommandMessage(
        deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
        controlId="key.0.0",
        capabilityId="raster.bitmap",
        commandType="set_frame",
        params={
            "image": "AP8Q",
            "encoding": "jpeg",
        },
    )


def test_device_level_capability_command_omits_control_id():
    message = hw_messages.control_command_for_capability(
        controller_id="controller-main",
        sender_session_id="controller-session",
        ref=CapabilityRef(
            deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
            capabilityId="device.power",
        ),
        command_type="wake",
        params={},
    )
    wire = message.to_dict()

    assert wire["messageType"] == "controlCommand"
    assert wire["subject"]["kind"] == "hardware_capability"
    assert wire["subject"]["identifiers"] == {
        "managerId": "manager-main",
        "deviceId": "deck",
        "capabilityId": "device.power",
    }
    assert "controlId" not in wire["body"]
    assert hw_messages.hardware_capability_ref_from_subject(message.subject) == (
        CapabilityRef(
            deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
            capabilityId="device.power",
        )
    )

    parsed = hw_messages.hardware_body_from_message(type(message).from_dict(wire))
    assert parsed == hw_messages.ControlCommandMessage(
        deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
        capabilityId="device.power",
        commandType="wake",
        params={},
    )


def test_hardware_refs_are_not_endpoint_addresses():
    ref = DeviceRef(managerId="manager-main", deviceId="deck")
    subject = hw_messages.hardware_subject_for_device(ref)

    assert subject.identifiers["managerId"] == "manager-main"
    assert subject.identifiers["deviceId"] == "deck"
    assert "hardware_manager:" not in subject.identifiers.values()


def test_hardware_bodies_reject_routing_metadata():
    with pytest.raises(ValidationError):
        hw_messages.ControlInputMessage.model_validate(
            {
                "deviceRef": {"managerId": "manager-main", "deviceId": "deck"},
                "controlId": "key.0.0",
                "capabilityId": "button.press",
                "eventType": "press",
                "recipient": "controller:main",
            }
        )


def test_hardware_message_rejects_mismatched_body_instance():
    with pytest.raises(TypeError, match="requires body type ControlInputMessage"):
        hw_messages.hardware_body_for_type(
            hw_messages.CONTROL_INPUT,
            hw_messages.ControlCommandMessage(
                deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
                capabilityId="raster.bitmap",
                commandType="clear",
            ),
        )


def test_hardware_message_builder_validates_message_type_body_pair():
    with pytest.raises(TypeError, match="requires body type ControlInputMessage"):
        hw_messages.hardware_message(
            sender="hardware_manager:manager-main",
            sender_session_id="manager-session",
            recipient="controller:main",
            message_type=hw_messages.CONTROL_INPUT,
            body=hw_messages.ControlCommandMessage(
                deviceRef=DeviceRef(managerId="manager-main", deviceId="deck"),
                capabilityId="raster.bitmap",
                commandType="clear",
            ),
            subject=hw_messages.hardware_subject_for_device(
                DeviceRef(managerId="manager-main", deviceId="deck")
            ),
        )


def test_capability_state_and_reply_messages_validate_targets_and_sequence():
    with pytest.raises(ValidationError, match="capability state target"):
        hw_messages.CapabilityStateChangedMessage.model_validate(
            {
                "deviceRef": {"managerId": "manager-main", "deviceId": "deck"},
                "capabilityId": "",
                "sequence": 1,
            }
        )

    with pytest.raises(ValidationError, match="sequence"):
        hw_messages.CapabilityStateChangedMessage.model_validate(
            {
                "deviceRef": {"managerId": "manager-main", "deviceId": "deck"},
                "capabilityId": "battery.level",
                "sequence": -1,
            }
        )

    with pytest.raises(ValidationError, match="command reply"):
        hw_messages.CommandReplyMessage.model_validate(
            {
                "deviceRef": {"managerId": "manager-main", "deviceId": "deck"},
                "capabilityId": "raster.bitmap",
                "commandType": "",
            }
        )


def test_hardware_messages_reject_non_finite_json_values():
    with pytest.raises(ValidationError, match="NaN or Infinity"):
        hw_messages.ControlInputMessage.model_validate(
            {
                "deviceRef": {"managerId": "manager-main", "deviceId": "deck"},
                "controlId": "key.0.0",
                "capabilityId": "button.press",
                "eventType": "press",
                "value": {"level": float("nan")},
            }
        )


def test_hardware_message_schema_exports_typed_bodies():
    schema = hw_messages.hardware_message_schema()

    assert schema["$id"] == "dev.deckr.message.hardware_messages.v1"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "oneOf" in schema
    control_input_variant = next(
        variant
        for variant in schema["oneOf"]
        if variant["allOf"][1]["properties"]["messageType"]["const"]
        == hw_messages.CONTROL_INPUT
    )

    variant_properties = control_input_variant["allOf"][1]["properties"]
    assert variant_properties["lane"]["const"] == "hardware_messages"
    assert variant_properties["body"]["$ref"] == "#/$defs/ControlInputMessage"
    assert schema["$defs"]["ControlInputMessage"]["additionalProperties"] is False
    message_types = {
        variant["allOf"][1]["properties"]["messageType"]["const"]
        for variant in schema["oneOf"]
    }
    removed_suffixes = ("Available", "DescriptorChanged", "Unavailable")
    assert not ({f"device{suffix}" for suffix in removed_suffixes} & message_types)
    assert not (
        {f"Device{suffix}Message" for suffix in removed_suffixes} & set(schema["$defs"])
    )


def test_old_slot_and_gesture_wire_names_are_absent():
    removed_inventory_types = {
        f"device{suffix}"
        for suffix in ("Available", "DescriptorChanged", "Unavailable")
    }
    old_names = {
        "HardwareDevice",
        "HardwareSlot",
        "HardwareCoordinates",
        "HardwareImageFormat",
        "KeyDownMessage",
        "KeyUpMessage",
        "DialRotateMessage",
        "TouchTapMessage",
        "TouchSwipeMessage",
        "SetImageMessage",
        "ClearSlotMessage",
        "SleepScreenMessage",
        "WakeScreenMessage",
        "KEY_DOWN",
        "KEY_UP",
        "DIAL_ROTATE",
        "TOUCH_TAP",
        "TOUCH_SWIPE",
        "SET_IMAGE",
        "CLEAR_SLOT",
        "SLEEP_SCREEN",
        "WAKE_SCREEN",
        "HardwareTransportMessage",
        "DEVICE_AVAILABLE",
        "DEVICE_DESCRIPTOR_CHANGED",
        "DEVICE_UNAVAILABLE",
        "device_available_message",
        "device_descriptor_changed_message",
        "device_unavailable_message",
    }
    for suffix in ("Available", "DescriptorChanged", "Unavailable"):
        old_names.add(f"Device{suffix}Message")

    for name in old_names:
        assert not hasattr(hw_messages, name), name
    for message_type in hw_messages.HARDWARE_BODY_BY_MESSAGE_TYPE:
        assert message_type not in {
            "keyDown",
            "keyUp",
            "dialRotate",
            "touchTap",
            "touchSwipe",
            "setImage",
            "clearSlot",
            "sleepScreen",
            "wakeScreen",
            *removed_inventory_types,
        }
