from __future__ import annotations

from deckr.hw import events as hw_events


class StubDevice:
    def __init__(self) -> None:
        self.id = "local-device"
        self.hid = "hid:local-device"
        self.name = "Stub Device"
        self.slots = [
            hw_events.HWSlot(
                id="0,0",
                coordinates=hw_events.Coordinates(column=0, row=0),
                image_format=hw_events.HWSImageFormat(width=72, height=72),
                gestures=frozenset({"key_down", "key_up"}),
            )
        ]

    async def set_image(self, slot_id: str, image: bytes) -> None:
        return

    async def clear_slot(self, slot_id: str) -> None:
        return

    async def sleep_screen(self) -> None:
        return

    async def wake_screen(self) -> None:
        return


def test_device_connected_serializes_with_camel_case_wire_fields():
    device = StubDevice()
    event = hw_events.DeviceConnectedEvent(device_id=device.id, device=device)

    message = hw_events.event_to_transport_message(event)
    wire = hw_events.hardware_message_to_wire(message)

    assert wire["type"] == "deviceConnected"
    assert wire["deviceId"] == "local-device"
    assert wire["device"]["hid"] == "hid:local-device"
    assert wire["device"]["name"] == "Stub Device"
    assert wire["device"]["slots"][0]["imageFormat"]["width"] == 72
    assert wire["device"]["slots"][0]["slotType"] == "key"

    parsed = hw_events.hardware_message_from_wire(wire)
    assert isinstance(parsed, hw_events.DeviceConnectedMessage)
    info = hw_events.device_info_from_wire(parsed.device)
    assert info.id == device.id
    assert info.name == "Stub Device"
    assert info.slots[0].gestures == frozenset({"key_down", "key_up"})


def test_set_image_command_round_trips_binary_payload():
    command = hw_events.SetImageCommand(
        device_id="local-device",
        slot_id="0,0",
        image=b"\x00\xff\x10",
    )

    message = hw_events.command_to_transport_message(command)
    wire = hw_events.hardware_message_to_wire(message)

    assert wire["type"] == "setImage"
    assert wire["deviceId"] == "local-device"
    assert wire["slotId"] == "0,0"
    assert isinstance(wire["image"], str)

    parsed = hw_events.hardware_message_from_wire(wire)
    restored = hw_events.transport_message_to_command(parsed)
    assert restored == command


def test_remote_device_id_round_trip():
    remote_id = hw_events.build_remote_device_id("bedroom-pi", "virtual-1")
    parsed = hw_events.parse_remote_device_id(remote_id)

    assert parsed == {
        "manager_id": "bedroom-pi",
        "device_id": "virtual-1",
    }
