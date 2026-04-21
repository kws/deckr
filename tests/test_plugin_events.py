"""Unit tests for plugin event models."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from deckr.plugin.events import (
    Coordinates,
    MultiActionPayload,
    SingleActionPayload,
    WillAppear,
    WillAppearPayload,
)


class TestMultiActionPayload:
    """Tests for MultiActionPayload model."""

    def test_create_multi_action_payload(self):
        """Test creating a MultiActionPayload with required fields."""
        payload = MultiActionPayload(
            controller="Keypad",
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        assert payload.controller == "Keypad"
        assert payload.is_in_multi_action is True
        assert payload.resources == {"icon": "path/to/icon.png"}
        assert payload.settings == {"key": "value"}
        assert payload.state is None

    def test_create_multi_action_payload_with_state(self):
        """Test creating a MultiActionPayload with state."""
        payload = MultiActionPayload(
            controller="Keypad",
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
            state=1,
        )
        assert payload.state == 1

    def test_multi_action_payload_serialization_camel_case(self):
        """Test that MultiActionPayload serializes to camelCase."""
        payload = MultiActionPayload(
            controller="Keypad",
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
            state=1,
        )
        data = payload.model_dump(by_alias=True)
        assert "isInMultiAction" in data
        assert data["isInMultiAction"] is True
        assert "controller" in data
        assert "resources" in data
        assert "settings" in data
        assert "state" in data

    def test_multi_action_payload_deserialization_from_camel_case(self):
        """Test that MultiActionPayload can be deserialized from camelCase."""
        data = {
            "controller": "Keypad",
            "isInMultiAction": True,
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
            "state": 1,
        }
        payload = MultiActionPayload.model_validate(data)
        assert payload.controller == "Keypad"
        assert payload.is_in_multi_action is True
        assert payload.resources == {"icon": "path/to/icon.png"}
        assert payload.settings == {"key": "value"}
        assert payload.state == 1

    def test_multi_action_payload_invalid_controller(self):
        """Test that MultiActionPayload rejects invalid controller values."""
        with pytest.raises(ValidationError):
            MultiActionPayload(
                controller="Encoder",  # Only "Keypad" is allowed
                resources={"icon": "path/to/icon.png"},
                settings={"key": "value"},
            )

    def test_multi_action_payload_invalid_is_in_multi_action(self):
        """Test that MultiActionPayload rejects False for is_in_multi_action."""
        with pytest.raises(ValidationError):
            MultiActionPayload(
                controller="Keypad",
                is_in_multi_action=False,  # Must be True
                resources={"icon": "path/to/icon.png"},
                settings={"key": "value"},
            )


class TestSingleActionPayload:
    """Tests for SingleActionPayload model."""

    def test_create_single_action_payload_keypad(self):
        """Test creating a SingleActionPayload with Keypad controller."""
        payload = SingleActionPayload(
            controller="Keypad",
            coordinates=Coordinates(column=2, row=3),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        assert payload.controller == "Keypad"
        assert payload.is_in_multi_action is False
        assert payload.coordinates.column == 2
        assert payload.coordinates.row == 3
        assert payload.resources == {"icon": "path/to/icon.png"}
        assert payload.settings == {"key": "value"}
        assert payload.state is None

    def test_create_single_action_payload_encoder(self):
        """Test creating a SingleActionPayload with Encoder controller."""
        payload = SingleActionPayload(
            controller="Encoder",
            coordinates=Coordinates(column=0, row=0),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        assert payload.controller == "Encoder"
        assert payload.is_in_multi_action is False

    def test_create_single_action_payload_with_state(self):
        """Test creating a SingleActionPayload with state."""
        payload = SingleActionPayload(
            controller="Keypad",
            coordinates=Coordinates(column=1, row=1),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
            state=42,
        )
        assert payload.state == 42

    def test_single_action_payload_serialization_camel_case(self):
        """Test that SingleActionPayload serializes to camelCase."""
        payload = SingleActionPayload(
            controller="Keypad",
            coordinates=Coordinates(column=2, row=3),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
            state=1,
        )
        data = payload.model_dump(by_alias=True)
        assert "isInMultiAction" in data
        assert data["isInMultiAction"] is False
        assert "controller" in data
        assert "coordinates" in data
        assert "resources" in data
        assert "settings" in data
        assert "state" in data

    def test_single_action_payload_deserialization_from_camel_case(self):
        """Test that SingleActionPayload can be deserialized from camelCase."""
        data = {
            "controller": "Keypad",
            "isInMultiAction": False,
            "coordinates": {"column": 2, "row": 3},
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
            "state": 1,
        }
        payload = SingleActionPayload.model_validate(data)
        assert payload.controller == "Keypad"
        assert payload.is_in_multi_action is False
        assert payload.coordinates.column == 2
        assert payload.coordinates.row == 3

    def test_single_action_payload_invalid_controller(self):
        """Test that SingleActionPayload rejects invalid controller values."""
        with pytest.raises(ValidationError):
            SingleActionPayload(
                controller="Invalid",  # Only "Encoder" or "Keypad" allowed
                coordinates=Coordinates(column=0, row=0),
                resources={"icon": "path/to/icon.png"},
                settings={"key": "value"},
            )

    def test_single_action_payload_invalid_is_in_multi_action(self):
        """Test that SingleActionPayload rejects True for is_in_multi_action."""
        with pytest.raises(ValidationError):
            SingleActionPayload(
                controller="Keypad",
                is_in_multi_action=True,  # Must be False
                coordinates=Coordinates(column=0, row=0),
                resources={"icon": "path/to/icon.png"},
                settings={"key": "value"},
            )

    def test_single_action_payload_missing_coordinates(self):
        """Test that SingleActionPayload requires coordinates."""
        with pytest.raises(ValidationError):
            SingleActionPayload(
                controller="Keypad",
                resources={"icon": "path/to/icon.png"},
                settings={"key": "value"},
            )


class TestWillAppearPayload:
    """Tests for WillAppearPayload discriminated union."""

    def test_will_appear_payload_multi_action(self):
        """Test that WillAppearPayload correctly discriminates MultiActionPayload."""
        data = {
            "controller": "Keypad",
            "isInMultiAction": True,
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
        }
        adapter = TypeAdapter(WillAppearPayload)
        payload = adapter.validate_python(data)
        assert isinstance(payload, MultiActionPayload)
        assert payload.is_in_multi_action is True

    def test_will_appear_payload_single_action(self):
        """Test that WillAppearPayload correctly discriminates SingleActionPayload."""
        data = {
            "controller": "Keypad",
            "isInMultiAction": False,
            "coordinates": {"column": 1, "row": 2},
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
        }
        adapter = TypeAdapter(WillAppearPayload)
        payload = adapter.validate_python(data)
        assert isinstance(payload, SingleActionPayload)
        assert payload.is_in_multi_action is False

    def test_will_appear_payload_single_action_encoder(self):
        """Test that WillAppearPayload works with Encoder controller."""
        data = {
            "controller": "Encoder",
            "isInMultiAction": False,
            "coordinates": {"column": 0, "row": 0},
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
        }
        adapter = TypeAdapter(WillAppearPayload)
        payload = adapter.validate_python(data)
        assert isinstance(payload, SingleActionPayload)
        assert payload.controller == "Encoder"

    def test_will_appear_payload_discriminator_required(self):
        """Test that is_in_multi_action is required for discrimination."""
        data = {
            "controller": "Keypad",
            "resources": {"icon": "path/to/icon.png"},
            "settings": {"key": "value"},
        }
        # Without discriminator, validation should fail
        adapter = TypeAdapter(WillAppearPayload)
        with pytest.raises(ValidationError):
            adapter.validate_python(data)


class TestWillAppear:
    """Tests for WillAppear model."""

    def test_create_will_appear_with_multi_action_payload(self):
        """Test creating a WillAppear event with MultiActionPayload."""
        payload = MultiActionPayload(
            controller="Keypad",
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        event = WillAppear(
            action="com.example.action",
            context="context123",
            device="device456",
            payload=payload,
        )
        assert event.event == "willAppear"
        assert event.action == "com.example.action"
        assert event.context == "context123"
        assert event.device == "device456"
        assert isinstance(event.payload, MultiActionPayload)

    def test_create_will_appear_with_single_action_payload(self):
        """Test creating a WillAppear event with SingleActionPayload."""
        payload = SingleActionPayload(
            controller="Encoder",
            coordinates=Coordinates(column=0, row=0),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        event = WillAppear(
            action="com.example.action",
            context="context123",
            device="device456",
            payload=payload,
        )
        assert event.event == "willAppear"
        assert isinstance(event.payload, SingleActionPayload)
        assert event.payload.controller == "Encoder"

    def test_will_appear_serialization(self):
        """Test that WillAppear serializes correctly."""
        payload = MultiActionPayload(
            controller="Keypad",
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
            state=1,
        )
        event = WillAppear(
            action="com.example.action",
            context="context123",
            device="device456",
            payload=payload,
        )
        data = event.model_dump(by_alias=True)
        assert data["event"] == "willAppear"
        assert data["action"] == "com.example.action"
        assert data["context"] == "context123"
        assert data["device"] == "device456"
        assert "payload" in data
        assert data["payload"]["isInMultiAction"] is True

    def test_will_appear_deserialization_from_dict(self):
        """Test that WillAppear can be deserialized from a dictionary."""
        data = {
            "event": "willAppear",
            "action": "com.example.action",
            "context": "context123",
            "device": "device456",
            "payload": {
                "controller": "Keypad",
                "isInMultiAction": True,
                "resources": {"icon": "path/to/icon.png"},
                "settings": {"key": "value"},
                "state": 1,
            },
        }
        event = WillAppear.model_validate(data)
        assert event.event == "willAppear"
        assert event.action == "com.example.action"
        assert isinstance(event.payload, MultiActionPayload)
        assert event.payload.state == 1

    def test_will_appear_deserialization_single_action(self):
        """Test that WillAppear can deserialize SingleActionPayload."""
        data = {
            "event": "willAppear",
            "action": "com.example.action",
            "context": "context123",
            "device": "device456",
            "payload": {
                "controller": "Encoder",
                "isInMultiAction": False,
                "coordinates": {"column": 2, "row": 3},
                "resources": {"icon": "path/to/icon.png"},
                "settings": {"key": "value"},
            },
        }
        event = WillAppear.model_validate(data)
        assert isinstance(event.payload, SingleActionPayload)
        assert event.payload.controller == "Encoder"
        assert event.payload.coordinates.column == 2
        assert event.payload.coordinates.row == 3

    def test_will_appear_json_serialization(self):
        """Test that WillAppear can serialize to JSON."""
        payload = SingleActionPayload(
            controller="Keypad",
            coordinates=Coordinates(column=1, row=1),
            resources={"icon": "path/to/icon.png"},
            settings={"key": "value"},
        )
        event = WillAppear(
            action="com.example.action",
            context="context123",
            device="device456",
            payload=payload,
        )
        json_str = event.model_dump_json(by_alias=True)
        assert "willAppear" in json_str
        assert "com.example.action" in json_str
        assert "isInMultiAction" in json_str

    def test_will_appear_json_deserialization(self):
        """Test that WillAppear can deserialize from JSON."""
        json_str = """{
            "event": "willAppear",
            "action": "com.example.action",
            "context": "context123",
            "device": "device456",
            "payload": {
                "controller": "Keypad",
                "isInMultiAction": true,
                "resources": {"icon": "path/to/icon.png"},
                "settings": {"key": "value"}
            }
        }"""
        event = WillAppear.model_validate_json(json_str)
        assert event.event == "willAppear"
        assert isinstance(event.payload, MultiActionPayload)
