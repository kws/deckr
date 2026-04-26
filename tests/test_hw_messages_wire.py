from __future__ import annotations

import importlib
import sys

import pytest
from pydantic import ValidationError

from deckr.hardware import messages as hw_messages


def test_legacy_hardware_events_module_is_not_importable():
    sys.modules.pop("deckr.hardware.events", None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("deckr.hardware.events")


def _stub_device_info() -> hw_messages.HardwareDevice:
    return hw_messages.HardwareDevice(
        id="local-device",
        fingerprint="fingerprint:local-device",
        hid="hid:local-device",
        name="Stub Device",
        slots=[
            hw_messages.HardwareSlot(
                id="0,0",
                coordinates=hw_messages.HardwareCoordinates(column=0, row=0),
                image_format=hw_messages.HardwareImageFormat(width=72, height=72),
                gestures=["key_down", "key_up"],
            )
        ],
    )


def test_device_connected_serializes_inside_deckr_envelope():
    device = _stub_device_info()
    message = hw_messages.hardware_input_message(
        manager_id="manager-main",
        device_id=device.id,
        body=hw_messages.DeviceConnectedMessage(device=device),
    )
    wire = message.to_dict()

    assert wire["lane"] == "hardware_messages"
    assert wire["messageType"] == "deviceConnected"
    assert wire["sender"] == "hardware_manager:manager-main"
    assert wire["recipient"]["targetType"] == "broadcast"
    assert wire["subject"]["identifiers"] == {
        "managerId": "manager-main",
        "deviceId": "local-device",
    }
    assert wire["body"]["device"]["hid"] == "hid:local-device"
    assert wire["body"]["device"]["fingerprint"] == "fingerprint:local-device"
    assert wire["body"]["device"]["slots"][0]["imageFormat"]["width"] == 72

    parsed = hw_messages.hardware_body_from_message(type(message).from_dict(wire))
    assert isinstance(parsed, hw_messages.DeviceConnectedMessage)
    assert parsed.device.id == device.id
    assert parsed.device.fingerprint == device.fingerprint
    assert parsed.device.slots[0].gestures == ("key_down", "key_up")


def test_hardware_device_fingerprint_is_required():
    with pytest.raises(ValidationError):
        hw_messages.HardwareDevice.model_validate(
            {
                "id": "local-device",
                "hid": "hid:local-device",
                "slots": [],
            }
        )


def test_set_image_command_round_trips_binary_payload():
    message = hw_messages.hardware_command_message(
        controller_id="controller-main",
        manager_id="manager-main",
        message_type=hw_messages.SET_IMAGE,
        device_id="local-device",
        control_id="0,0",
        control_kind="slot",
        body=hw_messages.SetImageMessage(
            slot_id="0,0",
            image=b"\x00\xff\x10",
        ),
    )

    wire = message.to_dict()

    assert wire["messageType"] == "setImage"
    assert wire["recipient"]["endpoint"] == "hardware_manager:manager-main"
    assert wire["subject"]["identifiers"]["deviceId"] == "local-device"
    assert wire["subject"]["identifiers"]["controlId"] == "0,0"
    assert hw_messages.hardware_control_ref_from_subject(message.subject) == (
        hw_messages.HardwareControlRef(
            manager_id="manager-main",
            device_id="local-device",
            control_id="0,0",
            control_kind="slot",
        )
    )
    assert isinstance(wire["body"]["image"], str)

    parsed = hw_messages.hardware_body_from_message(type(message).from_dict(wire))
    assert parsed == hw_messages.SetImageMessage(slot_id="0,0", image=b"\x00\xff\x10")


def test_hardware_refs_are_not_endpoint_addresses():
    ref = hw_messages.HardwareDeviceRef(manager_id="manager-main", device_id="deck")
    subject = hw_messages.hardware_subject_for_device(ref)

    assert subject.identifiers["managerId"] == "manager-main"
    assert subject.identifiers["deviceId"] == "deck"
    assert "hardware_manager:" not in subject.identifiers.values()


def test_hardware_bodies_reject_routing_metadata():
    with pytest.raises(ValidationError):
        hw_messages.KeyDownMessage.model_validate(
            {
                "deviceId": "local-device",
                "keyId": "0,0",
                "internalMetadata": {"source": "transport"},
            }
        )


def test_encoded_remote_device_ids_are_not_contract_helpers():
    assert not hasattr(hw_messages, "build_remote_device_id")
    assert not hasattr(hw_messages, "parse_remote_device_id")
    assert not hasattr(hw_messages, "hardware_manager_id_from_message")
    assert not hasattr(hw_messages, "hardware_event_message")
