from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckr.contracts.messages import controller_address, host_address
from deckr.pluginhost.messages import (
    ACTIONS_REGISTERED,
    HOST_ONLINE,
    KEY_DOWN,
    PLUGIN_EXTENSION,
    SET_SETTINGS,
    SET_TITLE,
    WILL_APPEAR,
    ActionsRegisteredBody,
    PluginExtensionBody,
    SettingsBody,
    SlotBinding,
    TitleOptionsBody,
    plugin_actions_subject,
    plugin_body_dict,
    plugin_body_for_type,
    plugin_message,
)


def test_core_plugin_bodies_forbid_stale_routing_identity_fields() -> None:
    with pytest.raises(ValidationError):
        plugin_body_for_type(HOST_ONLINE, {"hostId": "python"})

    with pytest.raises(ValidationError):
        plugin_body_for_type(SET_TITLE, {"text": "Demo", "contextId": "ctx"})

    with pytest.raises(ValidationError):
        plugin_body_for_type(SET_SETTINGS, {"settings": {}, "actionUuid": "other"})


def test_controller_event_body_takes_context_from_subject_not_payload() -> None:
    body = plugin_body_for_type(
        KEY_DOWN,
        {"event": {"event": "keyDown", "slotId": "0,0"}},
    )
    assert body.to_dict() == {
        "event": {"event": "keyDown", "slotId": "0,0"},
        "settings": {},
    }

    with pytest.raises(ValidationError):
        plugin_body_for_type(
            KEY_DOWN,
            {"event": {"event": "keyDown", "context": "ctx", "slotId": "0,0"}},
        )


def test_controller_event_body_accepts_frozen_json_settings() -> None:
    binding = SlotBinding(
        slot_id="0,0",
        action_uuid="com.example.action",
        settings={
            "slots": ["0,0", "1,0"],
            "extra_mappings": {
                "B1": {"action": "up"},
                "3,0": {
                    "action": "com.example.volume",
                    "settings": {"zone_name": "Bedroom"},
                },
            },
        },
    )

    body = plugin_body_for_type(
        WILL_APPEAR,
        {
            "event": {
                "slot": {
                    "slotId": "0,0",
                    "slotType": "key",
                },
            },
            "settings": binding.settings,
        },
    )

    assert body.to_dict()["settings"] == {
        "slots": ["0,0", "1,0"],
        "extra_mappings": {
            "B1": {"action": "up"},
            "3,0": {
                "action": "com.example.volume",
                "settings": {"zone_name": "Bedroom"},
            },
        },
    }


def test_action_descriptors_keep_registered_action_identity_as_payload_data() -> None:
    message = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("main"),
        message_type=ACTIONS_REGISTERED,
        body={
            "actionUuids": ["demo.action"],
            "actions": [{"uuid": "demo.action", "name": "Demo"}],
        },
        subject=plugin_actions_subject("python"),
    )

    body = plugin_body_dict(message)

    assert body["actionUuids"] == ["demo.action"]
    assert body["actions"] == [{"uuid": "demo.action", "name": "Demo"}]


def test_plugin_extension_body_has_explicit_non_routing_shape() -> None:
    body = plugin_body_for_type(
        PLUGIN_EXTENSION,
        {
            "extensionType": "com.example.demo",
            "extensionSchemaId": "com.example.demo.v1",
            "data": {"value": 1},
        },
    )
    assert body.to_dict() == {
        "extensionType": "com.example.demo",
        "extensionSchemaId": "com.example.demo.v1",
        "data": {"value": 1},
    }

    with pytest.raises(ValidationError):
        plugin_body_for_type(
            PLUGIN_EXTENSION,
            {
                "extensionType": "com.example.demo",
                "extensionSchemaId": "com.example.demo.v1",
                "data": {"contextId": "ctx"},
            },
        )

    with pytest.raises(ValidationError):
        plugin_body_for_type(
            PLUGIN_EXTENSION,
            {
                "extensionType": "com.example.demo",
                "extensionSchemaId": "com.example.demo.v1",
                "hostId": "python",
                "data": {},
            },
        )


def test_plugin_body_for_type_rejects_mismatched_body_instances() -> None:
    with pytest.raises(TypeError, match="requires body type TitleOptionsBody"):
        plugin_body_for_type(
            SET_TITLE,
            SettingsBody(settings={"title": "wrong model"}),
        )

    with pytest.raises(TypeError, match="requires body type PluginExtensionBody"):
        plugin_body_for_type(
            PLUGIN_EXTENSION,
            TitleOptionsBody(text="wrong model"),
        )

    extension_body = PluginExtensionBody(
        extension_type="com.example.demo",
        extension_schema_id="com.example.demo.v1",
        data={"value": 1},
    )
    with pytest.raises(ValueError, match="Unsupported plugin message type"):
        plugin_body_for_type("com.example.demo", extension_body)


def test_typed_plugin_body_schemas_are_exportable() -> None:
    actions_schema = ActionsRegisteredBody.model_json_schema(by_alias=True)
    extension_schema = PluginExtensionBody.model_json_schema(by_alias=True)

    assert actions_schema["additionalProperties"] is False
    assert "actionUuids" in actions_schema["properties"]
    assert extension_schema["additionalProperties"] is False
    assert {
        "extensionType",
        "extensionSchemaId",
        "data",
    }.issubset(extension_schema["properties"])
