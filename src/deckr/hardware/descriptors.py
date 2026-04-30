"""Device, control, and capability descriptor contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import (
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json

CapabilityAccess = Literal[
    "emits",
    "readable",
    "settable",
    "requestable",
    "invokable",
]
CapabilityDirection = Literal["input", "output", "state", "command"]
ConnectionStatus = Literal["connected", "available", "unavailable", "unknown"]
ControlGeometryUnit = Literal["grid", "pixel", "normalized", "millimeter"]
ProjectionOwner = Literal["hardware_manager", "adapter", "component"]
ProjectionType = Literal["projection", "derivation"]

DECKR_INPUT_BUTTON = "deckr.input.button"
DECKR_INPUT_ENCODER = "deckr.input.encoder"
DECKR_INPUT_TOUCH = "deckr.input.touch"
DECKR_OUTPUT_RASTER = "deckr.output.raster"
DECKR_DEVICE_POWER = "deckr.device.power"

CORE_CAPABILITY_FAMILIES = frozenset(
    {
        DECKR_DEVICE_POWER,
        DECKR_INPUT_BUTTON,
        DECKR_INPUT_ENCODER,
        DECKR_INPUT_TOUCH,
        DECKR_OUTPUT_RASTER,
    }
)

CORE_CAPABILITY_TYPES_BY_FAMILY: Mapping[str, frozenset[str]] = {
    DECKR_DEVICE_POWER: frozenset({"screen"}),
    DECKR_INPUT_BUTTON: frozenset({"activation", "momentary"}),
    DECKR_INPUT_ENCODER: frozenset({"relative"}),
    DECKR_INPUT_TOUCH: frozenset({"gesture"}),
    DECKR_OUTPUT_RASTER: frozenset({"bitmap"}),
}

BUTTON_ACTIVATION_EVENTS = ("press",)
BUTTON_MOMENTARY_EVENTS = ("down", "up")
ENCODER_RELATIVE_EVENTS = ("rotate",)
TOUCH_GESTURE_EVENTS = ("tap", "swipe")
RASTER_COMMAND_TYPES = ("set_frame", "clear")
POWER_COMMAND_TYPES = ("sleep", "wake")

DESCRIPTOR_SCHEMA_VERSION = "1"
DEVICE_DESCRIPTOR_SCHEMA_ID = "deckr.hardware.device_descriptor.v1"
CONTROL_DESCRIPTOR_SCHEMA_ID = "deckr.hardware.control_descriptor.v1"
CAPABILITY_DESCRIPTOR_SCHEMA_ID = "deckr.hardware.capability_descriptor.v1"

_CONTRACT_NAME_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*$"
_GLOBALLY_QUALIFIED_NAME_PATTERN = (
    r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_EXTENSION_CAPABILITY_FAMILY_PATTERN = (
    r"^(?!deckr\.)[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_CONTRACT_NAME_RE = re.compile(_CONTRACT_NAME_PATTERN)
_GLOBALLY_QUALIFIED_NAME_RE = re.compile(_GLOBALLY_QUALIFIED_NAME_PATTERN)
_ENDPOINT_ADDRESS_RE = re.compile(r"^(controller|host|hardware_manager):")
_JSON_SCHEMA_CONTRACT_KEYS = frozenset(
    {
        "$ref",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "items",
        "oneOf",
        "properties",
        "type",
    }
)
_DIRECTION_ACCESS: Mapping[CapabilityDirection, frozenset[CapabilityAccess]] = {
    "input": frozenset({"emits"}),
    "output": frozenset({"settable", "invokable"}),
    "state": frozenset({"readable", "requestable", "settable", "emits"}),
    "command": frozenset({"invokable"}),
}


def _require_non_empty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_not_endpoint_address(value: str, *, field_name: str) -> str:
    normalized = _require_non_empty(value, field_name=field_name)
    if _ENDPOINT_ADDRESS_RE.match(normalized):
        raise ValueError(f"{field_name} must not be a Deckr endpoint address")
    return normalized


def _require_contract_token(value: str, *, field_name: str) -> str:
    normalized = _require_non_empty(value, field_name=field_name)
    if not _CONTRACT_NAME_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be a lowercase contract identifier, not a display label"
        )
    return normalized


def _require_globally_qualified_name(value: str, *, field_name: str) -> str:
    normalized = _require_contract_token(value, field_name=field_name)
    if not _GLOBALLY_QUALIFIED_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be globally namespaced")
    return normalized


def _require_unique(values: Iterable[str], *, field_name: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"{field_name} must be unique: {', '.join(sorted(duplicates))}")


def _validate_unique_tuple(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    _require_unique(values, field_name=field_name)
    return values


class DeviceIdentifier(DeckrModel):
    """Stable or diagnostic identifier attached to a device."""

    identifier_type: str = Field(alias="type")
    value: str
    namespace: str | None = None
    issuer: str | None = None

    @field_validator("identifier_type")
    @classmethod
    def _validate_identifier_type(cls, value: str) -> str:
        return _require_contract_token(value, field_name="identifier type")

    @field_validator("value", "namespace", "issuer")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="identifier text")


class DeviceConnection(DeckrModel):
    """Structured protocol or transport facts about a device connection."""

    connection_id: str = Field(alias="connectionId")
    connection_type: str = Field(alias="type")
    status: ConnectionStatus = "available"
    label: str | None = None
    transport: str | None = None
    facts: JsonObject = Field(default_factory=dict)

    @field_validator("connection_id")
    @classmethod
    def _validate_connection_id(cls, value: str) -> str:
        return _require_not_endpoint_address(value, field_name="connection_id")

    @field_validator("connection_type")
    @classmethod
    def _validate_connection_type(cls, value: str) -> str:
        return _require_contract_token(value, field_name="connection type")

    @field_validator("label", "transport")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="connection text")

    @field_validator("facts", mode="after")
    @classmethod
    def _freeze_facts(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("facts")
    def _serialize_facts(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class DeviceSourceReference(DeckrModel):
    """Diagnostic reference to a source descriptor, report, layout, or protocol fact."""

    source_id: str = Field(alias="sourceId")
    source_type: str = Field(alias="type")
    connection_id: str | None = Field(default=None, alias="connectionId")
    label: str | None = None
    facts: JsonObject = Field(default_factory=dict)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return _require_not_endpoint_address(value, field_name="source_id")

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, value: str) -> str:
        return _require_contract_token(value, field_name="source type")

    @field_validator("connection_id", "label")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="source text")

    @field_validator("facts", mode="after")
    @classmethod
    def _freeze_facts(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("facts")
    def _serialize_facts(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class DeviceRef(DeckrModel):
    """Reference to a manager-local device in a hardware manager context."""

    manager_id: str = Field(alias="managerId")
    device_id: str = Field(alias="deviceId")
    fingerprint: str | None = None

    @field_validator("manager_id", "device_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _require_not_endpoint_address(value, field_name="device reference")

    @field_validator("fingerprint")
    @classmethod
    def _validate_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="fingerprint")


class ControlRef(DeckrModel):
    """Reference to a control on a concrete device."""

    device_ref: DeviceRef = Field(alias="deviceRef")
    control_id: str = Field(alias="controlId")

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str) -> str:
        return _require_non_empty(value, field_name="control_id")


class CapabilityRef(DeckrModel):
    """Reference to a device-level or control-level capability."""

    device_ref: DeviceRef | None = Field(default=None, alias="deviceRef")
    control_id: str | None = Field(default=None, alias="controlId")
    capability_id: str = Field(alias="capabilityId")

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="control_id")

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _require_contract_token(value, field_name="capability_id")


class ControlGeometry(DeckrModel):
    """A control's placement on a logical or physical surface."""

    x: float = 0
    y: float = 0
    width: float | None = None
    height: float | None = None
    unit: ControlGeometryUnit = "grid"
    rotation: int | None = None
    layer: int | None = None

    @model_validator(mode="after")
    def _validate_geometry(self) -> ControlGeometry:
        if self.width is not None and self.width <= 0:
            raise ValueError("geometry width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("geometry height must be positive")
        if self.unit == "normalized":
            for name, value in {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            }.items():
                if value is not None and not 0 <= value <= 1:
                    raise ValueError(f"normalized geometry {name} must be between 0 and 1")
        return self


class CapabilitySchema(DeckrModel):
    """A JSON Schema fragment carried by a capability descriptor."""

    schema_id: str | None = Field(default=None, alias="schemaId")
    json_schema: Mapping[str, Any] = Field(alias="schema")

    @field_validator("schema_id")
    @classmethod
    def _validate_schema_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_globally_qualified_name(value, field_name="schema_id")

    @field_validator("json_schema", mode="after")
    @classmethod
    def _freeze_schema(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not value:
            raise ValueError("capability schema must not be empty")
        if not any(key in value for key in _JSON_SCHEMA_CONTRACT_KEYS):
            raise ValueError(
                "capability schema must include a JSON Schema contract keyword"
            )
        return freeze_json(value)

    @field_serializer("json_schema")
    def _serialize_schema(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class CapabilityConstraint(DeckrModel):
    """A typed generic constraint on a capability value or command parameter."""

    constraint_type: str = Field(alias="type")
    subject: str
    value: JsonValue | None = None
    values: tuple[JsonValue, ...] = Field(default_factory=tuple)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None

    @field_validator("constraint_type")
    @classmethod
    def _validate_constraint_type(cls, value: str) -> str:
        return _require_contract_token(value, field_name="constraint type")

    @field_validator("subject", "unit")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_contract_token(value, field_name="constraint subject")

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: Any) -> Any:
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("values", mode="after")
    @classmethod
    def _freeze_values(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return freeze_json(value)

    @field_serializer("values")
    def _serialize_values(self, value: tuple[Any, ...]) -> list[Any]:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_constraint(self) -> CapabilityConstraint:
        if (
            self.value is None
            and not self.values
            and self.minimum is None
            and self.maximum is None
            and self.step is None
        ):
            raise ValueError("capability constraint must carry a bound or value")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("capability constraint minimum must not exceed maximum")
        if self.step is not None and self.step <= 0:
            raise ValueError("capability constraint step must be positive")
        return self


class CapabilityUnit(DeckrModel):
    """Unit metadata for a capability field."""

    subject: str
    unit: str
    symbol: str | None = None
    scale: float = 1.0

    @field_validator("subject", "unit", "symbol")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_contract_token(value, field_name="capability unit")

    @field_validator("scale")
    @classmethod
    def _validate_scale(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("capability unit scale must be positive")
        return value


class CapabilityProjection(DeckrModel):
    """Explicit projection or derivation metadata for a capability."""

    projection_type: ProjectionType = Field(default="projection", alias="type")
    owner: ProjectionOwner
    source: CapabilityRef
    description: str | None = None

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="projection description")


class CapabilityDescriptor(DeckrModel):
    """Descriptor for one input, output, state, or command capability."""

    capability_id: str = Field(alias="capabilityId")
    family: str
    capability_type: str = Field(alias="type")
    direction: CapabilityDirection
    access: tuple[CapabilityAccess, ...]
    value_schema: CapabilitySchema | None = Field(default=None, alias="valueSchema")
    command_schema: CapabilitySchema | None = Field(default=None, alias="commandSchema")
    constraints: tuple[CapabilityConstraint, ...] = Field(default_factory=tuple)
    units: tuple[CapabilityUnit, ...] = Field(default_factory=tuple)
    event_types: tuple[str, ...] = Field(default_factory=tuple, alias="eventTypes")
    command_types: tuple[str, ...] = Field(default_factory=tuple, alias="commandTypes")
    projection: CapabilityProjection | None = None
    sources: tuple[DeviceSourceReference, ...] = Field(default_factory=tuple)

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _require_contract_token(value, field_name="capability_id")

    @field_validator("family")
    @classmethod
    def _validate_family(cls, value: str) -> str:
        family = _require_globally_qualified_name(value, field_name="capability family")
        if family.startswith("deckr.") and family not in CORE_CAPABILITY_FAMILIES:
            raise ValueError(f"unsupported Deckr core capability family: {family}")
        return family

    @field_validator("capability_type")
    @classmethod
    def _validate_capability_type(cls, value: str) -> str:
        return _require_contract_token(value, field_name="capability type")

    @field_validator("access", mode="after")
    @classmethod
    def _validate_access(
        cls, value: tuple[CapabilityAccess, ...]
    ) -> tuple[CapabilityAccess, ...]:
        if not value:
            raise ValueError("capability access must not be empty")
        return _validate_unique_tuple(value, field_name="capability access")

    @field_validator("event_types", "command_types", mode="after")
    @classmethod
    def _validate_type_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(
            _require_contract_token(item, field_name="event or command type")
            for item in value
        )
        return _validate_unique_tuple(names, field_name="event or command types")

    @model_validator(mode="after")
    def _validate_capability(self) -> CapabilityDescriptor:
        allowed_access = _DIRECTION_ACCESS[self.direction]
        if not set(self.access) & allowed_access:
            allowed = ", ".join(sorted(allowed_access))
            raise ValueError(
                f"{self.direction} capability access must include one of: {allowed}"
            )
        if self.event_types and self.direction not in {"input", "state"}:
            raise ValueError("event types are only valid on input or state capabilities")
        if self.command_types and self.direction not in {"output", "command"}:
            raise ValueError(
                "command types are only valid on output or command capabilities"
            )
        if self.command_schema is not None and self.direction not in {
            "output",
            "command",
        }:
            raise ValueError(
                "command schema is only valid on output or command capabilities"
            )
        self._validate_core_family_type()
        return self

    def _validate_core_family_type(self) -> None:
        if self.family not in CORE_CAPABILITY_TYPES_BY_FAMILY:
            return
        allowed_types = CORE_CAPABILITY_TYPES_BY_FAMILY[self.family]
        if self.capability_type not in allowed_types:
            allowed = ", ".join(sorted(allowed_types))
            raise ValueError(
                f"{self.family} capability type must be one of: {allowed}"
            )
        if self.family == DECKR_INPUT_BUTTON:
            self._validate_button_events()
        elif self.family == DECKR_INPUT_ENCODER and self.event_types != ENCODER_RELATIVE_EVENTS:
            raise ValueError("deckr.input.encoder relative capabilities emit rotate")
        elif self.family == DECKR_INPUT_TOUCH and not set(self.event_types).issubset(
            TOUCH_GESTURE_EVENTS
        ):
            allowed = ", ".join(TOUCH_GESTURE_EVENTS)
            raise ValueError(f"deckr.input.touch gesture events must be in: {allowed}")
        elif self.family == DECKR_OUTPUT_RASTER and self.command_types:
            if not set(self.command_types).issubset(RASTER_COMMAND_TYPES):
                allowed = ", ".join(RASTER_COMMAND_TYPES)
                raise ValueError(
                    f"deckr.output.raster bitmap commands must be in: {allowed}"
                )
        elif self.family == DECKR_DEVICE_POWER and self.command_types:
            if not set(self.command_types).issubset(POWER_COMMAND_TYPES):
                allowed = ", ".join(POWER_COMMAND_TYPES)
                raise ValueError(
                    f"deckr.device.power screen commands must be in: {allowed}"
                )

    def _validate_button_events(self) -> None:
        if self.capability_type == "activation":
            if self.event_types != BUTTON_ACTIVATION_EVENTS:
                raise ValueError(
                    "deckr.input.button activation capabilities emit press only"
                )
            return
        if self.capability_type == "momentary":
            if self.event_types != BUTTON_MOMENTARY_EVENTS:
                raise ValueError(
                    "deckr.input.button momentary capabilities emit down and up"
                )


class ControlDescriptor(DeckrModel):
    """Descriptor for one addressable user-facing control or region."""

    control_id: str = Field(alias="controlId")
    kind: str
    label: str | None = None
    group_id: str | None = Field(default=None, alias="groupId")
    parent_control_id: str | None = Field(default=None, alias="parentControlId")
    related_control_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="relatedControlIds",
    )
    surface_id: str | None = Field(default=None, alias="surfaceId")
    geometry: ControlGeometry | None = None
    input_capabilities: tuple[CapabilityDescriptor, ...] = Field(
        default_factory=tuple,
        alias="inputCapabilities",
    )
    output_capabilities: tuple[CapabilityDescriptor, ...] = Field(
        default_factory=tuple,
        alias="outputCapabilities",
    )
    state_capabilities: tuple[CapabilityDescriptor, ...] = Field(
        default_factory=tuple,
        alias="stateCapabilities",
    )
    config_capabilities: tuple[CapabilityDescriptor, ...] = Field(
        default_factory=tuple,
        alias="configCapabilities",
    )
    diagnostic_capabilities: tuple[CapabilityDescriptor, ...] = Field(
        default_factory=tuple,
        alias="diagnosticCapabilities",
    )
    sources: tuple[DeviceSourceReference, ...] = Field(default_factory=tuple)

    @field_validator("control_id")
    @classmethod
    def _validate_control_id(cls, value: str) -> str:
        return _require_non_empty(value, field_name="control_id")

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return _require_contract_token(value, field_name="control kind")

    @field_validator(
        "label",
        "group_id",
        "parent_control_id",
        "surface_id",
    )
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="control text")

    @field_validator("related_control_ids", mode="after")
    @classmethod
    def _validate_related_control_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ids = tuple(_require_non_empty(item, field_name="related_control_id") for item in value)
        return _validate_unique_tuple(ids, field_name="related_control_ids")

    @property
    def capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """Return all capabilities owned by this control."""

        return (
            *self.input_capabilities,
            *self.output_capabilities,
            *self.state_capabilities,
            *self.config_capabilities,
            *self.diagnostic_capabilities,
        )

    @model_validator(mode="after")
    def _validate_control(self) -> ControlDescriptor:
        _require_unique(
            (capability.capability_id for capability in self.capabilities),
            field_name=f"capability ids on control {self.control_id}",
        )
        for capability in self.input_capabilities:
            if capability.direction != "input":
                raise ValueError("input_capabilities must have input direction")
        for capability in self.output_capabilities:
            if capability.direction != "output":
                raise ValueError("output_capabilities must have output direction")
        for capability in self.state_capabilities:
            if capability.direction != "state":
                raise ValueError("state_capabilities must have state direction")
        for capability in self.config_capabilities:
            if capability.direction not in {"state", "command"}:
                raise ValueError(
                    "config_capabilities must have state or command direction"
                )
        for capability in self.diagnostic_capabilities:
            if capability.direction != "state":
                raise ValueError("diagnostic_capabilities must have state direction")
        return self


class DeviceDescriptor(DeckrModel):
    """Descriptor for one controller-visible device or sub-surface."""

    device_id: str = Field(alias="deviceId")
    fingerprint: str
    display_name: str = Field(alias="displayName")
    manufacturer: str | None = None
    model: str | None = None
    model_id: str | None = Field(default=None, alias="modelId")
    serial_number: str | None = Field(default=None, alias="serialNumber")
    hardware_version: str | None = Field(default=None, alias="hardwareVersion")
    firmware_version: str | None = Field(default=None, alias="firmwareVersion")
    identifiers: tuple[DeviceIdentifier, ...] = Field(default_factory=tuple)
    connections: tuple[DeviceConnection, ...] = Field(default_factory=tuple)
    parent: DeviceRef | None = None
    default_status_indicator: CapabilityRef | None = Field(
        default=None,
        alias="defaultStatusIndicator",
    )
    controls: tuple[ControlDescriptor, ...] = Field(default_factory=tuple)
    capabilities: tuple[CapabilityDescriptor, ...] = Field(default_factory=tuple)
    sources: tuple[DeviceSourceReference, ...] = Field(default_factory=tuple)

    @field_validator("device_id")
    @classmethod
    def _validate_device_id(cls, value: str) -> str:
        return _require_not_endpoint_address(value, field_name="device_id")

    @field_validator(
        "fingerprint",
        "display_name",
        "manufacturer",
        "model",
        "model_id",
        "serial_number",
        "hardware_version",
        "firmware_version",
    )
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, field_name="device descriptor text")

    @model_validator(mode="after")
    def _validate_device_descriptor(self) -> DeviceDescriptor:
        _require_unique(
            (connection.connection_id for connection in self.connections),
            field_name="connection ids",
        )
        _require_unique(
            (control.control_id for control in self.controls),
            field_name="control ids",
        )
        _require_unique(
            (capability.capability_id for capability in self.capabilities),
            field_name="device-level capability ids",
        )
        self._validate_source_connections()
        self._validate_control_relations()
        self._validate_device_capability_directions()
        self._validate_default_status_indicator()
        self._validate_projections()
        return self

    def _validate_source_connections(self) -> None:
        connection_ids = {connection.connection_id for connection in self.connections}
        for source in (*self.sources, *self._all_sources_from_controls()):
            if source.connection_id is not None and source.connection_id not in connection_ids:
                raise ValueError(
                    f"source {source.source_id} references unknown connection "
                    f"{source.connection_id}"
                )

    def _all_sources_from_controls(self) -> tuple[DeviceSourceReference, ...]:
        sources: list[DeviceSourceReference] = []
        for control in self.controls:
            sources.extend(control.sources)
            for capability in control.capabilities:
                sources.extend(capability.sources)
        for capability in self.capabilities:
            sources.extend(capability.sources)
        return tuple(sources)

    def _validate_control_relations(self) -> None:
        control_ids = {control.control_id for control in self.controls}
        for control in self.controls:
            if control.parent_control_id is not None:
                if control.parent_control_id == control.control_id:
                    raise ValueError("control parent must not reference itself")
                if control.parent_control_id not in control_ids:
                    raise ValueError(
                        f"control {control.control_id} references unknown parent "
                        f"{control.parent_control_id}"
                    )
            for related_id in control.related_control_ids:
                if related_id == control.control_id:
                    raise ValueError("related control must not reference itself")
                if related_id not in control_ids:
                    raise ValueError(
                        f"control {control.control_id} references unknown related "
                        f"control {related_id}"
                    )

    def _validate_device_capability_directions(self) -> None:
        for capability in self.capabilities:
            if capability.direction not in {"state", "command", "output"}:
                raise ValueError(
                    "device-level capabilities must have state, command, or output "
                    "direction"
                )

    def _validate_default_status_indicator(self) -> None:
        if self.default_status_indicator is None:
            return
        capability = self._resolve_capability(self.default_status_indicator)
        if capability is None:
            raise ValueError("default status indicator must reference a real capability")
        if capability.direction != "output":
            raise ValueError("default status indicator must reference an output capability")

    def _validate_projections(self) -> None:
        for control_id, capability in self._all_capabilities():
            projection = capability.projection
            if projection is None:
                continue
            source = projection.source
            source_capability = self._resolve_capability(source)
            if source_capability is None:
                raise ValueError(
                    f"capability {capability.capability_id} projects from unknown "
                    f"capability {source.capability_id}"
                )
            if (
                source.control_id == control_id
                and source.capability_id == capability.capability_id
            ):
                raise ValueError("capability projection must not reference itself")

    def _all_capabilities(self) -> tuple[tuple[str | None, CapabilityDescriptor], ...]:
        result: list[tuple[str | None, CapabilityDescriptor]] = [
            (None, capability) for capability in self.capabilities
        ]
        for control in self.controls:
            result.extend(
                (control.control_id, capability) for capability in control.capabilities
            )
        return tuple(result)

    def _resolve_capability(self, ref: CapabilityRef) -> CapabilityDescriptor | None:
        if ref.device_ref is not None and ref.device_ref.device_id != self.device_id:
            return None
        if ref.control_id is None:
            for capability in self.capabilities:
                if capability.capability_id == ref.capability_id:
                    return capability
            return None
        for control in self.controls:
            if control.control_id != ref.control_id:
                continue
            for capability in control.capabilities:
                if capability.capability_id == ref.capability_id:
                    return capability
        return None


_SCHEMA_MODELS: Mapping[str, tuple[str, type[DeckrModel]]] = {
    DEVICE_DESCRIPTOR_SCHEMA_ID: ("Deckr DeviceDescriptor", DeviceDescriptor),
    CONTROL_DESCRIPTOR_SCHEMA_ID: ("Deckr ControlDescriptor", ControlDescriptor),
    CAPABILITY_DESCRIPTOR_SCHEMA_ID: ("Deckr CapabilityDescriptor", CapabilityDescriptor),
}


def descriptor_schema_artifacts() -> dict[str, dict[str, Any]]:
    """Return JSON Schema artifacts for descriptor consumers."""

    artifacts: dict[str, dict[str, Any]] = {}
    for schema_id, (title, model) in _SCHEMA_MODELS.items():
        schema = model.model_json_schema(
            by_alias=True,
            ref_template="#/$defs/{model}",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = schema_id
        schema["title"] = title
        schema["x-deckr-schema-version"] = DESCRIPTOR_SCHEMA_VERSION
        _apply_descriptor_schema_rules(schema)
        artifacts[schema_id] = schema
    return artifacts


def _apply_descriptor_schema_rules(schema: dict[str, Any]) -> None:
    for candidate in _schema_definitions(schema):
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        if {"capabilityId", "family", "type"}.issubset(properties):
            properties["family"] = {
                "anyOf": [
                    {"enum": sorted(CORE_CAPABILITY_FAMILIES)},
                    {
                        "pattern": _EXTENSION_CAPABILITY_FAMILY_PATTERN,
                        "type": "string",
                    },
                ],
                "description": (
                    "Deckr-owned families must be one of the v1 core families; "
                    "extension families must be globally namespaced and must not "
                    "use the deckr. namespace."
                ),
                "title": "Family",
            }
            type_schema = properties["type"]
            if isinstance(type_schema, dict):
                type_schema["pattern"] = _CONTRACT_NAME_PATTERN


def _schema_definitions(schema: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    definitions = [schema]
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        definitions.extend(item for item in defs.values() if isinstance(item, dict))
    return tuple(definitions)


def device_descriptor_schema() -> dict[str, Any]:
    """Return the canonical ``DeviceDescriptor`` JSON Schema artifact."""

    return descriptor_schema_artifacts()[DEVICE_DESCRIPTOR_SCHEMA_ID]


def control_descriptor_schema() -> dict[str, Any]:
    """Return the canonical ``ControlDescriptor`` JSON Schema artifact."""

    return descriptor_schema_artifacts()[CONTROL_DESCRIPTOR_SCHEMA_ID]


def capability_descriptor_schema() -> dict[str, Any]:
    """Return the canonical ``CapabilityDescriptor`` JSON Schema artifact."""

    return descriptor_schema_artifacts()[CAPABILITY_DESCRIPTOR_SCHEMA_ID]


__all__ = [
    "BUTTON_ACTIVATION_EVENTS",
    "BUTTON_MOMENTARY_EVENTS",
    "CAPABILITY_DESCRIPTOR_SCHEMA_ID",
    "CORE_CAPABILITY_FAMILIES",
    "CORE_CAPABILITY_TYPES_BY_FAMILY",
    "CONTROL_DESCRIPTOR_SCHEMA_ID",
    "DECKR_DEVICE_POWER",
    "DECKR_INPUT_BUTTON",
    "DECKR_INPUT_ENCODER",
    "DECKR_INPUT_TOUCH",
    "DECKR_OUTPUT_RASTER",
    "DESCRIPTOR_SCHEMA_VERSION",
    "DEVICE_DESCRIPTOR_SCHEMA_ID",
    "ENCODER_RELATIVE_EVENTS",
    "POWER_COMMAND_TYPES",
    "RASTER_COMMAND_TYPES",
    "TOUCH_GESTURE_EVENTS",
    "CapabilityAccess",
    "CapabilityConstraint",
    "CapabilityDescriptor",
    "CapabilityDirection",
    "CapabilityProjection",
    "CapabilityRef",
    "CapabilitySchema",
    "CapabilityUnit",
    "ConnectionStatus",
    "ControlDescriptor",
    "ControlGeometry",
    "ControlGeometryUnit",
    "ControlRef",
    "DeviceConnection",
    "DeviceDescriptor",
    "DeviceIdentifier",
    "DeviceRef",
    "DeviceSourceReference",
    "ProjectionOwner",
    "ProjectionType",
    "capability_descriptor_schema",
    "control_descriptor_schema",
    "descriptor_schema_artifacts",
    "device_descriptor_schema",
]
