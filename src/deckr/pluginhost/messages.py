"""Body contracts for the core ``plugin_messages`` lane."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from deckr.contracts.messages import (
    PLUGIN_MESSAGES_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    EntitySubject,
    MessageTarget,
    controller_address,
    endpoint_target,
    entity_subject,
    host_address,
    message_targets_endpoint,
)
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.hardware.descriptors import CapabilityRef, ControlRef, DeviceRef

_RESERVED_EXTENSION_DATA_FIELDS = frozenset(
    {
        "actionInstanceId",
        "bindingId",
        "configId",
        "contextId",
        "controlId",
        "hostId",
        "pageSessionId",
    }
)


def make_context_id() -> str:
    """Generate an opaque controller-owned plugin context handle."""
    return str(uuid.uuid4())


def make_binding_id() -> str:
    """Generate an opaque controller-owned control binding id."""
    return str(uuid.uuid4())


def make_page_session_id() -> str:
    """Generate an opaque controller-owned dynamic page session id."""
    return str(uuid.uuid4())


class PluginMessageBody(DeckrModel):
    """Base class for typed ``plugin_messages`` lane bodies."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class EmptyPluginBody(PluginMessageBody):
    """Body for messages whose meaning lives entirely in the envelope/subject."""


class PluginExtensionBody(PluginMessageBody):
    """Explicit extension body for plugin lane extension traffic."""

    extension_type: str
    extension_schema_id: str
    data: JsonObject = Field(default_factory=dict)

    @field_validator("data", mode="before")
    @classmethod
    def _thaw_data(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("data", mode="after")
    @classmethod
    def _freeze_data(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        reserved = _RESERVED_EXTENSION_DATA_FIELDS & set(value)
        if reserved:
            fields = ", ".join(sorted(reserved))
            raise ValueError(f"extension data must not contain routing fields: {fields}")
        return freeze_json(value)

    @field_serializer("data")
    def _serialize_data(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


CapabilityViewKind = Literal["raw", "native", "projected", "derived", "extension"]
CapabilityProvenance = Literal["native", "projection", "derivation", "extension"]
CapabilityDirection = Literal["input", "output", "state", "command"]
TemplateRoleCardinality = Literal["single", "collection"]
SettingsScope = Literal["plugin", "action_instance"]
SettingsProvenance = Literal[
    "config_default",
    "user_override",
    "template_override",
    "runtime",
    "stale_schema",
]


def _require_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


class CapabilityRequirementSelector(DeckrModel):
    """One acceptable capability shape for a plugin requirement."""

    capability_id: str | None = Field(default=None, alias="capabilityId")
    family: str | None = None
    capability_type: str | None = Field(default=None, alias="type")
    direction: CapabilityDirection | None = None
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")

    @model_validator(mode="after")
    def _require_selector_field(self) -> CapabilityRequirementSelector:
        if not any(
            (
                self.capability_id,
                self.family,
                self.capability_type,
                self.direction,
                self.event_types,
                self.command_types,
            )
        ):
            raise ValueError("capability selector must include at least one criterion")
        return self


class CapabilityRequirement(DeckrModel):
    """A named plugin input, output, state, config, or diagnostic requirement."""

    name: str
    required: bool = True
    preferences: tuple[CapabilityRequirementSelector, ...]
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")
    views: tuple[CapabilityViewKind, ...] = Field(default_factory=tuple)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _require_text(value, field_name="requirement name")

    @field_validator("preferences", mode="after")
    @classmethod
    def _validate_preferences(
        cls, value: tuple[CapabilityRequirementSelector, ...]
    ) -> tuple[CapabilityRequirementSelector, ...]:
        if not value:
            raise ValueError("capability requirement must include preferences")
        return value


class DynamicPageRoleDescriptor(DeckrModel):
    """A semantic role in a plugin-declared dynamic page template."""

    role_id: str = Field(alias="roleId")
    cardinality: TemplateRoleCardinality = "single"
    optional: bool = False
    min_count: int | None = Field(default=None, alias="min")
    preferred_count: int | None = Field(default=None, alias="preferred")
    max_count: int | None = Field(default=None, alias="max")
    requirements: tuple[CapabilityRequirement, ...]
    layout: JsonObject = Field(default_factory=dict)

    @field_validator("role_id")
    @classmethod
    def _validate_role_id(cls, value: str) -> str:
        return _require_text(value, field_name="dynamic page role id")

    @field_validator("requirements", mode="after")
    @classmethod
    def _validate_requirements(
        cls, value: tuple[CapabilityRequirement, ...]
    ) -> tuple[CapabilityRequirement, ...]:
        if not value:
            raise ValueError("dynamic page role must include capability requirements")
        return value

    @field_validator("layout", mode="before")
    @classmethod
    def _thaw_layout(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("layout", mode="after")
    @classmethod
    def _freeze_layout(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("layout")
    def _serialize_layout(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_counts(self) -> DynamicPageRoleDescriptor:
        counts = [
            count
            for count in (self.min_count, self.preferred_count, self.max_count)
            if count is not None
        ]
        if any(count < 0 for count in counts):
            raise ValueError("dynamic page role counts must be non-negative")
        if (
            self.min_count is not None
            and self.max_count is not None
            and self.min_count > self.max_count
        ):
            raise ValueError("dynamic page role min must not exceed max")
        if (
            self.preferred_count is not None
            and self.min_count is not None
            and self.preferred_count < self.min_count
        ):
            raise ValueError("dynamic page role preferred must be at least min")
        if (
            self.preferred_count is not None
            and self.max_count is not None
            and self.preferred_count > self.max_count
        ):
            raise ValueError("dynamic page role preferred must not exceed max")
        return self


class DynamicPageTemplateDescriptor(DeckrModel):
    """A plugin-declared dynamic page template resolved by the controller."""

    template_id: str = Field(alias="templateId")
    roles: tuple[DynamicPageRoleDescriptor, ...]

    @field_validator("template_id")
    @classmethod
    def _validate_template_id(cls, value: str) -> str:
        return _require_text(value, field_name="dynamic page template id")

    @field_validator("roles", mode="after")
    @classmethod
    def _validate_roles(
        cls, value: tuple[DynamicPageRoleDescriptor, ...]
    ) -> tuple[DynamicPageRoleDescriptor, ...]:
        if not value:
            raise ValueError("dynamic page template must include roles")
        role_ids = [role.role_id for role in value]
        duplicates = {role_id for role_id in role_ids if role_ids.count(role_id) > 1}
        if duplicates:
            raise ValueError(
                "dynamic page template role ids must be unique: "
                + ", ".join(sorted(duplicates))
            )
        return value


class MatchedCapability(DeckrModel):
    """A capability selected by the controller for a binding or page role."""

    requirement_name: str | None = Field(default=None, alias="requirementName")
    role_id: str | None = Field(default=None, alias="roleId")
    capability: CapabilityRef
    family: str
    capability_type: str = Field(alias="type")
    direction: CapabilityDirection
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")
    provenance: CapabilityProvenance = "native"
    source: CapabilityRef | None = None


class BindingMetadata(DeckrModel):
    """Plugin-facing metadata for one active binding lease."""

    plugin_id: str | None = Field(default=None, alias="pluginId")
    action_id: str = Field(alias="actionId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    context_id: str = Field(alias="contextId")
    binding_id: str = Field(alias="bindingId")
    page_session_id: str | None = Field(default=None, alias="pageSessionId")
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_ref: ControlRef = Field(alias="controlRef")
    role_id: str | None = Field(default=None, alias="roleId")
    item_key: str | None = Field(default=None, alias="itemKey")
    handler: str | None = None
    matched_capabilities: tuple[MatchedCapability, ...] = Field(
        default_factory=tuple,
        alias="matchedCapabilities",
    )
    output_generation: int = Field(default=0, alias="outputGeneration")


class ActionInstanceMetadata(DeckrModel):
    """Plugin-facing metadata for one controller-owned action instance."""

    plugin_id: str | None = Field(default=None, alias="pluginId")
    action_id: str = Field(alias="actionId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    context_id: str | None = Field(default=None, alias="contextId")


class PageSessionMetadata(DeckrModel):
    """Plugin-facing metadata for one dynamic page session."""

    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    page_id: str = Field(alias="pageId")
    page_session_id: str = Field(alias="pageSessionId")
    context_id: str = Field(alias="contextId")
    template_id: str | None = Field(default=None, alias="templateId")
    owner_binding_id: str | None = Field(default=None, alias="ownerBindingId")
    bindings: tuple[BindingMetadata, ...] = Field(default_factory=tuple)


class CapabilityInputEvent(DeckrModel):
    """Capability-oriented plugin input event delivered to an active binding."""

    capability: CapabilityRef
    event_type: str = Field(alias="eventType")
    value: JsonValue | None = None
    sequence: int | None = None
    occurred_at: datetime = Field(alias="occurredAt")
    producer: str | None = None
    source: CapabilityRef | None = None
    view: CapabilityViewKind | None = None

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        return _require_text(value, field_name="input event type")

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: Any) -> Any:
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)

    @field_serializer("occurred_at")
    def _serialize_occurred_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ActionInstanceLifecycleBody(PluginMessageBody):
    metadata: ActionInstanceMetadata
    settings: JsonObject = Field(default_factory=dict)
    reason: str | None = None

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class BindingAttachedBody(PluginMessageBody):
    binding: BindingMetadata
    settings: JsonObject = Field(default_factory=dict)

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class BindingDetachedBody(PluginMessageBody):
    binding: BindingMetadata
    reason: str


class PageSessionLifecycleBody(PluginMessageBody):
    page_session: PageSessionMetadata = Field(alias="pageSession")
    reason: str | None = None


class CapabilityInputBody(PluginMessageBody):
    binding: BindingMetadata
    event: CapabilityInputEvent


class BindingOutputBody(PluginMessageBody):
    binding: BindingMetadata
    capability: CapabilityRef
    command_type: str = Field(alias="commandType")
    params: JsonObject = Field(default_factory=dict)
    generation: int

    @field_validator("command_type")
    @classmethod
    def _validate_command_type(cls, value: str) -> str:
        return _require_text(value, field_name="output command type")

    @field_validator("params", mode="before")
    @classmethod
    def _thaw_params(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class SettingsSchemaMetadata(DeckrModel):
    """Schema metadata attached to editable settings targets."""

    schema_id: str | None = Field(default=None, alias="schemaId")
    json_schema: JsonObject | None = Field(default=None, alias="schema")
    stale: bool = False

    @field_validator("json_schema", mode="before")
    @classmethod
    def _thaw_schema(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("json_schema", mode="after")
    @classmethod
    def _freeze_schema(
        cls, value: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("json_schema")
    def _serialize_schema(
        self, value: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None


class SettingsTargetRef(DeckrModel):
    """Durable settings identity independent of live context or binding handles."""

    scope: SettingsScope
    controller_id: str = Field(alias="controllerId")
    config_id: str = Field(alias="configId")
    plugin_id: str | None = Field(default=None, alias="pluginId")
    action_id: str | None = Field(default=None, alias="actionId")
    action_instance_id: str | None = Field(default=None, alias="actionInstanceId")
    stable_id: str | None = Field(default=None, alias="stableId")

    @model_validator(mode="after")
    def _validate_scope_fields(self) -> SettingsTargetRef:
        if self.scope == "plugin":
            if not self.plugin_id:
                raise ValueError("plugin settings target requires pluginId")
            if self.action_id or self.action_instance_id or self.stable_id:
                raise ValueError("plugin settings target must not include action ids")
        if self.scope == "action_instance":
            missing = [
                name
                for name, value in {
                    "pluginId": self.plugin_id,
                    "actionId": self.action_id,
                    "actionInstanceId": self.action_instance_id,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(
                    "action instance settings target missing: "
                    + ", ".join(missing)
                )
        return self

    def key(self) -> str:
        parts = [
            f"scope={self.scope}",
            f"controller={self.controller_id}",
            f"config={self.config_id}",
            f"plugin={self.plugin_id or ''}",
        ]
        if self.scope == "action_instance":
            parts.extend(
                [
                    f"action={self.action_id or ''}",
                    f"instance={self.action_instance_id or ''}",
                    f"stable={self.stable_id or ''}",
                ]
            )
        return "|".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for settings command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class SettingsTargetDescription(DeckrModel):
    """Editor-facing description of one editable settings target."""

    target: SettingsTargetRef
    plugin_id: str = Field(alias="pluginId")
    action_id: str | None = Field(default=None, alias="actionId")
    label: str | None = None
    placement: JsonObject = Field(default_factory=dict)
    schema_metadata: SettingsSchemaMetadata = Field(
        default_factory=SettingsSchemaMetadata,
        alias="schemaMetadata",
    )
    provenance: tuple[SettingsProvenance, ...] = Field(default_factory=tuple)

    @field_validator("placement", mode="before")
    @classmethod
    def _thaw_placement(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("placement", mode="after")
    @classmethod
    def _freeze_placement(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("placement")
    def _serialize_placement(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class SettingsSnapshot(DeckrModel):
    """Current settings value and metadata for a target."""

    target: SettingsTargetRef
    settings: JsonObject = Field(default_factory=dict)
    provenance: tuple[SettingsProvenance, ...] = Field(default_factory=tuple)
    schema_metadata: SettingsSchemaMetadata = Field(
        default_factory=SettingsSchemaMetadata,
        alias="schemaMetadata",
    )

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class SettingsRequestBody(PluginMessageBody):
    target: SettingsTargetRef


class SettingsPatchBody(PluginMessageBody):
    target: SettingsTargetRef
    settings: JsonObject = Field(default_factory=dict)

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class SettingsReplaceBody(SettingsPatchBody):
    pass


class SettingsSnapshotBody(PluginMessageBody):
    target: SettingsTargetRef
    settings: JsonObject = Field(default_factory=dict)
    provenance: tuple[SettingsProvenance, ...] = Field(default_factory=tuple)
    schema_metadata: SettingsSchemaMetadata = Field(
        default_factory=SettingsSchemaMetadata,
        alias="schemaMetadata",
    )

    @classmethod
    def from_snapshot(cls, snapshot: SettingsSnapshot) -> SettingsSnapshotBody:
        return cls(
            target=snapshot.target,
            settings=snapshot.settings,
            provenance=snapshot.provenance,
            schemaMetadata=snapshot.schema_metadata,
        )

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


def _target(
    recipient: str | EndpointAddress | MessageTarget,
) -> MessageTarget:
    if isinstance(recipient, EndpointTarget | BroadcastTarget):
        return recipient
    return endpoint_target(recipient)


def plugin_message(
    *,
    sender: str | EndpointAddress,
    recipient: str | EndpointAddress | MessageTarget,
    message_type: str,
    body: PluginMessageBody | Mapping[str, Any] | None = None,
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    parsed_body = plugin_body_for_type(message_type, body or {})
    return DeckrMessage(
        lane=PLUGIN_MESSAGES_LANE,
        messageType=message_type,
        sender=sender,
        recipient=_target(recipient),
        subject=subject,
        body=parsed_body.to_dict(),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def plugin_body_for_type(
    message_type: str,
    body: PluginMessageBody | Mapping[str, Any],
) -> PluginMessageBody:
    body_type = PLUGIN_BODY_BY_MESSAGE_TYPE.get(message_type)
    if body_type is None:
        raise ValueError(f"Unsupported plugin message type {message_type!r}")
    if isinstance(body, PluginMessageBody):
        if not isinstance(body, body_type):
            raise TypeError(
                f"{message_type!r} requires body type {body_type.__name__}, "
                f"got {type(body).__name__}"
            )
        return body
    return body_type.model_validate(body)


def plugin_body(message: DeckrMessage) -> PluginMessageBody:
    return plugin_body_for_type(message.message_type, message.body)


def plugin_body_dict(message: DeckrMessage) -> Mapping[str, Any]:
    return plugin_body(message).to_dict()


def plugin_message_for_host(message: DeckrMessage, host_id: str) -> bool:
    return message_targets_endpoint(message, host_address(host_id))


def plugin_message_for_controller(
    message: DeckrMessage,
    controller_id: str | None = None,
) -> bool:
    if controller_id is None:
        return message.recipient.endpoint_family == "controller" if isinstance(
            message.recipient, BroadcastTarget
        ) else message.recipient.endpoint.family == "controller"
    return message_targets_endpoint(message, controller_address(controller_id))


def context_subject(
    context_id: str,
    *,
    config_id: str | None = None,
    action_instance_id: str | None = None,
    binding_id: str | None = None,
    page_session_id: str | None = None,
) -> EntitySubject:
    identifiers: dict[str, str] = {"contextId": context_id}
    if config_id is not None:
        identifiers["configId"] = config_id
    if action_instance_id is not None:
        identifiers["actionInstanceId"] = action_instance_id
    if binding_id is not None:
        identifiers["bindingId"] = binding_id
    if page_session_id is not None:
        identifiers["pageSessionId"] = page_session_id
    return entity_subject("context", **identifiers)


def subject_context_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("contextId")
    return str(value) if value is not None else None


def subject_config_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("configId")
    return str(value) if value is not None else None


def subject_action_instance_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("actionInstanceId")
    return str(value) if value is not None else None


def subject_binding_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("bindingId")
    return str(value) if value is not None else None


def subject_page_session_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("pageSessionId")
    return str(value) if value is not None else None


def plugin_host_subject(host_id: str) -> EntitySubject:
    return entity_subject("plugin_host", hostId=host_id)


class ActionDescriptor(DeckrModel):
    """Action identity advertised by a plugin host."""

    action_id: str = Field(alias="actionId")
    name: str | None = None
    plugin_id: str | None = Field(default=None, alias="pluginId")
    requirements: tuple[CapabilityRequirement, ...] | None = None
    dynamic_page_templates: tuple[DynamicPageTemplateDescriptor, ...] | None = Field(
        default=None,
        alias="dynamicPageTemplates",
    )
    controllers: tuple[str, ...] | None = None
    property_inspector_path: str | None = None
    manifest_defaults: JsonObject | None = None
    settings_schema: JsonObject | None = Field(default=None, alias="settingsSchema")
    plugin_settings_schema: JsonObject | None = Field(
        default=None,
        alias="pluginSettingsSchema",
    )

    @field_validator("action_id")
    @classmethod
    def _validate_action_id(cls, value: str) -> str:
        return _require_text(value, field_name="action id")

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="plugin id")

    @field_validator("requirements", mode="after")
    @classmethod
    def _validate_requirements(
        cls,
        value: tuple[CapabilityRequirement, ...] | None,
    ) -> tuple[CapabilityRequirement, ...] | None:
        if value is None:
            return None
        names = [requirement.name for requirement in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(
                "action requirement names must be unique: "
                + ", ".join(sorted(duplicates))
            )
        return value

    @field_validator("dynamic_page_templates", mode="after")
    @classmethod
    def _validate_dynamic_page_templates(
        cls,
        value: tuple[DynamicPageTemplateDescriptor, ...] | None,
    ) -> tuple[DynamicPageTemplateDescriptor, ...] | None:
        if value is None:
            return None
        template_ids = [template.template_id for template in value]
        duplicates = {
            template_id
            for template_id in template_ids
            if template_ids.count(template_id) > 1
        }
        if duplicates:
            raise ValueError(
                "action dynamic page template ids must be unique: "
                + ", ".join(sorted(duplicates))
            )
        return value

    @field_validator(
        "manifest_defaults",
        "settings_schema",
        "plugin_settings_schema",
        mode="before",
    )
    @classmethod
    def _thaw_json_object(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator(
        "manifest_defaults",
        "settings_schema",
        "plugin_settings_schema",
        mode="after",
    )
    @classmethod
    def _freeze_json_object(
        cls,
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("manifest_defaults", "settings_schema", "plugin_settings_schema")
    def _serialize_json_object(
        self,
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for action registration payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class PluginActionCatalog(DeckrModel):
    """Current action catalog advertised by one plugin host endpoint."""

    host_id: str = Field(alias="hostId")
    host_endpoint: EndpointAddress = Field(alias="hostEndpoint")
    session_id: str = Field(alias="sessionId")
    timestamp: datetime
    ttl_seconds: int = Field(alias="ttlSeconds")
    actions: Mapping[str, ActionDescriptor] = Field(default_factory=dict)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_validator("actions", mode="after")
    @classmethod
    def _freeze_actions(
        cls, value: Mapping[str, ActionDescriptor]
    ) -> Mapping[str, ActionDescriptor]:
        return freeze_json(value)

    @field_serializer("actions")
    def _serialize_actions(
        self, value: Mapping[str, ActionDescriptor]
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }


class TitleOptions(DeckrModel):
    """Font and styling options for controller-rendered titles."""

    font_family: str | None = None
    font_size: int | str | None = None
    font_style: str | None = None
    title_color: str | None = None
    title_alignment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class PageChildBindingDescriptor(DeckrModel):
    """One semantic child binding requested for a concrete page session."""

    control_id: str = Field(alias="controlId")
    role_id: str | None = Field(default=None, alias="roleId")
    item_key: str | None = Field(default=None, alias="itemKey")
    handler: str | None = None
    settings: JsonObject = Field(default_factory=dict)
    title_options: TitleOptions | None = None

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str) -> str:
        return _require_text(value, field_name="page child control id")

    @field_validator("role_id", "item_key", "handler")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="page child metadata")

    @field_validator("settings", mode="before")
    @classmethod
    def _thaw_settings(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("settings", mode="after")
    @classmethod
    def _freeze_settings(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("settings")
    def _serialize_settings(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class DynamicPageCommand(DeckrModel):
    """Concrete page-session command resolved by the controller."""

    page_id: str = Field(alias="pageId")
    template_id: str | None = Field(default=None, alias="templateId")
    bindings: tuple[PageChildBindingDescriptor, ...]

    @field_validator("page_id")
    @classmethod
    def _validate_page_id(cls, value: str) -> str:
        return _require_text(value, field_name="dynamic page id")

    @field_validator("bindings", mode="after")
    @classmethod
    def _validate_bindings(
        cls, value: tuple[PageChildBindingDescriptor, ...]
    ) -> tuple[PageChildBindingDescriptor, ...]:
        if not value:
            raise ValueError("dynamic page command must include child bindings")
        control_ids = [binding.control_id for binding in value]
        duplicates = {
            control_id for control_id in control_ids if control_ids.count(control_id) > 1
        }
        if duplicates:
            raise ValueError(
                "dynamic page child control ids must be unique: "
                + ", ".join(sorted(duplicates))
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class OpenPageBody(PluginMessageBody):
    descriptor: DynamicPageCommand


class UpdatePageBody(PluginMessageBody):
    descriptor: DynamicPageCommand


class ReplacePageBody(PluginMessageBody):
    descriptor: DynamicPageCommand


def make_dynamic_page_id() -> str:
    """Generate a unique page ID for dynamic pages."""
    return str(uuid.uuid4())


# Message type constants
ACTION_INSTANCE_CREATED = "actionInstanceCreated"
ACTION_INSTANCE_DESTROYED = "actionInstanceDestroyed"
BINDING_ATTACHED = "bindingAttached"
BINDING_DETACHED = "bindingDetached"
PAGE_SESSION_OPENED = "pageSessionOpened"
PAGE_SESSION_CLOSED = "pageSessionClosed"
CAPABILITY_INPUT = "capabilityInput"
BINDING_OUTPUT = "bindingOutput"
SETTINGS_REQUEST = "settingsRequest"
SETTINGS_PATCH = "settingsPatch"
SETTINGS_REPLACE = "settingsReplace"
SETTINGS_SNAPSHOT = "settingsSnapshot"
OPEN_PAGE = "openPage"
UPDATE_PAGE = "updatePage"
REPLACE_PAGE = "replacePage"
CLOSE_PAGE = "closePage"
PLUGIN_EXTENSION = "pluginExtension"


# Host -> controller commands a controller-lite should implement.
CORE_COMMAND_MESSAGE_TYPES = frozenset(
    {
        BINDING_OUTPUT,
        SETTINGS_REQUEST,
        SETTINGS_PATCH,
        SETTINGS_REPLACE,
    }
)

# Deckr-specific controller extensions beyond the core command set.
DECKR_EXTENSION_COMMAND_MESSAGE_TYPES = frozenset(
    {
        OPEN_PAGE,
        UPDATE_PAGE,
        REPLACE_PAGE,
        CLOSE_PAGE,
    }
)

# Types that are commands/requests from host to controller (need contextId routing)
COMMAND_MESSAGE_TYPES = (
    CORE_COMMAND_MESSAGE_TYPES | DECKR_EXTENSION_COMMAND_MESSAGE_TYPES
)


PLUGIN_BODY_BY_MESSAGE_TYPE: dict[str, type[PluginMessageBody]] = {
    ACTION_INSTANCE_CREATED: ActionInstanceLifecycleBody,
    ACTION_INSTANCE_DESTROYED: ActionInstanceLifecycleBody,
    BINDING_ATTACHED: BindingAttachedBody,
    BINDING_DETACHED: BindingDetachedBody,
    PAGE_SESSION_OPENED: PageSessionLifecycleBody,
    PAGE_SESSION_CLOSED: PageSessionLifecycleBody,
    CAPABILITY_INPUT: CapabilityInputBody,
    BINDING_OUTPUT: BindingOutputBody,
    SETTINGS_REQUEST: SettingsRequestBody,
    SETTINGS_PATCH: SettingsPatchBody,
    SETTINGS_REPLACE: SettingsReplaceBody,
    SETTINGS_SNAPSHOT: SettingsSnapshotBody,
    OPEN_PAGE: OpenPageBody,
    UPDATE_PAGE: UpdatePageBody,
    REPLACE_PAGE: ReplacePageBody,
    CLOSE_PAGE: EmptyPluginBody,
    PLUGIN_EXTENSION: PluginExtensionBody,
}

OpenPageBody.model_rebuild()
UpdatePageBody.model_rebuild()
ReplacePageBody.model_rebuild()
