from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckr.hardware import events as hw_events


def _stub_device_info() -> hw_events.WireHWDevice:
    return hw_events.WireHWDevice(
        id="local-device",
        hid="hid:local-device",
        name="Stub Device",
        slots=[
            hw_events.WireHWSlot(
                id="0,0",
                coordinates=hw_events.WireCoordinates(column=0, row=0),
                image_format=hw_events.WireHWSImageFormat(width=72, height=72),
                gestures=["key_down", "key_up"],
            )
        ],
    )


def test_device_connected_serializes_with_camel_case_wire_fields():
    device = _stub_device_info()
    message = hw_events.DeviceConnectedMessage(
        device_id=device.id,
        device=device,
    )
    wire = hw_events.hardware_message_to_wire(message)

    assert wire["type"] == "deviceConnected"
    assert wire["deviceId"] == "local-device"
    assert wire["device"]["hid"] == "hid:local-device"
    assert wire["device"]["name"] == "Stub Device"
    assert wire["device"]["slots"][0]["imageFormat"]["width"] == 72
    assert wire["device"]["slots"][0]["slotType"] == "key"

    parsed = hw_events.hardware_message_from_wire(wire)
    assert isinstance(parsed, hw_events.DeviceConnectedMessage)
    assert parsed.device.id == device.id
    assert parsed.device.name == "Stub Device"
    assert parsed.device.slots[0].gestures == ("key_down", "key_up")


def test_set_image_command_round_trips_binary_payload():
    message = hw_events.SetImageMessage(
        device_id="local-device",
        slot_id="0,0",
        image=b"\x00\xff\x10",
    )

    wire = hw_events.hardware_message_to_wire(message)

    assert wire["type"] == "setImage"
    assert wire["deviceId"] == "local-device"
    assert wire["slotId"] == "0,0"
    assert isinstance(wire["image"], str)

    parsed = hw_events.hardware_message_from_wire(wire)
    assert parsed == message


def test_hardware_messages_reject_unknown_routing_metadata():
    with pytest.raises(ValidationError):
        hw_events.KeyDownMessage.model_validate(
            {
                "deviceId": "local-device",
                "keyId": "0,0",
                "internalMetadata": {"source": "transport"},
                "type": "keyDown",
            }
        )


def test_remote_device_id_round_trip():
    remote_id = hw_events.build_remote_device_id("bedroom-pi", "virtual-1")
    parsed = hw_events.parse_remote_device_id(remote_id)

    assert parsed == {
        "manager_id": "bedroom-pi",
        "device_id": "virtual-1",
    }


def test_legacy_hello_messages_are_rejected():
    with pytest.raises(ValidationError):
        hw_events.hardware_message_from_wire(
            {
                "type": "managerHello",
                "managerId": "bedroom-pi",
            }
        )

    with pytest.raises(ValidationError):
        hw_events.hardware_message_from_wire(
            {
                "type": "controllerHello",
                "controllerId": "controller-main",
            }
        )
