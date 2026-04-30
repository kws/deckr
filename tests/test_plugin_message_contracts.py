from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deckr.contracts.messages import host_address
from deckr.pluginhost.messages import (
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    PLUGIN_EXTENSION,
    SETTINGS_PATCH,
    ActionDescriptor,
    BindingMetadata,
    CapabilityInputBody,
    CapabilityRequirement,
    CapabilityRequirementSelector,
    DynamicPageCommand,
    DynamicPageRoleDescriptor,
    DynamicPageTemplateDescriptor,
    MatchedCapability,
    PageChildBindingDescriptor,
    PluginActionCatalog,
    PluginExtensionBody,
    SettingsPatchBody,
    SettingsSnapshot,
    SettingsTargetDescription,
    SettingsTargetRef,
    context_subject,
    parse_settings_target_key,
    plugin_body_for_type,
    plugin_message_schema,
    subject_action_instance_id,
    subject_binding_id,
    subject_config_id,
    subject_page_session_id,
)


def _settings_target() -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_instance",
        controllerId="controller-main",
        configId="device-config-1",
        pluginId="demo.plugin",
        actionId="demo.action",
        actionInstanceId="instance-1",
        stableId="weather",
    )


def _binding_metadata() -> BindingMetadata:
    return BindingMetadata(
        actionId="demo.action",
        actionInstanceId="instance-1",
        configId="device-config-1",
        contextId="ctx-1",
        bindingId="binding-1",
        deviceRef={"managerId": "manager-1", "deviceId": "device-1"},
        controlRef={
            "deviceRef": {"managerId": "manager-1", "deviceId": "device-1"},
            "controlId": "0,0",
        },
        roleId="album",
        itemKey="kind-of-blue",
        handler="album",
        matchedCapabilities=[
            MatchedCapability(
                requirementName="press",
                capability={
                    "deviceRef": {"managerId": "manager-1", "deviceId": "device-1"},
                    "controlId": "0,0",
                    "capabilityId": "button.press",
                },
                family="deckr.input.button",
                type="activation",
                direction="input",
                eventTypes=("press",),
                provenance="projection",
            )
        ],
    )


def test_core_plugin_bodies_forbid_stale_routing_identity_fields() -> None:
    with pytest.raises(ValidationError):
        plugin_body_for_type(
            SETTINGS_PATCH,
            {
                "target": _settings_target().to_dict(),
                "settings": {},
                "actionUuid": "other",
            },
        )

    with pytest.raises(ValidationError):
        plugin_body_for_type(
            BINDING_OUTPUT,
            {
                "contextId": "ctx",
                "binding": _binding_metadata().model_dump(by_alias=True, exclude_none=True, mode="json"),
                "capability": {"controlId": "0,0", "capabilityId": "raster.bitmap"},
                "commandType": "clear",
                "generation": 1,
            },
        )


def test_plugin_action_catalog_serializes_actions_by_action_id() -> None:
    catalog = PluginActionCatalog(
        hostId="python",
        hostEndpoint=host_address("python"),
        sessionId="session-1",
        timestamp=datetime(2026, 4, 29, tzinfo=UTC),
        ttlSeconds=15,
        actions={"demo.action": {"actionId": "demo.action", "name": "Demo"}},
    )

    assert catalog.model_dump(by_alias=True, mode="json") == {
        "hostId": "python",
        "hostEndpoint": "host:python",
        "sessionId": "session-1",
        "timestamp": "2026-04-29T00:00:00Z",
        "ttlSeconds": 15,
        "actions": {"demo.action": {"actionId": "demo.action", "name": "Demo"}},
    }


def test_plugin_action_catalog_validates_host_and_action_identity() -> None:
    with pytest.raises(ValidationError, match="hostEndpoint"):
        PluginActionCatalog(
            hostId="python",
            hostEndpoint=host_address("other"),
            sessionId="session-1",
            timestamp=datetime(2026, 4, 29, tzinfo=UTC),
            ttlSeconds=15,
            actions={},
        )

    with pytest.raises(ValidationError, match="map keys"):
        PluginActionCatalog(
            hostId="python",
            hostEndpoint=host_address("python"),
            sessionId="session-1",
            timestamp=datetime(2026, 4, 29, tzinfo=UTC),
            ttlSeconds=15,
            actions={"demo.other": {"actionId": "demo.action", "name": "Demo"}},
        )


def test_action_descriptor_carries_capability_requirements_page_templates_and_settings_schema() -> None:
    descriptor = ActionDescriptor(
        actionId="demo.pager",
        name="Pager",
        requirements=[
            CapabilityRequirement(
                name="press",
                preferences=[
                    CapabilityRequirementSelector(
                        family="deckr.input.button",
                        type="activation",
                        direction="input",
                        eventTypes=("press",),
                    )
                ],
                eventTypes=("press",),
                views=("projected",),
            )
        ],
        dynamicPageTemplates=[
            DynamicPageTemplateDescriptor(
                templateId="browser",
                roles=[
                    DynamicPageRoleDescriptor(
                        roleId="content",
                        cardinality="collection",
                        min=1,
                        preferred=6,
                        requirements=[
                            CapabilityRequirement(
                                name="content-press",
                                preferences=[
                                    CapabilityRequirementSelector(
                                        family="deckr.input.button",
                                        type="activation",
                                        direction="input",
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
        settingsSchema={"type": "object", "properties": {"title": {"type": "string"}}},
        pluginSettingsSchema={"type": "object", "properties": {"token": {"type": "string"}}},
    )

    assert descriptor.to_dict()["requirements"][0]["preferences"] == [
        {
            "family": "deckr.input.button",
            "type": "activation",
            "direction": "input",
            "eventTypes": ["press"],
            "commandTypes": [],
        }
    ]
    assert descriptor.to_dict()["dynamicPageTemplates"][0]["roles"][0]["roleId"] == (
        "content"
    )
    assert descriptor.to_dict()["settingsSchema"]["properties"]["title"]["type"] == (
        "string"
    )
    assert descriptor.to_dict()["pluginSettingsSchema"]["properties"]["token"]["type"] == (
        "string"
    )


def test_settings_target_and_snapshot_are_target_based() -> None:
    target = _settings_target()
    body = SettingsSnapshot(
        target=target,
        settings={"title": "Weather"},
        provenance=("user_override",),
        schemaMetadata={
            "schemaId": "demo.settings.v1",
            "schema": {"type": "object"},
            "stale": False,
        },
    )

    wire = body.to_dict()

    assert wire == {
        "target": {
            "scope": "action_instance",
            "controllerId": "controller-main",
            "configId": "device-config-1",
            "pluginId": "demo.plugin",
            "actionId": "demo.action",
            "actionInstanceId": "instance-1",
            "stableId": "weather",
        },
        "settings": {"title": "Weather"},
        "provenance": ["user_override"],
        "schemaMetadata": {
            "schemaId": "demo.settings.v1",
            "schema": {"type": "object"},
            "stale": False,
        },
    }
    assert "contextId" not in target.key()
    assert "bindingId" not in target.key()
    assert "pageSessionId" not in target.key()
    assert "|" not in target.key()
    assert "=" not in target.key()
    assert parse_settings_target_key(target.key()) == target


def test_settings_target_description_mirrors_target_identity() -> None:
    target = _settings_target()

    with pytest.raises(ValidationError, match="pluginId"):
        SettingsTargetDescription(
            target=target,
            pluginId="other.plugin",
            actionId=target.action_id,
        )

    with pytest.raises(ValidationError, match="actionId"):
        SettingsTargetDescription(
            target=target,
            pluginId=target.plugin_id,
            actionId="other.action",
        )


def test_action_descriptor_rejects_duplicate_requirement_names() -> None:
    requirement = CapabilityRequirement(
        name="press",
        preferences=[CapabilityRequirementSelector(family="deckr.input.button")],
    )

    with pytest.raises(ValidationError, match="requirement names"):
        ActionDescriptor(
            actionId="demo.action",
            requirements=[requirement, requirement],
        )


def test_dynamic_page_command_uses_child_binding_semantics() -> None:
    descriptor = DynamicPageCommand(
        pageId="page-1",
        templateId="artist-pager",
        bindings=[
            PageChildBindingDescriptor(
                controlId="0,0",
                roleId="album",
                itemKey="kind-of-blue",
                handler="album",
                settings={"albumIndex": 0},
            ),
            PageChildBindingDescriptor(
                controlId="0,1",
                roleId="page_control",
                itemKey="close",
                handler="close",
            ),
        ],
    )

    assert descriptor.to_dict() == {
        "pageId": "page-1",
        "templateId": "artist-pager",
        "bindings": [
            {
                "controlId": "0,0",
                "roleId": "album",
                "itemKey": "kind-of-blue",
                "handler": "album",
                "settings": {"albumIndex": 0},
            },
            {
                "controlId": "0,1",
                "roleId": "page_control",
                "itemKey": "close",
                "handler": "close",
                "settings": {},
            },
        ],
    }


def test_v1_capability_input_body_carries_binding_metadata() -> None:
    body = plugin_body_for_type(
        CAPABILITY_INPUT,
        {
            "binding": _binding_metadata().model_dump(by_alias=True, exclude_none=True, mode="json"),
            "event": {
                "capability": {
                    "deviceRef": {"managerId": "manager-1", "deviceId": "device-1"},
                    "controlId": "0,0",
                    "capabilityId": "button.press",
                },
                "eventType": "press",
                "value": {"pressed": True},
                "sequence": 12,
                "occurredAt": "2026-04-30T10:00:00Z",
                "producer": "hardware_manager",
                "view": "projected",
            },
        },
    )

    assert isinstance(body, CapabilityInputBody)
    assert body.to_dict()["binding"]["handler"] == "album"
    assert body.to_dict()["event"]["eventType"] == "press"


def test_plugin_metadata_rejects_empty_ids_and_negative_sequences() -> None:
    with pytest.raises(ValidationError, match="binding metadata id"):
        BindingMetadata.model_validate(
            {
                **_binding_metadata().model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "bindingId": "",
            }
        )

    with pytest.raises(ValidationError, match="sequence"):
        plugin_body_for_type(
            CAPABILITY_INPUT,
            {
                "binding": _binding_metadata().model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "event": {
                    "capability": {
                        "deviceRef": {
                            "managerId": "manager-1",
                            "deviceId": "device-1",
                        },
                        "controlId": "0,0",
                        "capabilityId": "button.press",
                    },
                    "eventType": "press",
                    "sequence": -1,
                    "occurredAt": "2026-04-30T10:00:00Z",
                },
            },
        )


def test_plugin_capability_refs_for_binding_io_must_be_bound_to_control() -> None:
    with pytest.raises(ValidationError, match="requires deviceRef and controlId"):
        plugin_body_for_type(
            CAPABILITY_INPUT,
            {
                "binding": _binding_metadata().model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "event": {
                    "capability": {"capabilityId": "button.press"},
                    "eventType": "press",
                    "occurredAt": "2026-04-30T10:00:00Z",
                },
            },
        )

    with pytest.raises(ValidationError, match="requires deviceRef and controlId"):
        plugin_body_for_type(
            BINDING_OUTPUT,
            {
                "binding": _binding_metadata().model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {"capabilityId": "raster.bitmap"},
                "commandType": "clear",
                "generation": 1,
            },
        )


def test_capability_requirement_selectors_reject_malformed_names() -> None:
    with pytest.raises(ValidationError, match="capability family"):
        CapabilityRequirementSelector(family="Deckr Button")

    with pytest.raises(ValidationError, match="event type"):
        CapabilityRequirementSelector(
            family="deckr.input.button",
            eventTypes=("Press!",),
        )


def test_v1_binding_output_body_targets_matched_capability() -> None:
    body = plugin_body_for_type(
        BINDING_OUTPUT,
        {
            "binding": _binding_metadata().model_dump(by_alias=True, exclude_none=True, mode="json"),
            "capability": {
                "deviceRef": {"managerId": "manager-1", "deviceId": "device-1"},
                "controlId": "0,0",
                "capabilityId": "raster.bitmap",
            },
            "commandType": "set_frame",
            "params": {"image": "abc", "encoding": "jpeg"},
            "generation": 3,
        },
    )

    assert body.to_dict()["commandType"] == "set_frame"
    assert body.to_dict()["params"] == {"image": "abc", "encoding": "jpeg"}


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
                "data": {"actionId": "demo.action"},
            },
        )


def test_context_subject_carries_explicit_lifecycle_ids() -> None:
    subject = context_subject(
        "ctx-live",
        config_id="device-a",
        action_instance_id="instance-a",
        binding_id="binding-a",
        page_session_id="session-a",
    )

    assert subject.identifiers["contextId"] == "ctx-live"
    assert subject_config_id(subject) == "device-a"
    assert subject_action_instance_id(subject) == "instance-a"
    assert subject_binding_id(subject) == "binding-a"
    assert subject_page_session_id(subject) == "session-a"


def test_plugin_body_for_type_rejects_mismatched_body_instances() -> None:
    with pytest.raises(TypeError, match="requires body type SettingsPatchBody"):
        plugin_body_for_type(
            SETTINGS_PATCH,
            PluginExtensionBody(
                extension_type="com.example.demo",
                extension_schema_id="com.example.demo.v1",
                data={},
            ),
        )

    with pytest.raises(TypeError, match="requires body type PluginExtensionBody"):
        plugin_body_for_type(
            PLUGIN_EXTENSION,
            SettingsPatchBody(
                target=_settings_target(),
                settings={"title": "wrong model"},
            ),
        )


def test_typed_plugin_body_schemas_are_exportable() -> None:
    extension_schema = PluginExtensionBody.model_json_schema(by_alias=True)
    lane_schema = plugin_message_schema()

    assert extension_schema["additionalProperties"] is False
    assert {
        "extensionType",
        "extensionSchemaId",
        "data",
    }.issubset(extension_schema["properties"])
    assert lane_schema["$id"] == "deckr.message.plugin_messages.v1"
    assert lane_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    binding_output_variant = next(
        variant
        for variant in lane_schema["oneOf"]
        if variant["allOf"][1]["properties"]["messageType"]["const"]
        == BINDING_OUTPUT
    )
    assert binding_output_variant["allOf"][1]["properties"]["lane"]["const"] == (
        "plugin_messages"
    )
    assert binding_output_variant["allOf"][1]["properties"]["body"]["$ref"] == (
        "#/$defs/BindingOutputBody"
    )
