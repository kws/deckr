from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckr.hardware import events as hw_events


def _stub_device_info() -> hw_events.HardwareDevice:
    return hw_events.HardwareDevice(
        id="local-device",
        hid="hid:local-device",
        name="Stub Device",
        slots=[
            hw_events.HardwareSlot(
                id="0,0",
                coordinates=hw_events.HardwareCoordinates(column=0, row=0),
                image_format=hw_events.HardwareImageFormat(width=72, height=72),
                gestures=["key_down", "key_up"],
            )
        ],
    )


def test_device_connected_serializes_inside_deckr_envelope():
    device = _stub_device_info()
    message = hw_events.hardware_input_message(
        manager_id="manager-main",
        device_id=device.id,
        body=hw_events.DeviceConnectedMessage(device=device),
    )
    wire = message.to_dict()

    assert wire["lane"] == "hardware_events"
    assert wire["messageType"] == "deviceConnected"
    assert wire["sender"] == "hardware_manager:manager-main"
    assert wire["recipient"]["targetType"] == "broadcast"
    assert wire["subject"]["identifiers"] == {
        "managerId": "manager-main",
        "deviceId": "local-device",
    }
    assert wire["body"]["device"]["hid"] == "hid:local-device"
    assert wire["body"]["device"]["slots"][0]["imageFormat"]["width"] == 72

    parsed = hw_events.hardware_body_from_message(type(message).from_dict(wire))
    assert isinstance(parsed, hw_events.DeviceConnectedMessage)
    assert parsed.device.id == device.id
    assert parsed.device.slots[0].gestures == ("key_down", "key_up")


def test_set_image_command_round_trips_binary_payload():
    message = hw_events.hardware_command_message(
        controller_id="controller-main",
        manager_id="manager-main",
        message_type=hw_events.SET_IMAGE,
        device_id="local-device",
        control_id="0,0",
        control_kind="slot",
        body=hw_events.SetImageMessage(
            slot_id="0,0",
            image=b"\x00\xff\x10",
        ),
    )

    wire = message.to_dict()

    assert wire["messageType"] == "setImage"
    assert wire["recipient"]["endpoint"] == "hardware_manager:manager-main"
    assert wire["subject"]["identifiers"]["deviceId"] == "local-device"
    assert wire["subject"]["identifiers"]["controlId"] == "0,0"
    assert isinstance(wire["body"]["image"], str)

    parsed = hw_events.hardware_body_from_message(type(message).from_dict(wire))
    assert parsed == hw_events.SetImageMessage(slot_id="0,0", image=b"\x00\xff\x10")


def test_hardware_bodies_reject_routing_metadata():
    with pytest.raises(ValidationError):
        hw_events.KeyDownMessage.model_validate(
            {
                "deviceId": "local-device",
                "keyId": "0,0",
                "internalMetadata": {"source": "transport"},
            }
        )


def test_encoded_remote_device_ids_are_not_contract_helpers():
    assert not hasattr(hw_events, "build_remote_device_id")
    assert not hasattr(hw_events, "parse_remote_device_id")
