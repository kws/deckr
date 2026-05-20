"""Generic component dependency declarations and readiness conditions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from deckr.components._defs import ReadinessState
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address


class DependencyKind(StrEnum):
    FEATURE = "feature"


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


@dataclass(frozen=True, slots=True)
class ComponentDependency:
    name: str
    kind: DependencyKind
    mode: DependencyMode
    feature_id: str
    endpoint: EndpointAddress | None = None


@dataclass(frozen=True, slots=True)
class DependencyCondition:
    name: str
    kind: DependencyKind
    mode: DependencyMode
    state: DependencyConditionState
    reason: str | None = None
    diagnostics: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind.value,
            "mode": self.mode.value,
            "state": self.state.value,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        if self.diagnostics:
            result["diagnostics"] = dict(self.diagnostics)
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
    unknown = sorted(set(source) - {"kind", "mode", "feature_id", "endpoint"})
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
    if kind != DependencyKind.FEATURE:
        raise ValueError(f"{field_name}.kind must be 'feature'")
    feature_id = source.get("feature_id")
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise ValueError(f"{field_name}.feature_id must be a non-empty string")
    endpoint_source = source.get("endpoint")
    endpoint = None
    if endpoint_source is not None:
        if not isinstance(endpoint_source, str) or not endpoint_source.strip():
            raise ValueError(f"{field_name}.endpoint must be a non-empty string")
        endpoint = parse_endpoint_address(endpoint_source.strip())
    return ComponentDependency(
        name=name,
        kind=kind,
        mode=mode,
        feature_id=feature_id.strip(),
        endpoint=endpoint,
    )


def _enum_value(enum_type, value: Any, *, field_name: str):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of {allowed}") from exc
