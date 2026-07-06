from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import CapabilityRef, DeviceRef


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


