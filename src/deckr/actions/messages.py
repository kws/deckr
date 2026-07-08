"""Body contracts for the core ``actions`` lane."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal

from pydantic import (
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from deckr.actions.endpoints import (
    action_provider_address,
    require_provider_instance_id,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import (
    ACTION_MESSAGES_SCHEMA_ID,
    ACTIONS_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    EntitySubject,
    MessageTarget,
    controller_address,
    endpoint_target,
    entity_subject,
    message_targets_endpoint,
)
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.hardware.descriptors import (
    CORE_CAPABILITY_FAMILIES,
    CapabilityDirection,
    CapabilityRef,
    ControlRef,
    DeviceRef,
)

_RESERVED_EXTENSION_DATA_FIELDS = frozenset(
    {
        "actionId",
        "actionInstanceId",
        "actionUuid",
        "action_uuid",
        "bindingId",
        "capabilityId",
        "configId",
        "controlId",
        "controlRef",
        "contextId",
        "deviceRef",
        "providerInstanceId",
        "pageSessionId",
        "providerId",
        "uuid",
    }
)

_CONTRACT_NAME_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*$"
_GLOBALLY_QUALIFIED_NAME_PATTERN = (
    r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_EXTENSION_CAPABILITY_FAMILY_PATTERN = (
    r"^(?!dev\.deckr\.)[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_CONTRACT_NAME_RE = re.compile(_CONTRACT_NAME_PATTERN)
_GLOBALLY_QUALIFIED_NAME_RE = re.compile(_GLOBALLY_QUALIFIED_NAME_PATTERN)
_EXTENSION_CAPABILITY_FAMILY_RE = re.compile(_EXTENSION_CAPABILITY_FAMILY_PATTERN)

ActionWarmPolicy = Literal["stop_on_unmount", "keep_until_stopped"]


def _reserved_extension_data_paths(
    value: Any,
    *,
    path: str = "data",
) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in _RESERVED_EXTENSION_DATA_FIELDS:
                paths.append(child_path)
            paths.extend(_reserved_extension_data_paths(item, path=child_path))
        return tuple(paths)
    if isinstance(value, list | tuple):
        paths: list[str] = []
        for index, item in enumerate(value):
            paths.extend(
                _reserved_extension_data_paths(item, path=f"{path}[{index}]")
            )
        return tuple(paths)
    return ()


def make_context_id() -> str:
    """Generate an opaque controller-owned action context handle."""
    return str(uuid.uuid4())


def make_binding_id() -> str:
    """Generate an opaque controller-owned control binding id."""
    return str(uuid.uuid4())


def make_page_session_id() -> str:
    """Generate an opaque controller-owned dynamic page session id."""
    return str(uuid.uuid4())


class ActionMessageBody(DeckrModel):
    """Base class for typed ``actions`` lane bodies."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class EmptyActionBody(ActionMessageBody):
    """Body for messages whose meaning lives entirely in the envelope/subject."""


class ActionExtensionBody(ActionMessageBody):
    """Explicit extension body for action lane extension traffic."""

    extension_type: str
    extension_schema_id: str
    data: JsonObject = Field(default_factory=dict)

    @field_validator("extension_type", "extension_schema_id")
    @classmethod
    def _validate_extension_identity(cls, value: str) -> str:
        return _require_globally_qualified_name(value, field_name="extension identity")

    @field_validator("data", mode="before")
    @classmethod
    def _thaw_data(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("data", mode="after")
    @classmethod
    def _freeze_data(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        reserved = _reserved_extension_data_paths(value)
        if reserved:
            fields = ", ".join(sorted(reserved))
            raise ValueError(f"extension data must not contain routing fields: {fields}")
        return freeze_json(value)

    @field_serializer("data")
    def _serialize_data(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


CapabilityViewKind = Literal["raw", "native", "projected", "derived", "extension"]
CapabilityProvenance = Literal["native", "projection", "derivation", "extension"]
SettingsScope = Literal["action_provider_instance", "action_instance"]
PageChildBindingTargetKind = Literal["self", "action"]
ActionLifecycleRejectionTargetKind = Literal[
    "action_instance",
    "binding",
    "page_session",
]
ActionLifecycleRejectionReason = Literal[
    "action_not_available",
    "provider_not_ready",
    "invalid_settings",
    "unsupported_capability",
    "resource_unavailable",
    "permission_denied",
    "stale_lifecycle",
    "internal_error",
]
SettingsProvenance = Literal[
    "config_default",
    "user_override",
    "template_override",
    "runtime",
    "stale_schema",
]
ActionAvailabilityStatus = Literal["available", "unavailable", "probing"]


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name=field_name)


def _require_non_negative(value: int | None, *, field_name: str) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _require_contract_name(value: str, *, field_name: str) -> str:
    normalized = _require_text(value, field_name=field_name)
    if not _CONTRACT_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase contract identifier")
    return normalized


def _require_globally_qualified_name(value: str, *, field_name: str) -> str:
    normalized = _require_contract_name(value, field_name=field_name)
    if not _GLOBALLY_QUALIFIED_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be globally namespaced")
    return normalized


def _require_capability_family(value: str, *, field_name: str) -> str:
    normalized = _require_text(value, field_name=field_name)
    if normalized in CORE_CAPABILITY_FAMILIES:
        return normalized
    if _EXTENSION_CAPABILITY_FAMILY_RE.fullmatch(normalized):
        return normalized
    raise ValueError(
        f"{field_name} must be a Deckr core capability family or globally namespaced extension family"
    )


def _validate_contract_names(
    values: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    return tuple(
        _require_contract_name(value, field_name=field_name) for value in values
    )


def _require_bound_capability_ref(
    ref: CapabilityRef,
    *,
    field_name: str,
) -> CapabilityRef:
    if ref.device_ref is None or ref.control_id is None:
        raise ValueError(f"{field_name} requires deviceRef and controlId")
    return ref


class CapabilityRequirementSelector(DeckrModel):
    """One acceptable capability shape for an action requirement."""

    capability_id: str | None = Field(default=None, alias="capabilityId")
    family: str | None = None
    capability_type: str | None = Field(default=None, alias="type")
    direction: CapabilityDirection | None = None
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_contract_name(value, field_name="capability id")

    @field_validator("family")
    @classmethod
    def _validate_family(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_capability_family(value, field_name="capability family")

    @field_validator("capability_type")
    @classmethod
    def _validate_capability_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_contract_name(value, field_name="capability type")

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="event type")

    @field_validator("command_types")
    @classmethod
    def _validate_command_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="command type")

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
    """A named action input, output, state, config, or diagnostic requirement."""

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

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="event type")

    @field_validator("command_types")
    @classmethod
    def _validate_command_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="command type")

    @field_validator("preferences", mode="after")
    @classmethod
    def _validate_preferences(
        cls, value: tuple[CapabilityRequirementSelector, ...]
    ) -> tuple[CapabilityRequirementSelector, ...]:
        if not value:
            raise ValueError("capability requirement must include preferences")
        return value


class MatchedCapability(DeckrModel):
    """A capability selected by the controller for a binding requirement."""

    requirement_name: str | None = Field(default=None, alias="requirementName")
    capability: CapabilityRef
    family: str
    capability_type: str = Field(alias="type")
    direction: CapabilityDirection
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")
    provenance: CapabilityProvenance = "native"
    source: CapabilityRef | None = None

    @field_validator("requirement_name")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="matched capability metadata")

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: CapabilityRef) -> CapabilityRef:
        return _require_bound_capability_ref(value, field_name="matched capability")

    @field_validator("family")
    @classmethod
    def _validate_family(cls, value: str) -> str:
        return _require_capability_family(value, field_name="capability family")

    @field_validator("capability_type")
    @classmethod
    def _validate_capability_type(cls, value: str) -> str:
        return _require_contract_name(value, field_name="capability type")

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="event type")

    @field_validator("command_types")
    @classmethod
    def _validate_command_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_contract_names(value, field_name="command type")


class BindingMetadata(DeckrModel):
    """Action-provider-facing metadata for one controller-owned control binding."""

    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_id: str = Field(alias="actionId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    context_id: str = Field(alias="contextId")
    binding_id: str = Field(alias="bindingId")
    page_session_id: str | None = Field(default=None, alias="pageSessionId")
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_ref: ControlRef = Field(alias="controlRef")
    item_key: str | None = Field(default=None, alias="itemKey")
    handler: str | None = None
    matched_capabilities: tuple[MatchedCapability, ...] = Field(
        default_factory=tuple,
        alias="matchedCapabilities",
    )
    output_generation: int = Field(default=0, alias="outputGeneration")

    @field_validator(
        "action_id",
        "action_instance_id",
        "config_id",
        "context_id",
        "binding_id",
        "provider_id",
        "provider_instance_id",
    )
    @classmethod
    def _validate_required_ids(cls, value: str) -> str:
        return _require_text(value, field_name="binding metadata id")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("page_session_id", "item_key", "handler")
    @classmethod
    def _validate_optional_ids(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="binding metadata id")

    @field_validator("output_generation")
    @classmethod
    def _validate_output_generation(cls, value: int) -> int:
        return _require_non_negative(value, field_name="output generation") or 0


class ActionInstanceMetadata(DeckrModel):
    """Action-provider-facing metadata for one controller-owned action instance."""

    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_id: str = Field(alias="actionId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    context_id: str = Field(alias="contextId")

    @field_validator(
        "provider_instance_id",
        "provider_id",
        "action_id",
        "action_instance_id",
        "config_id",
        "context_id",
    )
    @classmethod
    def _validate_required_ids(cls, value: str) -> str:
        return _require_text(value, field_name="action instance metadata id")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")


class PageSessionMetadata(DeckrModel):
    """Action-provider-facing metadata for one dynamic page session."""

    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    page_id: str = Field(alias="pageId")
    page_session_id: str = Field(alias="pageSessionId")
    context_id: str = Field(alias="contextId")
    owner_binding_id: str | None = Field(default=None, alias="ownerBindingId")
    bindings: tuple[BindingMetadata, ...] = Field(default_factory=tuple)

    @field_validator(
        "action_instance_id",
        "config_id",
        "page_id",
        "page_session_id",
        "context_id",
        "provider_instance_id",
        "provider_id",
    )
    @classmethod
    def _validate_required_ids(cls, value: str) -> str:
        return _require_text(value, field_name="page session metadata id")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("owner_binding_id")
    @classmethod
    def _validate_optional_ids(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="page session metadata id")


class CapabilityInputEvent(DeckrModel):
    """Capability-oriented action input event delivered to an active binding."""

    capability: CapabilityRef
    event_type: str = Field(alias="eventType")
    value: JsonValue | None = None
    sequence: int | None = None
    occurred_at: datetime = Field(alias="occurredAt")
    producer: str | None = None
    source: CapabilityRef | None = None
    view: CapabilityViewKind | None = None

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: CapabilityRef) -> CapabilityRef:
        return _require_bound_capability_ref(value, field_name="input capability")

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        return _require_contract_name(value, field_name="input event type")

    @field_validator("sequence")
    @classmethod
    def _validate_sequence(cls, value: int | None) -> int | None:
        return _require_non_negative(value, field_name="sequence")

    @field_validator("producer")
    @classmethod
    def _validate_producer(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="producer")

    @field_validator("value", mode="before")
    @classmethod
    def _thaw_value(cls, value: Any) -> Any:
        return thaw_json(value)

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


class ActionInstanceLifecycleBody(ActionMessageBody):
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


class BindingAttachedBody(ActionMessageBody):
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


class BindingDetachedBody(ActionMessageBody):
    binding: BindingMetadata
    reason: str


class PageSessionLifecycleBody(ActionMessageBody):
    page_session: PageSessionMetadata = Field(alias="pageSession")
    reason: str | None = None


class ActionLifecycleRejectedBody(ActionMessageBody):
    target_kind: ActionLifecycleRejectionTargetKind = Field(alias="targetKind")
    action_instance: ActionInstanceMetadata | None = Field(
        default=None,
        alias="actionInstance",
    )
    binding: BindingMetadata | None = None
    page_session: PageSessionMetadata | None = Field(
        default=None,
        alias="pageSession",
    )
    reason: ActionLifecycleRejectionReason
    message: str | None = None
    retryable: bool = False
    details: JsonObject = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="rejection message")

    @field_validator("details", mode="before")
    @classmethod
    def _thaw_details(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("details", mode="after")
    @classmethod
    def _freeze_details(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("details")
    def _serialize_details(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_target(self) -> ActionLifecycleRejectedBody:
        targets = {
            "action_instance": self.action_instance,
            "binding": self.binding,
            "page_session": self.page_session,
        }
        present = [kind for kind, target in targets.items() if target is not None]
        if present != [self.target_kind]:
            raise ValueError(
                "action lifecycle rejection requires exactly one target matching targetKind"
            )
        return self


class CapabilityInputBody(ActionMessageBody):
    binding: BindingMetadata
    event: CapabilityInputEvent


class BindingOutputBody(ActionMessageBody):
    binding: BindingMetadata
    capability: CapabilityRef
    command_type: str = Field(alias="commandType")
    params: JsonObject = Field(default_factory=dict)
    generation: int

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: CapabilityRef) -> CapabilityRef:
        return _require_bound_capability_ref(value, field_name="output capability")

    @field_validator("command_type")
    @classmethod
    def _validate_command_type(cls, value: str) -> str:
        return _require_contract_name(value, field_name="output command type")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        return _require_non_negative(value, field_name="generation") or 0

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


class BindingOverlayBody(ActionMessageBody):
    binding: BindingMetadata
    template: str
    title: str | None = None
    params: JsonObject = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None, alias="durationSeconds")
    overlay_id: str | None = Field(default=None, alias="overlayId")
    generation: int

    @field_validator("template")
    @classmethod
    def _validate_template(cls, value: str) -> str:
        return _require_contract_name(value, field_name="overlay template")

    @field_validator("title", "overlay_id")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="overlay metadata")

    @field_validator("duration_seconds")
    @classmethod
    def _validate_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not isfinite(value) or value <= 0:
            raise ValueError("durationSeconds must be a positive finite number")
        return value

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        return _require_non_negative(value, field_name="generation") or 0

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


class BindingOverlayClearBody(ActionMessageBody):
    binding: BindingMetadata
    overlay_id: str | None = Field(default=None, alias="overlayId")
    generation: int

    @field_validator("overlay_id")
    @classmethod
    def _validate_overlay_id(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="overlay metadata")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        return _require_non_negative(value, field_name="generation") or 0


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
    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_id: str | None = Field(default=None, alias="actionId")
    action_instance_id: str | None = Field(default=None, alias="actionInstanceId")
    stable_id: str | None = Field(default=None, alias="stableId")

    @field_validator(
        "controller_id",
        "config_id",
        "provider_id",
    )
    @classmethod
    def _validate_required_ids(cls, value: str) -> str:
        return _require_text(value, field_name="settings target id")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("action_id", "action_instance_id", "stable_id")
    @classmethod
    def _validate_optional_ids(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="settings target id")

    @model_validator(mode="after")
    def _validate_scope_fields(self) -> SettingsTargetRef:
        if self.scope == "action_provider_instance":
            if self.action_id or self.action_instance_id or self.stable_id:
                raise ValueError(
                    "action provider instance settings target must not include action ids"
                )
        if self.scope == "action_instance":
            missing = [
                name
                for name, value in {
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
            "settings",
            "target",
            encode_key_token(self.scope),
            encode_key_token(self.controller_id),
            encode_key_token(self.config_id),
            encode_key_token(self.provider_instance_id),
            encode_key_token(self.provider_id),
        ]
        if self.scope == "action_instance":
            parts.extend(
                [
                    encode_key_token(self.action_id or ""),
                    encode_key_token(self.action_instance_id or ""),
                    "1" if self.stable_id is not None else "0",
                ]
            )
            if self.stable_id is not None:
                parts.append(encode_key_token(self.stable_id))
        return ".".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for settings command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def settings_target_key(target: SettingsTargetRef) -> str:
    return target.key()


def parse_settings_target_key(key: str) -> SettingsTargetRef | None:
    parts = key.split(".")
    if len(parts) < 7 or parts[:2] != ["settings", "target"]:
        return None
    try:
        scope = decode_key_token(parts[2])
        controller_id = decode_key_token(parts[3])
        config_id = decode_key_token(parts[4])
        provider_instance_id = decode_key_token(parts[5])
        provider_id = decode_key_token(parts[6])
        if scope == "action_provider_instance" and len(parts) == 7:
            return SettingsTargetRef(
                scope="action_provider_instance",
                controllerId=controller_id,
                configId=config_id,
                providerInstanceId=provider_instance_id,
                providerId=provider_id,
            )
        if scope != "action_instance":
            return None
        if len(parts) not in {10, 11}:
            return None
        stable_flag = parts[9]
        if stable_flag == "0":
            if len(parts) != 10:
                return None
            stable_id = None
        elif stable_flag == "1":
            if len(parts) != 11:
                return None
            stable_id = decode_key_token(parts[10])
        else:
            return None
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=controller_id,
            configId=config_id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
            actionId=decode_key_token(parts[7]),
            actionInstanceId=decode_key_token(parts[8]),
            stableId=stable_id,
        )
    except ValueError:
        return None


class SettingsTargetDescription(DeckrModel):
    """Editor-facing description of one editable settings target."""

    target: SettingsTargetRef
    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_id: str | None = Field(default=None, alias="actionId")
    label: str | None = None
    placement: JsonObject = Field(default_factory=dict)
    schema_metadata: SettingsSchemaMetadata = Field(
        default_factory=SettingsSchemaMetadata,
        alias="schemaMetadata",
    )
    provenance: tuple[SettingsProvenance, ...] = Field(default_factory=tuple)

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, value: str) -> str:
        return _require_text(value, field_name="settings target provider id")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("action_id", "label")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="settings target description")

    @model_validator(mode="after")
    def _validate_target_mirror(self) -> SettingsTargetDescription:
        if self.provider_instance_id != self.target.provider_instance_id:
            raise ValueError(
                "settings target description providerInstanceId must match target"
            )
        if self.provider_id != self.target.provider_id:
            raise ValueError("settings target description providerId must match target")
        if self.action_id != self.target.action_id:
            raise ValueError("settings target description actionId must match target")
        return self

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


class SettingsSnapshot(ActionMessageBody):
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

    @classmethod
    def from_snapshot(cls, snapshot: SettingsSnapshot) -> SettingsSnapshot:
        return cls(
            target=snapshot.target,
            settings=snapshot.settings,
            provenance=snapshot.provenance,
            schemaMetadata=snapshot.schema_metadata,
        )


class SettingsRequestBody(ActionMessageBody):
    target: SettingsTargetRef


class SettingsPatchBody(ActionMessageBody):
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


def _target(
    recipient: str | EndpointAddress | MessageTarget,
) -> MessageTarget:
    if isinstance(recipient, EndpointTarget | BroadcastTarget):
        return recipient
    return endpoint_target(recipient)


def action_message(
    *,
    sender: str | EndpointAddress,
    sender_session_id: str,
    recipient: str | EndpointAddress | MessageTarget,
    recipient_session_id: str | None = None,
    message_type: str,
    body: ActionMessageBody | Mapping[str, Any] | None = None,
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
    contract: ContractPointer | Mapping[str, Any] | None = None,
) -> DeckrMessage:
    parsed_body = action_body_for_type(message_type, body or {})
    return DeckrMessage(
        lane=ACTIONS_LANE,
        messageType=message_type,
        sender=sender,
        senderSessionId=sender_session_id,
        recipient=_target(recipient),
        recipientSessionId=recipient_session_id,
        subject=subject,
        body=parsed_body.to_dict(),
        inReplyTo=in_reply_to,
        causationId=causation_id,
        contract=contract,
    )


def action_body_for_type(
    message_type: str,
    body: ActionMessageBody | Mapping[str, Any],
) -> ActionMessageBody:
    body_type = ACTION_BODY_BY_MESSAGE_TYPE.get(message_type)
    if body_type is None:
        raise ValueError(f"Unsupported action message type {message_type!r}")
    if isinstance(body, ActionMessageBody):
        if not isinstance(body, body_type):
            raise TypeError(
                f"{message_type!r} requires body type {body_type.__name__}, "
                f"got {type(body).__name__}"
            )
        return body
    return body_type.model_validate(body)


def action_body(message: DeckrMessage) -> ActionMessageBody:
    return action_body_for_type(message.message_type, message.body)


def action_body_dict(message: DeckrMessage) -> Mapping[str, Any]:
    return action_body(message).to_dict()


def action_message_for_provider(
    message: DeckrMessage,
    provider_instance_id: str,
) -> bool:
    return message_targets_endpoint(message, action_provider_address(provider_instance_id))


def action_message_for_controller(
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
    provider_instance_id: str | None = None,
    provider_id: str | None = None,
    config_id: str | None = None,
    action_instance_id: str | None = None,
    binding_id: str | None = None,
    page_session_id: str | None = None,
) -> EntitySubject:
    identifiers: dict[str, str] = {"contextId": context_id}
    if provider_instance_id is not None:
        identifiers["providerInstanceId"] = require_provider_instance_id(
            provider_instance_id,
            field_name="providerInstanceId",
        )
    if provider_id is not None:
        identifiers["providerId"] = _require_text(provider_id, field_name="provider id")
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


def subject_provider_instance_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("providerInstanceId")
    return str(value) if value is not None else None


def subject_provider_id(subject: EntitySubject) -> str | None:
    value = subject.identifiers.get("providerId")
    return str(value) if value is not None else None


def action_provider_instance_subject(
    provider_instance_id: str,
    *,
    provider_id: str,
) -> EntitySubject:
    return entity_subject(
        "action_provider_instance",
        providerInstanceId=require_provider_instance_id(
            provider_instance_id,
            field_name="providerInstanceId",
        ),
        providerId=_require_text(provider_id, field_name="provider id"),
    )


class ActionDescriptor(DeckrModel):
    """Action identity advertised by an action provider."""

    action_id: str = Field(alias="actionId")
    name: str | None = None
    provider_id: str | None = Field(default=None, alias="providerId")
    requirements: tuple[CapabilityRequirement, ...] | None = None
    controllers: tuple[str, ...] | None = None
    warm_policy: ActionWarmPolicy = Field(
        default="stop_on_unmount",
        alias="warmPolicy",
    )
    property_inspector_path: str | None = None
    manifest_defaults: JsonObject | None = None
    settings_schema: JsonObject | None = Field(default=None, alias="settingsSchema")
    provider_settings_schema: JsonObject | None = Field(
        default=None,
        alias="providerSettingsSchema",
    )

    @field_validator("action_id")
    @classmethod
    def _validate_action_id(cls, value: str) -> str:
        return _require_text(value, field_name="action id")

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="provider id")

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

    @field_validator(
        "manifest_defaults",
        "settings_schema",
        "provider_settings_schema",
        mode="before",
    )
    @classmethod
    def _thaw_json_object(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator(
        "manifest_defaults",
        "settings_schema",
        "provider_settings_schema",
        mode="after",
    )
    @classmethod
    def _freeze_json_object(
        cls,
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("manifest_defaults", "settings_schema", "provider_settings_schema")
    def _serialize_json_object(
        self,
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for action registration payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ActionAvailabilitySelector(DeckrModel):
    """Controller request selector for one provider action."""

    action_id: str = Field(alias="actionId")

    @field_validator("action_id")
    @classmethod
    def _validate_action_id(cls, value: str) -> str:
        return _require_text(value, field_name="availability action id")


class ActionAvailabilityEntry(DeckrModel):
    """Provider-direct availability for one action descriptor."""

    action_id: str = Field(alias="actionId")
    status: ActionAvailabilityStatus
    descriptor: ActionDescriptor | None = None
    reason: str | None = None

    @field_validator("action_id")
    @classmethod
    def _validate_action_id(cls, value: str) -> str:
        return _require_text(value, field_name="availability action id")

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="availability reason")

    @model_validator(mode="after")
    def _validate_descriptor_identity(self) -> ActionAvailabilityEntry:
        if self.status == "available" and self.descriptor is None:
            raise ValueError("available action availability requires descriptor")
        if self.descriptor is not None and self.descriptor.action_id != self.action_id:
            raise ValueError("availability descriptor actionId must match entry actionId")
        return self


class PageChildBindingTarget(DeckrModel):
    """Action target for one dynamic-page child binding."""

    kind: PageChildBindingTargetKind
    action_id: str | None = Field(default=None, alias="actionId")
    provider_instance_id: str | None = Field(default=None, alias="providerInstanceId")
    provider_labels: Mapping[str, str] | None = Field(
        default=None,
        alias="providerLabels",
    )
    instance_key: str | None = Field(default=None, alias="instanceKey")

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return _require_text(value, field_name="page child target kind")

    @field_validator("action_id", "instance_key")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _require_optional_text(value, field_name="page child target")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("provider_labels", mode="after")
    @classmethod
    def _validate_provider_labels(
        cls,
        value: Mapping[str, str] | None,
    ) -> Mapping[str, str] | None:
        if value is None:
            return None
        return freeze_json(
            {
                _require_text(key, field_name="provider label key"): _require_text(
                    item,
                    field_name="provider label value",
                )
                for key, item in value.items()
            }
        )

    @field_serializer("provider_labels")
    def _serialize_provider_labels(
        self,
        value: Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        if value is None:
            return None
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_target_fields(self) -> PageChildBindingTarget:
        if self.kind == "self":
            if (
                self.action_id is not None
                or self.provider_instance_id is not None
                or self.provider_labels
                or self.instance_key is not None
            ):
                raise ValueError("self page child target must not include action selector fields")
            return self

        if self.kind == "action":
            if self.action_id is None:
                raise ValueError("action page child target requires actionId")
            return self

        raise ValueError("page child target kind must be 'self' or 'action'")


class PageChildBindingDescriptor(DeckrModel):
    """One concrete child binding requested for a dynamic page session."""

    control_id: str = Field(alias="controlId")
    target: PageChildBindingTarget
    item_key: str | None = Field(default=None, alias="itemKey")
    handler: str | None = None
    settings: JsonObject = Field(default_factory=dict)

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str) -> str:
        return _require_text(value, field_name="page child control id")

    @field_validator("item_key", "handler")
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
        """Serialize for action command payloads."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class OpenPageBody(ActionMessageBody):
    descriptor: DynamicPageCommand


class ReplacePageBody(ActionMessageBody):
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
ACTION_LIFECYCLE_REJECTED = "actionLifecycleRejected"
CAPABILITY_INPUT = "capabilityInput"
BINDING_OUTPUT = "bindingOutput"
BINDING_OVERLAY = "bindingOverlay"
BINDING_OVERLAY_CLEAR = "bindingOverlayClear"
SETTINGS_REQUEST = "settingsRequest"
SETTINGS_PATCH = "settingsPatch"
SETTINGS_REPLACE = "settingsReplace"
SETTINGS_SNAPSHOT = "settingsSnapshot"
OPEN_PAGE = "openPage"
REPLACE_PAGE = "replacePage"
CLOSE_PAGE = "closePage"
ACTION_EXTENSION = "actionExtension"


ACTION_PROVIDER_COMMAND_MESSAGE_TYPES = frozenset(
    {
        BINDING_OUTPUT,
        BINDING_OVERLAY,
        BINDING_OVERLAY_CLEAR,
        SETTINGS_REQUEST,
        SETTINGS_PATCH,
        SETTINGS_REPLACE,
    }
)

CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES = frozenset(
    {
        ACTION_LIFECYCLE_REJECTED,
        OPEN_PAGE,
        REPLACE_PAGE,
        CLOSE_PAGE,
    }
)

# Types that are commands/requests from action provider to controller (need contextId routing)
COMMAND_MESSAGE_TYPES = (
    ACTION_PROVIDER_COMMAND_MESSAGE_TYPES | CONTROLLER_EXTENSION_COMMAND_MESSAGE_TYPES
)


ACTION_BODY_BY_MESSAGE_TYPE: dict[str, type[ActionMessageBody]] = {
    ACTION_INSTANCE_CREATED: ActionInstanceLifecycleBody,
    ACTION_INSTANCE_DESTROYED: ActionInstanceLifecycleBody,
    BINDING_ATTACHED: BindingAttachedBody,
    BINDING_DETACHED: BindingDetachedBody,
    PAGE_SESSION_OPENED: PageSessionLifecycleBody,
    PAGE_SESSION_CLOSED: PageSessionLifecycleBody,
    ACTION_LIFECYCLE_REJECTED: ActionLifecycleRejectedBody,
    CAPABILITY_INPUT: CapabilityInputBody,
    BINDING_OUTPUT: BindingOutputBody,
    BINDING_OVERLAY: BindingOverlayBody,
    BINDING_OVERLAY_CLEAR: BindingOverlayClearBody,
    SETTINGS_REQUEST: SettingsRequestBody,
    SETTINGS_PATCH: SettingsPatchBody,
    SETTINGS_REPLACE: SettingsReplaceBody,
    SETTINGS_SNAPSHOT: SettingsSnapshot,
    OPEN_PAGE: OpenPageBody,
    REPLACE_PAGE: ReplacePageBody,
    CLOSE_PAGE: EmptyActionBody,
    ACTION_EXTENSION: ActionExtensionBody,
}

OpenPageBody.model_rebuild()
ReplacePageBody.model_rebuild()

_ACTION_NO_CONTRACT_MESSAGE_TYPES = frozenset(
    {}
)

_ACTION_OPTIONAL_CONTRACT_MESSAGE_TYPES = frozenset(
    {
        ACTION_EXTENSION,
    }
)


def action_message_schema() -> dict[str, Any]:
    """Return the canonical ``actions`` lane JSON Schema artifact."""

    definitions: dict[str, Any] = {}
    envelope_ref = _add_schema_model(definitions, DeckrMessage)
    variants: list[dict[str, Any]] = []
    for message_type, body_type in ACTION_BODY_BY_MESSAGE_TYPE.items():
        body_ref = _add_schema_model(definitions, body_type)
        required = ["lane", "messageType", "body"]
        properties: dict[str, Any] = {
            "lane": {"const": ACTIONS_LANE},
            "messageType": {"const": message_type},
            "body": body_ref,
        }
        if message_type in _ACTION_NO_CONTRACT_MESSAGE_TYPES:
            properties["contract"] = False
        elif message_type not in _ACTION_OPTIONAL_CONTRACT_MESSAGE_TYPES:
            required.append("contract")
            properties["contract"] = {"$ref": "#/$defs/ContractPointer"}
        variants.append(
            {
                "allOf": [
                    envelope_ref,
                    {
                        "type": "object",
                        "required": required,
                        "properties": properties,
                    },
                ]
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ACTION_MESSAGES_SCHEMA_ID,
        "title": "Deckr actions Lane Message",
        "x-deckr-schema-version": "1",
        "oneOf": variants,
        "$defs": definitions,
    }


def _add_schema_model(
    definitions: dict[str, Any],
    model: type[DeckrModel],
) -> dict[str, str]:
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    for name, definition in schema.pop("$defs", {}).items():
        _add_schema_definition(definitions, name, definition)
    schema.pop("$schema", None)
    name = model.__name__
    _add_schema_definition(definitions, name, schema)
    return {"$ref": f"#/$defs/{name}"}


def _add_schema_definition(
    definitions: dict[str, Any],
    name: str,
    definition: Mapping[str, Any],
) -> None:
    schema = deepcopy(dict(definition))
    existing = definitions.get(name)
    if existing is not None:
        if existing != schema:
            raise RuntimeError(f"Conflicting schema definition {name!r}")
        return
    definitions[name] = schema
