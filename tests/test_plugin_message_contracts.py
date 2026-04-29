from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deckr.contracts.messages import host_address
from deckr.pluginhost.messages import (
    KEY_DOWN,
    PLUGIN_EXTENSION,
    SET_SETTINGS,
    SET_TITLE,
    WILL_APPEAR,
    ControlBindingDescriptor,
    PluginActionCatalog,
    PluginExtensionBody,
    SettingsBody,
    TitleOptionsBody,
    context_subject,
    plugin_body_for_type,
    subject_action_instance_id,
    subject_binding_id,
    subject_config_id,
    subject_page_session_id,
)


def test_core_plugin_bodies_forbid_stale_routing_identity_fields() -> None:
    with pytest.raises(ValueError):
        plugin_body_for_type("hostOnline", {"hostId": "python"})

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
    binding = ControlBindingDescriptor(
        control_id="0,0",
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


def test_plugin_action_catalog_serializes_actions_by_uuid() -> None:
    catalog = PluginActionCatalog(
        hostId="python",
        hostEndpoint=host_address("python"),
        sessionId="session-1",
        timestamp=datetime(2026, 4, 29, tzinfo=UTC),
        ttlSeconds=15,
        actions={"demo.action": {"uuid": "demo.action", "name": "Demo"}},
    )

    assert catalog.model_dump(by_alias=True, mode="json") == {
        "hostId": "python",
        "hostEndpoint": "host:python",
        "sessionId": "session-1",
        "timestamp": "2026-04-29T00:00:00Z",
        "ttlSeconds": 15,
        "actions": {"demo.action": {"uuid": "demo.action", "name": "Demo"}},
    }


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
                "data": {"bindingId": "binding"},
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


def test_context_subject_carries_explicit_lifecycle_ids() -> None:
    subject = context_subject(
        "ctx-live",
        config_id="device-a",
        action_instance_id="instance-a",
        binding_id="binding-a",
        page_session_id="session-a",
        action_uuid="action-a",
    )

    assert subject.identifiers["contextId"] == "ctx-live"
    assert subject_config_id(subject) == "device-a"
    assert subject_action_instance_id(subject) == "instance-a"
    assert subject_binding_id(subject) == "binding-a"
    assert subject_page_session_id(subject) == "session-a"


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
    extension_schema = PluginExtensionBody.model_json_schema(by_alias=True)

    assert extension_schema["additionalProperties"] is False
    assert {
        "extensionType",
        "extensionSchemaId",
        "data",
    }.issubset(extension_schema["properties"])
