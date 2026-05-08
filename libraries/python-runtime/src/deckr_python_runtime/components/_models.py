from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from deckr.contracts.lanes import LaneContract
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from pydantic import Field, field_serializer, field_validator


class ComponentCardinality(StrEnum):
    SINGLETON = "singleton"
    MULTI_INSTANCE = "multi_instance"


class ReadinessState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    UNREADY = "unready"


class ComponentManifest(DeckrModel):
    component_id: str
    consumes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    cardinality: ComponentCardinality = ComponentCardinality.SINGLETON
    lane_contracts: tuple[LaneContract, ...] = ()
    endpoint_slots: tuple[str, ...] = ()
    role: str | None = None

    @field_validator("component_id")
    @classmethod
    def _validate_component_id(cls, value: str) -> str:
        return _non_empty_string(value, field_name="component_id")


class DependencyKind(StrEnum):
    ENDPOINT = "endpoint"
    SERVICE = "service"


class DependencyMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    PREFERRED = "preferred"
    OBSERVED = "observed"


class DependencyConditionState(StrEnum):
    UNKNOWN = "unknown"
    SATISFIED = "satisfied"
    DEGRADED = "degraded"
    UNSATISFIED = "unsatisfied"


class ComponentDependency(DeckrModel):
    name: str
    kind: DependencyKind
    mode: DependencyMode
    endpoint: EndpointAddress
    lane: str
    namespace: str | None = None

    @field_validator("name", "lane")
    @classmethod
    def _validate_required_text(cls, value: str, info) -> str:
        return _non_empty_string(value, field_name=info.field_name)

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty_string(value, field_name="namespace")

    @field_validator("endpoint", mode="before")
    @classmethod
    def _validate_endpoint(cls, value: str | EndpointAddress) -> EndpointAddress:
        return parse_endpoint_address(value)


class DependencyCondition(DeckrModel):
    name: str
    kind: DependencyKind
    mode: DependencyMode
    state: DependencyConditionState
    reason: str | None = None
    diagnostics: JsonObject = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _non_empty_string(value, field_name="name")

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty_string(value, field_name="reason")

    @field_validator("diagnostics")
    @classmethod
    def _freeze_diagnostics(cls, value: Mapping[str, Any]) -> JsonObject:
        return freeze_json(value)

    @field_serializer("diagnostics")
    def _serialize_diagnostics(self, value: JsonObject) -> dict[str, Any]:
        return thaw_json(value)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind.value,
            "mode": self.mode.value,
            "state": self.state.value,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        if self.diagnostics:
            result["diagnostics"] = thaw_json(self.diagnostics)
        return result


def dependency_effective_readiness(
    conditions: Mapping[str, DependencyCondition],
) -> tuple[ReadinessState, tuple[str, ...], Mapping[str, object]]:
    diagnostics = {
        name: condition.to_dict()
        for name, condition in sorted(conditions.items())
        if condition.state != DependencyConditionState.SATISFIED
        or condition.mode != DependencyMode.REQUIRED
    }
    blocking = [
        condition
        for condition in conditions.values()
        if condition.mode == DependencyMode.REQUIRED
        and condition.state
        in {
            DependencyConditionState.UNKNOWN,
            DependencyConditionState.DEGRADED,
            DependencyConditionState.UNSATISFIED,
        }
    ]
    if not blocking:
        return ReadinessState.READY, (), {"dependencies": diagnostics}
    reasons = tuple(
        f"dependency.{condition.name}.{condition.state.value}"
        for condition in sorted(blocking, key=lambda item: item.name)
    )
    return ReadinessState.UNREADY, reasons, {"dependencies": diagnostics}


def dependency_from_mapping(
    name: str,
    source: Mapping[str, Any],
    *,
    field_name: str,
) -> ComponentDependency:
    unknown = sorted(set(source) - {"kind", "mode", "lane", "endpoint", "namespace"})
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"Unknown dependency field(s) in {field_name}: {names}")
    kind = _enum_value(
        DependencyKind,
        source.get("kind"),
        field_name=f"{field_name}.kind",
    )
    mode = _enum_value(
        DependencyMode,
        source.get("mode"),
        field_name=f"{field_name}.mode",
    )
    endpoint_source = source.get("endpoint")
    if not isinstance(endpoint_source, str) or not endpoint_source.strip():
        raise ValueError(f"{field_name}.endpoint must be a non-empty string")
    endpoint = parse_endpoint_address(endpoint_source.strip())

    if kind == DependencyKind.ENDPOINT:
        lane = source.get("lane")
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError(f"{field_name}.lane must be a non-empty string")
        if source.get("namespace") is not None:
            raise ValueError(f"{field_name}.namespace is only valid for service")
        return ComponentDependency(
            name=name,
            kind=kind,
            mode=mode,
            endpoint=endpoint,
            lane=lane.strip(),
        )

    namespace = source.get("namespace")
    if endpoint.family != "service":
        raise ValueError(f"{field_name}.endpoint must use service:<service-id>")
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError(f"{field_name}.namespace must be a non-empty string")
    if source.get("lane") is not None:
        raise ValueError(f"{field_name}.lane is implied for service dependencies")
    return ComponentDependency(
        name=name,
        kind=kind,
        mode=mode,
        endpoint=endpoint,
        lane="services",
        namespace=namespace.strip(),
    )


def _enum_value(enum_type, value: Any, *, field_name: str):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of {allowed}") from exc


def _non_empty_string(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    return value
