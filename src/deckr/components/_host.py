from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio

from deckr.beacon import (
    BeaconFeatureEventType,
    Candidate,
)
from deckr.components._defs import Component
from deckr.components._runner import ComponentManager
from deckr.components.dependencies import (
    ComponentDependency,
    DependencyCondition,
    DependencyConditionState,
    dependency_effective_readiness,
    dependency_from_mapping,
)
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    BackpressureHandling,
    DeliveryGuarantee,
    DeliveryOrdering,
    DeliveryPersistence,
    DeliveryReplay,
    DeliverySemantics,
    ExpiryHandling,
    IdempotencySemantics,
    LaneContract,
    LaneContractRegistry,
    MalformedMessageHandling,
    MessageFamily,
    MessageFamilyDelivery,
)
from deckr.contracts.messages import CORE_LANE_NAMES
from deckr.core.config import ConfigDocument
from deckr.core.util.runtime_id import require_runtime_id
from deckr.lanes import Lane, LaneRegistry
from deckr.state import (
    DEFAULT_STATE_STORE_NAME,
    StateStore,
    StateStorePolicy,
)
from deckr.substrates.nats_kv import KvBucketPolicy

if TYPE_CHECKING:
    from deckr.runtime import Deckr

COMPONENT_ENTRYPOINT_GROUP = "deckr.components"
COMPONENT_INSTANCE_SOURCE_ENTRYPOINT_GROUP = "deckr.component_instance_sources"
REMOVED_LANE_CONTRACT_FIELDS = frozenset(
    {
        "mqtt",
        "remote_endpoints",
        "route_policy",
        "route_table",
        "routes",
        "transport_route",
        "websocket",
    }
)
DELIVERY_CONTRACT_FIELDS = frozenset(
    {
        "persistence",
        "guarantee",
        "replay",
        "ordering",
        "ordering_keys",
        "expiry",
        "local_backpressure",
        "remote_backpressure",
        "malformed_messages",
        "message_families",
    }
)
REMOVED_DELIVERY_CONTRACT_FIELDS = REMOVED_LANE_CONTRACT_FIELDS | frozenset(
    {"durability"}
)


class ComponentCardinality(StrEnum):
    SINGLETON = "singleton"
    MULTI_INSTANCE = "multi_instance"


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    component_id: str
    consumes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    cardinality: ComponentCardinality = ComponentCardinality.SINGLETON
    lane_contracts: tuple[LaneContract, ...] = ()
    endpoint_slots: tuple[str, ...] = ()
    role: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLaneSet:
    consumes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ComponentContext:
    component_id: str
    instance_id: str
    runtime_name: str
    manifest: ComponentManifest
    config: Mapping[str, Any]
    endpoints: Mapping[str, str]
    base_dir: Path
    lanes: LaneRegistry
    state_for: Callable[..., StateStore]
    kv_bucket_for: Callable[[KvBucketPolicy], Any] | None = None

    def require_lane(self, name: str) -> Lane:
        return self.lanes.require(name)

    def require_endpoint_id(self, slot: str) -> str:
        endpoint_id = self.endpoints.get(slot)
        if endpoint_id is None:
            raise KeyError(
                f"Component {self.runtime_name!r} has no endpoint slot {slot!r}"
            )
        return endpoint_id

    def state(
        self,
        name: str = DEFAULT_STATE_STORE_NAME,
        *,
        policy: StateStorePolicy | None = None,
    ) -> StateStore:
        return self.state_for(name, policy=policy)

    def kv_bucket(self, policy: KvBucketPolicy) -> Any:
        if self.kv_bucket_for is None:
            raise RuntimeError("Deckr component context does not provide KV buckets")
        return self.kv_bucket_for(policy)


class ComponentFactory(Protocol):
    def __call__(self, context: ComponentContext) -> Component: ...


class LaneResolver(Protocol):
    def __call__(
        self,
        *,
        manifest: ComponentManifest,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
    ) -> ResolvedLaneSet: ...


class LaneBindingValidator(Protocol):
    def __call__(
        self,
        *,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
        lane_contracts: LaneContractRegistry,
    ) -> None: ...


class ComponentConfigValidator(Protocol):
    def __call__(
        self,
        *,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
    ) -> None: ...


class ComponentInstanceSourceReporter(Protocol):
    def __call__(
        self,
        message: str,
        *,
        component_id: str | None = None,
        instance_id: str | None = None,
    ) -> None: ...


def _noop_component_instance_source_reporter(
    message: str,
    *,
    component_id: str | None = None,
    instance_id: str | None = None,
) -> None:
    del message, component_id, instance_id


@dataclass(frozen=True, slots=True)
class ComponentInstanceDefinition:
    component_id: str
    instance_id: str
    config: Mapping[str, Any] = field(default_factory=dict)
    endpoints: Mapping[str, str] = field(default_factory=dict)
    dependencies: Mapping[str, ComponentDependency] = field(default_factory=dict)
    runtime_name: str | None = None
    config_address: str | None = None
    generated_by: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentInstanceSourceContext:
    source_id: str
    source_config: Mapping[str, Any]
    resolved_config: Mapping[str, Any]
    base_dir: Path
    report: ComponentInstanceSourceReporter = field(
        default=_noop_component_instance_source_reporter,
        repr=False,
        compare=False,
    )


class ComponentInstanceSourceLoader(Protocol):
    def __call__(
        self,
        context: ComponentInstanceSourceContext,
    ) -> Sequence[ComponentInstanceDefinition]: ...


@dataclass(frozen=True, slots=True)
class ComponentInstanceSourceDefinition:
    source_id: str
    load: ComponentInstanceSourceLoader


@dataclass(frozen=True, slots=True)
class PlanningEvent:
    message: str
    source_id: str | None = None
    component_id: str | None = None
    instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentPlanningReport:
    events: tuple[PlanningEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    manifest: ComponentManifest
    factory: ComponentFactory
    resolve_lanes: LaneResolver | None = None
    validate_lane_bindings: LaneBindingValidator | None = None
    validate_config: ComponentConfigValidator | None = None

    def lanes_for(
        self,
        *,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
    ) -> ResolvedLaneSet:
        if self.resolve_lanes is not None:
            return self.resolve_lanes(
                manifest=self.manifest,
                config=config,
                endpoints=endpoints,
                instance_id=instance_id,
            )
        return ResolvedLaneSet(
            consumes=self.manifest.consumes,
            publishes=self.manifest.publishes,
        )

    def validate_resolved_lane_bindings(
        self,
        *,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
        lane_contracts: LaneContractRegistry,
    ) -> None:
        if self.validate_lane_bindings is None:
            return
        self.validate_lane_bindings(
            config=config,
            endpoints=endpoints,
            instance_id=instance_id,
            lane_contracts=lane_contracts,
        )

    def validate_instance_config(
        self,
        *,
        config: Mapping[str, Any],
        endpoints: Mapping[str, str],
        instance_id: str,
    ) -> None:
        if self.validate_config is None:
            return
        self.validate_config(
            config=config,
            endpoints=endpoints,
            instance_id=instance_id,
        )


ComponentDefinitions = Mapping[str, ComponentDefinition] | Sequence[ComponentDefinition]
ComponentInstanceSourceDefinitions = (
    Mapping[str, ComponentInstanceSourceDefinition]
    | Sequence[ComponentInstanceSourceDefinition]
)


@dataclass(frozen=True, slots=True)
class ComponentInstanceSpec:
    component_id: str
    instance_id: str
    runtime_name: str
    config: Mapping[str, Any]
    endpoints: Mapping[str, str]
    dependencies: Mapping[str, ComponentDependency]
    definition: ComponentDefinition
    lanes: ResolvedLaneSet
    config_address: str | None = None
    generated_by: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentHostPlan:
    specs: tuple[ComponentInstanceSpec, ...]
    lane_contracts: LaneContractRegistry
    lane_names: tuple[str, ...]
    base_dir: Path
    report: ComponentPlanningReport = field(default_factory=ComponentPlanningReport)

    @classmethod
    def from_specs(
        cls,
        specs: Sequence[ComponentInstanceSpec],
        *,
        base_dir: Path | None = None,
        lane_contracts: LaneContractRegistry | Sequence[LaneContract] | None = None,
    ) -> ComponentHostPlan:
        normalized_specs = tuple(specs)
        registry = _build_lane_contract_registry(
            normalized_specs,
            extra_contracts=lane_contracts,
        )
        _validate_component_lane_bindings(normalized_specs, registry)
        return cls(
            specs=normalized_specs,
            lane_contracts=registry,
            lane_names=_lane_names_for_specs(
                normalized_specs,
                lane_contracts=registry,
            ),
            base_dir=base_dir or Path.cwd(),
        )

    @property
    def components(self) -> tuple[ComponentInstanceSpec, ...]:
        return self.specs


@dataclass(frozen=True, slots=True)
class ComponentHost:
    component_manager: ComponentManager
    components: tuple[Component, ...]
    lane_names: tuple[str, ...]
    lanes: LaneRegistry

    def get_lane(self, name: str) -> Lane | None:
        return self.lanes.get(name)

    async def stop(self) -> None:
        await self.component_manager.stop()


def runtime_name_for(component_id: str, instance_id: str) -> str:
    if instance_id == "default":
        return component_id
    return f"{component_id}:{instance_id}"


def available_component_ids() -> list[str]:
    return sorted(
        entry_point.name
        for entry_point in entry_points().select(group=COMPONENT_ENTRYPOINT_GROUP)
    )


def load_component_definition(component_id: str) -> ComponentDefinition | None:
    for entry_point in entry_points().select(group=COMPONENT_ENTRYPOINT_GROUP):
        if entry_point.name != component_id:
            continue
        definition = entry_point.load()
        if not isinstance(definition, ComponentDefinition):
            raise TypeError(
                f"Entry point {component_id!r} did not load a ComponentDefinition"
            )
        return definition
    return None


def available_component_instance_source_ids() -> list[str]:
    return sorted(
        entry_point.name
        for entry_point in entry_points().select(
            group=COMPONENT_INSTANCE_SOURCE_ENTRYPOINT_GROUP
        )
    )


def load_component_instance_source_definition(
    source_id: str,
) -> ComponentInstanceSourceDefinition | None:
    for entry_point in entry_points().select(
        group=COMPONENT_INSTANCE_SOURCE_ENTRYPOINT_GROUP
    ):
        if entry_point.name != source_id:
            continue
        definition = entry_point.load()
        if not isinstance(definition, ComponentInstanceSourceDefinition):
            raise TypeError(
                f"Entry point {source_id!r} did not load a "
                "ComponentInstanceSourceDefinition"
            )
        if definition.source_id != source_id:
            raise ValueError(
                f"Component instance source entry point {source_id!r} loaded "
                f"source {definition.source_id!r}"
            )
        return definition
    return None


def build_lane_contract_registry(
    instance_specs: Sequence[ComponentInstanceSpec],
    document: ConfigDocument,
) -> LaneContractRegistry:
    return _build_lane_contract_registry(instance_specs, document=document)


def resolve_component_host_plan(
    document: ConfigDocument,
    *,
    definitions: ComponentDefinitions | None = None,
    instance_source_definitions: ComponentInstanceSourceDefinitions | None = None,
) -> ComponentHostPlan:
    report_events: list[PlanningEvent] = []
    instance_defs = list(
        _configured_component_instance_definitions(document, report_events=report_events)
    )
    instance_defs.extend(
        _generated_component_instance_definitions(
            document,
            instance_source_definitions=instance_source_definitions,
            report_events=report_events,
        )
    )
    specs = tuple(
        _specs_from_instance_definitions(
            instance_defs,
            document=document,
            definitions=definitions,
            report_events=report_events,
        )
    )
    lane_contracts = _build_lane_contract_registry(specs, document=document)
    _validate_component_lane_bindings(specs, lane_contracts)
    _report_dependency_declarations(specs, report_events)
    _report_endpoint_dependency_cycles(specs, report_events)
    return ComponentHostPlan(
        specs=specs,
        lane_contracts=lane_contracts,
        lane_names=_lane_names_for_specs(specs, lane_contracts=lane_contracts),
        base_dir=document.base_dir,
        report=ComponentPlanningReport(events=tuple(report_events)),
    )


def resolve_component_instance_specs(
    document: ConfigDocument,
    *,
    definitions: ComponentDefinitions | None = None,
    instance_source_definitions: ComponentInstanceSourceDefinitions | None = None,
) -> list[ComponentInstanceSpec]:
    return list(
        resolve_component_host_plan(
            document,
            definitions=definitions,
            instance_source_definitions=instance_source_definitions,
        ).specs
    )


def configured_component_instance_specs(
    document: ConfigDocument,
    *,
    definitions: ComponentDefinitions | None = None,
    instance_source_definitions: ComponentInstanceSourceDefinitions | None = None,
) -> list[ComponentInstanceSpec]:
    return resolve_component_instance_specs(
        document,
        definitions=definitions,
        instance_source_definitions=instance_source_definitions,
    )


@asynccontextmanager
async def start_components(
    deckr: Deckr,
    plan: ComponentHostPlan,
) -> AsyncIterator[ComponentHost]:
    _validate_runtime_for_plan(deckr, plan)
    component_manager = ComponentManager()
    async with anyio.create_task_group() as tg:
        await tg.start(component_manager.run)
        host = await _activate_component_plan(deckr, plan, component_manager)
        if any(spec.dependencies for spec in plan.specs):
            tg.start_soon(
                _run_dependency_observer,
                deckr,
                plan,
                component_manager,
                name="deckr.component-dependencies",
            )
        try:
            yield host
        finally:
            await host.stop()
            tg.cancel_scope.cancel()


GENERIC_INSTANCE_FIELDS = frozenset(
    {"component", "instance_id", "runtime_name", "endpoints", "config", "dependencies"}
)


def _configured_component_instance_definitions(
    document: ConfigDocument,
    *,
    report_events: list[PlanningEvent],
) -> list[ComponentInstanceDefinition]:
    instances = document.children("deckr.components.instances")
    definitions: list[ComponentInstanceDefinition] = []
    for config_name, source in instances.items():
        definitions.append(
            _component_instance_definition_from_mapping(
                source,
                config_address=f"deckr.components.instances.{config_name}",
                generated_by=None,
            )
        )
        report_events.append(
            PlanningEvent(
                message=f"configured component instance {config_name}",
                instance_id=definitions[-1].instance_id,
                component_id=definitions[-1].component_id,
            )
        )
    return definitions


def _component_instance_definition_from_mapping(
    source: Mapping[str, Any],
    *,
    config_address: str | None,
    generated_by: str | None,
) -> ComponentInstanceDefinition:
    unknown = sorted(set(source) - GENERIC_INSTANCE_FIELDS)
    if unknown:
        names = ", ".join(unknown)
        location = f" in {config_address}" if config_address else ""
        raise ValueError(f"Unknown component instance field(s){location}: {names}")

    component_id = source.get("component")
    if not isinstance(component_id, str) or not component_id.strip():
        raise ValueError(f"{config_address or 'Component instance'}.component required")
    instance_id = require_runtime_id(
        str(source.get("instance_id", "")).strip(),
        label="Component instance ID",
        source_hint=f"Set `{config_address}.instance_id`.",
    )
    runtime_name_source = source.get("runtime_name")
    runtime_name = None
    if runtime_name_source is not None:
        runtime_name = require_runtime_id(
            str(runtime_name_source).strip(),
            label="Component runtime name",
            source_hint=f"Set `{config_address}.runtime_name`.",
        )

    endpoints_source = source.get("endpoints")
    endpoints: dict[str, str] = {}
    if endpoints_source is not None:
        if not isinstance(endpoints_source, Mapping):
            raise ValueError(f"{config_address}.endpoints must be a table")
        for slot, endpoint_id in endpoints_source.items():
            if not isinstance(slot, str) or not slot.strip():
                raise ValueError(f"{config_address}.endpoints keys must be strings")
            if not isinstance(endpoint_id, str) or not endpoint_id.strip():
                raise ValueError(
                    f"{config_address}.endpoints.{slot} must be a non-empty string"
                )
            endpoints[slot.strip()] = require_runtime_id(
                endpoint_id.strip(),
                label=f"Endpoint ID for {slot}",
                source_hint=f"Set `{config_address}.endpoints.{slot}`.",
            )

    config = source.get("config", {})
    if not isinstance(config, Mapping):
        raise ValueError(f"{config_address}.config must be a table")

    dependencies_source = source.get("dependencies", {})
    dependencies: dict[str, ComponentDependency] = {}
    if not isinstance(dependencies_source, Mapping):
        raise ValueError(f"{config_address}.dependencies must be a table")
    for dependency_name, dependency_source in dependencies_source.items():
        if not isinstance(dependency_name, str) or not dependency_name.strip():
            raise ValueError(f"{config_address}.dependencies keys must be strings")
        if not isinstance(dependency_source, Mapping):
            raise ValueError(
                f"{config_address}.dependencies.{dependency_name} must be a table"
            )
        normalized_name = dependency_name.strip()
        if normalized_name in dependencies:
            raise ValueError(
                f"Duplicate dependency name in {config_address}: {normalized_name}"
            )
        dependencies[normalized_name] = dependency_from_mapping(
            normalized_name,
            dependency_source,
            field_name=f"{config_address}.dependencies.{dependency_name}",
        )

    return ComponentInstanceDefinition(
        component_id=component_id.strip(),
        instance_id=instance_id,
        runtime_name=runtime_name,
        config=dict(config),
        endpoints=endpoints,
        dependencies=dependencies,
        config_address=config_address,
        generated_by=generated_by,
    )


def _source_declarations(document: ConfigDocument) -> tuple[Mapping[str, Any], ...]:
    components = document.namespace("deckr.components")
    if components is None:
        return ()
    source = components.get("instance_sources")
    if source is None:
        return ()
    if isinstance(source, str) or not isinstance(source, Sequence):
        raise ValueError("deckr.components.instance_sources must be an array of tables")
    declarations: list[Mapping[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"deckr.components.instance_sources[{index}] must be a table"
            )
        declarations.append(item)
    return tuple(declarations)


def _instance_source_ids(source: Mapping[str, Any], *, index: int) -> tuple[str, str]:
    declaration_id = source.get("id")
    source_id = source.get("source")
    if not isinstance(declaration_id, str) or not declaration_id.strip():
        raise ValueError(
            f"deckr.components.instance_sources[{index}].id must be a non-empty string"
        )
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(
            "deckr.components.instance_sources"
            f"[{index}].source must be a non-empty string"
        )
    return declaration_id.strip(), source_id.strip()


def _component_instance_source_reporter(
    source_id: str,
    report_events: list[PlanningEvent],
) -> ComponentInstanceSourceReporter:
    def report(
        message: str,
        *,
        component_id: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        report_events.append(
            PlanningEvent(
                source_id=source_id,
                component_id=component_id,
                instance_id=instance_id,
                message=message,
            )
        )

    return report


def _generated_component_instance_definitions(
    document: ConfigDocument,
    *,
    instance_source_definitions: ComponentInstanceSourceDefinitions | None,
    report_events: list[PlanningEvent],
) -> list[ComponentInstanceDefinition]:
    source_definitions = (
        _instance_source_definition_mapping(instance_source_definitions)
        if instance_source_definitions is not None
        else None
    )
    generated: list[ComponentInstanceDefinition] = []
    seen_declaration_ids: set[str] = set()
    for index, source in enumerate(_source_declarations(document)):
        declaration_id, source_id = _instance_source_ids(source, index=index)
        if declaration_id in seen_declaration_ids:
            raise ValueError(
                "Duplicate Deckr component instance source declaration id: "
                f"{declaration_id}"
            )
        seen_declaration_ids.add(declaration_id)
        definition = (
            source_definitions.get(source_id)
            if source_definitions is not None
            else load_component_instance_source_definition(source_id)
        )
        if definition is None:
            raise ValueError(f"Unknown Deckr component instance source: {source_id}")

        context = ComponentInstanceSourceContext(
            source_id=source_id,
            source_config=source,
            resolved_config=document.raw,
            base_dir=document.base_dir,
            report=_component_instance_source_reporter(source_id, report_events),
        )
        output = tuple(definition.load(context))
        report_events.append(
            PlanningEvent(source_id=source_id, message=f"ran instance source {declaration_id}")
        )
        for item in output:
            if not isinstance(item, ComponentInstanceDefinition):
                raise TypeError(
                    f"Component instance source {source_id!r} returned "
                    "a non-ComponentInstanceDefinition"
                )
            generated.append(
                ComponentInstanceDefinition(
                    component_id=item.component_id,
                    instance_id=item.instance_id,
                    config=dict(item.config),
                    endpoints=dict(item.endpoints),
                    dependencies=dict(item.dependencies),
                    runtime_name=item.runtime_name,
                    config_address=item.config_address,
                    generated_by=source_id,
                )
            )
            report_events.append(
                PlanningEvent(
                    source_id=source_id,
                    component_id=item.component_id,
                    instance_id=item.instance_id,
                    message="generated component instance",
                )
            )
    return generated


def _definition_mapping(definitions: ComponentDefinitions) -> dict[str, ComponentDefinition]:
    if isinstance(definitions, Mapping):
        return dict(definitions)
    return {definition.manifest.component_id: definition for definition in definitions}


def _instance_source_definition_mapping(
    definitions: ComponentInstanceSourceDefinitions,
) -> dict[str, ComponentInstanceSourceDefinition]:
    if isinstance(definitions, Mapping):
        return dict(definitions)
    return {definition.source_id: definition for definition in definitions}


def _component_definition_for(
    component_id: str,
    *,
    definitions: Mapping[str, ComponentDefinition] | None,
) -> ComponentDefinition:
    definition = (
        definitions.get(component_id)
        if definitions is not None
        else load_component_definition(component_id)
    )
    if definition is None:
        raise ValueError(f"Unknown Deckr component id: {component_id}")
    if definition.manifest.component_id != component_id:
        raise ValueError(
            f"Component definition for {component_id!r} declares "
            f"{definition.manifest.component_id!r}"
        )
    return definition


def _validate_instance_endpoints(
    instance: ComponentInstanceDefinition,
    definition: ComponentDefinition,
) -> None:
    allowed = set(definition.manifest.endpoint_slots)
    unknown = sorted(set(instance.endpoints) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(
            f"Component instance {instance.instance_id!r} for "
            f"{instance.component_id!r} uses unknown endpoint slot(s): {names}"
        )
    missing = sorted(allowed - set(instance.endpoints))
    if missing:
        names = ", ".join(missing)
        raise ValueError(
            f"Component instance {instance.instance_id!r} for "
            f"{instance.component_id!r} is missing endpoint slot(s): {names}"
        )


def _specs_from_instance_definitions(
    instances: Sequence[ComponentInstanceDefinition],
    *,
    document: ConfigDocument,
    definitions: ComponentDefinitions | None,
    report_events: list[PlanningEvent],
) -> list[ComponentInstanceSpec]:
    definition_map = _definition_mapping(definitions) if definitions is not None else None
    seen_instance_ids: set[str] = set()
    seen_runtime_names: set[str] = set()
    seen_endpoints: dict[tuple[str, str], str] = {}
    component_counts: dict[str, int] = {}
    specs: list[ComponentInstanceSpec] = []

    for instance in instances:
        if instance.instance_id in seen_instance_ids:
            raise ValueError(
                f"Duplicate Deckr component instance id: {instance.instance_id}"
            )
        seen_instance_ids.add(instance.instance_id)
        definition = _component_definition_for(
            instance.component_id,
            definitions=definition_map,
        )
        runtime_name = instance.runtime_name or runtime_name_for(
            instance.component_id,
            instance.instance_id,
        )
        if runtime_name in seen_runtime_names:
            raise ValueError(f"Duplicate Deckr component runtime name: {runtime_name}")
        seen_runtime_names.add(runtime_name)
        component_counts[instance.component_id] = (
            component_counts.get(instance.component_id, 0) + 1
        )
        _validate_instance_endpoints(instance, definition)
        for family, endpoint_id in instance.endpoints.items():
            key = (family, endpoint_id)
            existing = seen_endpoints.get(key)
            if existing is not None:
                raise ValueError(
                    f"Duplicate Deckr endpoint id {endpoint_id!r} for family "
                    f"{family!r}: {existing!r} and {instance.instance_id!r}"
                )
            seen_endpoints[key] = instance.instance_id

        definition.validate_instance_config(
            config=instance.config,
            endpoints=instance.endpoints,
            instance_id=instance.instance_id,
        )
        specs.append(
            ComponentInstanceSpec(
                component_id=instance.component_id,
                instance_id=instance.instance_id,
                runtime_name=runtime_name,
                config=instance.config,
                endpoints=instance.endpoints,
                dependencies=instance.dependencies,
                definition=definition,
                lanes=definition.lanes_for(
                    config=instance.config,
                    endpoints=instance.endpoints,
                    instance_id=instance.instance_id,
                ),
                config_address=instance.config_address,
                generated_by=instance.generated_by,
            )
        )
        report_events.append(
            PlanningEvent(
                component_id=instance.component_id,
                instance_id=instance.instance_id,
                message="planned component instance",
            )
        )

    for component_id, count in sorted(component_counts.items()):
        if count <= 1:
            continue
        definition = _component_definition_for(component_id, definitions=definition_map)
        if definition.manifest.cardinality == ComponentCardinality.SINGLETON:
            raise ValueError(
                f"Component {component_id!r} has singleton cardinality but "
                f"{count} instances were planned"
            )
    return specs


def _report_dependency_declarations(
    specs: Sequence[ComponentInstanceSpec],
    report_events: list[PlanningEvent],
) -> None:
    for spec in specs:
        for dependency in sorted(spec.dependencies.values(), key=lambda item: item.name):
            report_events.append(
                PlanningEvent(
                    component_id=spec.component_id,
                    instance_id=spec.instance_id,
                    message=(
                        "declared "
                        f"{dependency.mode.value} {dependency.kind.value} "
                        f"dependency {dependency.name}"
                    ),
                )
            )


def _report_endpoint_dependency_cycles(
    specs: Sequence[ComponentInstanceSpec],
    report_events: list[PlanningEvent],
) -> None:
    endpoint_owners: dict[tuple[str, str], str] = {}
    for spec in specs:
        for family, endpoint_id in spec.endpoints.items():
            endpoint_owners[(family, endpoint_id)] = spec.instance_id

    graph: dict[str, set[str]] = {spec.instance_id: set() for spec in specs}
    for spec in specs:
        for dependency in spec.dependencies.values():
            if dependency.endpoint is None:
                continue
            target = endpoint_owners.get(
                (dependency.endpoint.family, dependency.endpoint.endpoint_id)
            )
            if target is not None and target != spec.instance_id:
                graph[spec.instance_id].add(target)

    reported: set[tuple[str, ...]] = set()

    def walk(start: str, current: str, path: tuple[str, ...]) -> None:
        for target in sorted(graph.get(current, ())):
            if target == start:
                cycle = path + (target,)
                canonical = _canonical_cycle(cycle)
                if canonical not in reported:
                    reported.add(canonical)
                    report_events.append(
                        PlanningEvent(
                            instance_id=start,
                            message=(
                                "endpoint-filtered feature dependency cycle: "
                                + " -> ".join(cycle)
                            ),
                        )
                    )
                continue
            if target in path:
                continue
            walk(start, target, path + (target,))

    for instance_id in sorted(graph):
        walk(instance_id, instance_id, (instance_id,))


def _canonical_cycle(cycle: tuple[str, ...]) -> tuple[str, ...]:
    body = cycle[:-1]
    rotations = tuple(body[index:] + body[:index] for index in range(len(body)))
    return min(rotations)


def _string_set(value: Any, *, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} must be a list of non-empty strings")
        items.append(item)
    return frozenset(items)


def _optional_string_set(value: Any, *, field_name: str) -> frozenset[str] | None:
    if value is None:
        return None
    return _string_set(value, field_name=field_name)


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _broadcast_targets(value: Any, *, field_name: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a table of scope = endpoint_family")
    targets: dict[str, str] = {}
    for scope, endpoint_family in value.items():
        if not isinstance(scope, str) or not scope:
            raise ValueError(f"{field_name} scopes must be non-empty strings")
        if not isinstance(endpoint_family, str) or not endpoint_family:
            raise ValueError(
                f"{field_name} endpoint families must be non-empty strings"
            )
        targets[scope] = endpoint_family
    return targets


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} must be a list of non-empty strings")
        items.append(item)
    return tuple(items)


def _enum_value(enum_type, value: Any, *, field_name: str):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of {allowed}") from exc


def _optional_enum_value(enum_type, value: Any, *, field_name: str):
    if value is None:
        return None
    return _enum_value(enum_type, value, field_name=field_name)


def _message_family_delivery_from_mapping(
    value: Any,
    *,
    field_name: str,
) -> MessageFamilyDelivery:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a table")
    family = _optional_enum_value(
        MessageFamily,
        value.get("family"),
        field_name=f"{field_name}.family",
    )
    if family is None:
        raise ValueError(f"{field_name}.family is required")
    idempotency = _optional_enum_value(
        IdempotencySemantics,
        value.get("idempotency"),
        field_name=f"{field_name}.idempotency",
    )
    return MessageFamilyDelivery(
        family=family,
        message_types=_string_set(
            value.get("message_types"),
            field_name=f"{field_name}.message_types",
        ),
        idempotency=idempotency,
        ordering_keys=_string_tuple(
            value.get("ordering_keys"),
            field_name=f"{field_name}.ordering_keys",
        ),
    )


def _message_family_deliveries(
    value: Any, *, field_name: str
) -> tuple[MessageFamilyDelivery, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a list of tables")
    return tuple(
        _message_family_delivery_from_mapping(
            item,
            field_name=f"{field_name}.{index}",
        )
        for index, item in enumerate(value)
    )


def _delivery_from_mapping(source: Any, *, lane: str) -> DeliverySemantics | None:
    if source is None:
        return None
    if isinstance(source, str):
        raise ValueError(
            f"Lane contract {lane!r} delivery must be a table, not a string"
        )
    if not isinstance(source, Mapping):
        raise ValueError(f"Lane contract {lane!r} delivery must be a table")
    removed = sorted(key for key in REMOVED_DELIVERY_CONTRACT_FIELDS if key in source)
    if removed:
        keys = ", ".join(f"delivery.{key}" for key in removed)
        raise ValueError(f"{keys} are not part of the v1 lane contract")
    unknown = sorted(set(source) - DELIVERY_CONTRACT_FIELDS)
    if unknown:
        keys = ", ".join(f"delivery.{key}" for key in unknown)
        raise ValueError(f"{keys} are not part of the v1 lane contract")
    return DeliverySemantics(
        persistence=(
            _optional_enum_value(
                DeliveryPersistence,
                source.get("persistence"),
                field_name="delivery.persistence",
            )
            or DeliveryPersistence.EPHEMERAL
        ),
        guarantee=(
            _optional_enum_value(
                DeliveryGuarantee,
                source.get("guarantee"),
                field_name="delivery.guarantee",
            )
            or DeliveryGuarantee.AT_MOST_ONCE
        ),
        replay=(
            _optional_enum_value(
                DeliveryReplay,
                source.get("replay"),
                field_name="delivery.replay",
            )
            or DeliveryReplay.NONE
        ),
        ordering=(
            _optional_enum_value(
                DeliveryOrdering,
                source.get("ordering"),
                field_name="delivery.ordering",
            )
            or DeliveryOrdering.LOCAL_OR_CONNECTION_FIFO
        ),
        ordering_keys=_string_tuple(
            source.get("ordering_keys"),
            field_name="delivery.ordering_keys",
        ),
        expiry=(
            _optional_enum_value(
                ExpiryHandling,
                source.get("expiry"),
                field_name="delivery.expiry",
            )
            or ExpiryHandling.DROP_AND_REPORT
        ),
        local_backpressure=(
            _optional_enum_value(
                BackpressureHandling,
                source.get("local_backpressure"),
                field_name="delivery.local_backpressure",
            )
            or BackpressureHandling.DROP_SUBSCRIBER
        ),
        remote_backpressure=(
            _optional_enum_value(
                BackpressureHandling,
                source.get("remote_backpressure"),
                field_name="delivery.remote_backpressure",
            )
            or BackpressureHandling.DISCONNECT
        ),
        malformed_messages=(
            _optional_enum_value(
                MalformedMessageHandling,
                source.get("malformed_messages"),
                field_name="delivery.malformed_messages",
            )
            or MalformedMessageHandling.DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION
        ),
        message_families=_message_family_deliveries(
            source.get("message_families"),
            field_name="delivery.message_families",
        ),
    )


def _lane_contract_from_mapping(lane: str, source: Mapping[str, Any]) -> LaneContract:
    schema_id = source.get("schema_id")
    if schema_id is not None and not isinstance(schema_id, str):
        raise ValueError(f"Lane contract {lane!r} schema_id must be a string")
    if source.get("delivery_semantics") is not None:
        raise ValueError(
            f"Lane contract {lane!r} delivery_semantics has been replaced by delivery"
        )
    removed = sorted(key for key in REMOVED_LANE_CONTRACT_FIELDS if key in source)
    if removed:
        keys = ", ".join(removed)
        raise ValueError(
            f"Lane contract {lane!r} removed field(s) {keys} are not part of "
            "the v1 lane contract; use direct NATS-backed lane contract fields"
        )
    return LaneContract(
        lane=lane,
        schema_id=schema_id,
        message_types=_string_set(
            source.get("message_types"),
            field_name=f"Lane contract {lane!r} message_types",
        ),
        delivery=_delivery_from_mapping(source.get("delivery"), lane=lane),
        allowed_sender_families=_optional_string_set(
            source.get("allowed_sender_families"),
            field_name="allowed_sender_families",
        ),
        allowed_recipient_families=_optional_string_set(
            source.get("allowed_recipient_families"),
            field_name="allowed_recipient_families",
        ),
        broadcast_targets=_broadcast_targets(
            source.get("broadcast_targets"),
            field_name="broadcast_targets",
        ),
        default_broadcast_hop_limit=_optional_int(
            source.get("default_broadcast_hop_limit"),
            field_name="default_broadcast_hop_limit",
        ),
    )


def _narrow_families(
    base: frozenset[str] | None,
    override: frozenset[str] | None,
    *,
    field_name: str,
    lane: str,
) -> frozenset[str] | None:
    if override is None:
        return base
    if base is not None and not override.issubset(base):
        raise ValueError(
            f"Deployment lane contract {lane!r} must not widen {field_name}"
        )
    return override


def _narrow_broadcast_targets(
    base: Mapping[str, str],
    override: Mapping[str, str],
    *,
    lane: str,
) -> Mapping[str, str]:
    if not override:
        return base
    for scope, endpoint_family in override.items():
        if base.get(scope) != endpoint_family:
            raise ValueError(
                f"Deployment lane contract {lane!r} must not widen broadcast_targets"
            )
    return override


def _narrow_default_broadcast_hop_limit(
    base: int | None,
    override: int | None,
    *,
    lane: str,
) -> int | None:
    if override is None:
        return base
    if base is not None and override > base:
        raise ValueError(
            f"Deployment lane contract {lane!r} must not widen default_broadcast_hop_limit"
        )
    return override


def _narrow_message_types(
    base: frozenset[str],
    override: frozenset[str],
    *,
    lane: str,
) -> frozenset[str]:
    if not override:
        return base
    if base and not override.issubset(base):
        raise ValueError(
            f"Deployment lane contract {lane!r} must not widen message_types"
        )
    return override


def _narrow_delivery(
    base: DeliverySemantics | None,
    override: DeliverySemantics | None,
    *,
    lane: str,
) -> DeliverySemantics | None:
    if override is None:
        return base
    if base is not None and override != base:
        raise ValueError(f"Deployment lane contract {lane!r} must not change delivery")
    return override


def _narrow_lane_contract(base: LaneContract, override: LaneContract) -> LaneContract:
    if override.schema_id is not None and base.schema_id not in {
        None,
        override.schema_id,
    }:
        raise ValueError(
            f"Deployment lane contract {base.lane!r} must not change schema_id"
        )
    return LaneContract(
        lane=base.lane,
        schema_id=base.schema_id or override.schema_id,
        message_types=_narrow_message_types(
            base.message_types,
            override.message_types,
            lane=base.lane,
        ),
        delivery=_narrow_delivery(base.delivery, override.delivery, lane=base.lane),
        allowed_sender_families=_narrow_families(
            base.allowed_sender_families,
            override.allowed_sender_families,
            field_name="allowed_sender_families",
            lane=base.lane,
        ),
        allowed_recipient_families=_narrow_families(
            base.allowed_recipient_families,
            override.allowed_recipient_families,
            field_name="allowed_recipient_families",
            lane=base.lane,
        ),
        broadcast_targets=_narrow_broadcast_targets(
            base.broadcast_targets,
            override.broadcast_targets,
            lane=base.lane,
        ),
        default_broadcast_hop_limit=_narrow_default_broadcast_hop_limit(
            base.default_broadcast_hop_limit,
            override.default_broadcast_hop_limit,
            lane=base.lane,
        ),
    )


def _build_lane_contract_registry(
    instance_specs: Sequence[ComponentInstanceSpec],
    *,
    document: ConfigDocument | None = None,
    extra_contracts: LaneContractRegistry | Sequence[LaneContract] | None = None,
) -> LaneContractRegistry:
    contracts: dict[str, LaneContract] = dict(CORE_LANE_CONTRACTS)
    if isinstance(extra_contracts, LaneContractRegistry):
        provided_contracts = tuple(extra_contracts.contracts.values())
    else:
        provided_contracts = tuple(extra_contracts or ())

    for contract in provided_contracts:
        if contract.lane in CORE_LANE_CONTRACTS:
            if CORE_LANE_CONTRACTS[contract.lane] != contract:
                raise ValueError(
                    f"Extension lane contract input must not override "
                    f"core lane contract {contract.lane!r}"
                )
            continue
        existing = contracts.get(contract.lane)
        if existing is not None and existing != contract:
            raise ValueError(
                f"Duplicate lane contract {contract.lane!r} declarations "
                "must be identical"
            )
        contracts[contract.lane] = existing or contract

    for spec in instance_specs:
        for contract in spec.definition.manifest.lane_contracts:
            if contract.lane in CORE_LANE_CONTRACTS:
                raise ValueError(
                    f"Component {spec.component_id!r} must not override "
                    f"core lane contract {contract.lane!r}"
                )
            existing = contracts.get(contract.lane)
            if existing is not None and existing != contract:
                raise ValueError(
                    f"Duplicate lane contract {contract.lane!r} declarations "
                    "must be identical"
                )
            contracts[contract.lane] = existing or contract

    if document is not None:
        for lane, source in sorted(document.children("deckr.lane_contracts").items()):
            if lane in CORE_LANE_CONTRACTS:
                raise ValueError(
                    f"Deployment config must not override core lane contract {lane!r}"
                )
            contract = _lane_contract_from_mapping(lane, source)
            existing = contracts.get(lane)
            contracts[lane] = (
                _narrow_lane_contract(existing, contract)
                if existing is not None
                else contract
            )
    return LaneContractRegistry(contracts.values())


def _lane_names_for_specs(
    instance_specs: Sequence[ComponentInstanceSpec],
    *,
    lane_contracts: LaneContractRegistry | None = None,
) -> tuple[str, ...]:
    lane_names: set[str] = set(CORE_LANE_NAMES)
    for spec in instance_specs:
        lane_names.update(spec.lanes.consumes)
        lane_names.update(spec.lanes.publishes)
    if lane_contracts is not None:
        lane_names.update(
            lane
            for lane in lane_contracts.contracts
            if lane not in CORE_LANE_CONTRACTS
        )
    return tuple(sorted(lane_names))


def _validate_component_lane_bindings(
    specs: Sequence[ComponentInstanceSpec],
    lane_contracts: LaneContractRegistry,
) -> None:
    for spec in specs:
        spec.definition.validate_resolved_lane_bindings(
            config=spec.config,
            endpoints=spec.endpoints,
            instance_id=spec.instance_id,
            lane_contracts=lane_contracts,
        )


def _validate_runtime_for_plan(deckr: Deckr, plan: ComponentHostPlan) -> None:
    if not deckr.is_running:
        raise RuntimeError("Deckr runtime must be entered before starting components")

    missing_lanes = sorted(set(plan.lane_names) - set(deckr.lanes.names))
    if missing_lanes:
        lanes = ", ".join(repr(lane) for lane in missing_lanes)
        raise LookupError(f"Deckr runtime is missing required lane(s): {lanes}")

    runtime_contracts = deckr.lane_contracts.contracts
    for lane, contract in sorted(plan.lane_contracts.contracts.items()):
        if runtime_contracts.get(lane) != contract:
            raise ValueError(
                f"Deckr runtime lane contract for {lane!r} does not match "
                "the component host plan"
            )


async def _activate_component_plan(
    deckr: Deckr,
    plan: ComponentHostPlan,
    component_manager: ComponentManager,
) -> ComponentHost:
    created: list[Component] = []
    for spec in plan.specs:
        context = ComponentContext(
            component_id=spec.component_id,
            instance_id=spec.instance_id,
            runtime_name=spec.runtime_name,
            manifest=spec.definition.manifest,
            config=spec.config,
            endpoints=spec.endpoints,
            base_dir=plan.base_dir,
            lanes=deckr.lanes,
            state_for=deckr.state,
            kv_bucket_for=getattr(deckr._substrate, "kv_bucket", None),  # noqa: SLF001
        )
        component = spec.definition.factory(context)
        if not isinstance(component, Component):
            raise TypeError(
                f"Component {spec.component_id!r} did not return a Component"
            )
        created.append(component)

    for component in created:
        await component_manager.add_component(component)

    return ComponentHost(
        component_manager=component_manager,
        components=tuple(created),
        lane_names=plan.lane_names,
        lanes=deckr.lanes,
    )


async def _run_dependency_observer(
    deckr: Deckr,
    plan: ComponentHostPlan,
    component_manager: ComponentManager,
) -> None:
    specs = tuple(spec for spec in plan.specs if spec.dependencies)
    beacon = deckr.beacon
    feature_ids = sorted(
        {
            dependency.feature_id
            for spec in specs
            for dependency in spec.dependencies.values()
        }
    )
    feature_snapshots: dict[str, dict[str, Candidate] | None] = {
        feature_id: {} for feature_id in feature_ids
    }
    send, receive = anyio.create_memory_object_stream[object](max_buffer_size=1)

    async def notify() -> None:
        try:
            send.send_nowait(object())
        except anyio.WouldBlock:
            pass

    async def watch_feature(feature_id: str) -> None:
        while True:
            async with beacon.watch(feature_id) as changes:
                feature_snapshots[feature_id] = {
                    candidate.key: candidate
                    for candidate in beacon.candidates(feature_id)
                }
                await notify()
                async for event in changes:
                    snapshot = feature_snapshots.get(feature_id)
                    if snapshot is None:
                        snapshot = {}
                        feature_snapshots[feature_id] = snapshot
                    if (
                        event.event_type
                        in {
                            BeaconFeatureEventType.ADVERTISED,
                            BeaconFeatureEventType.UPDATED,
                        }
                        and event.candidate is not None
                    ):
                        snapshot[event.key] = event.candidate
                    else:
                        snapshot.pop(event.key, None)
                    await notify()

    async with send, receive, anyio.create_task_group() as tg:
        for feature_id in feature_ids:
            tg.start_soon(watch_feature, feature_id)
        while True:
            await _evaluate_dependency_readiness(
                specs,
                feature_snapshots=feature_snapshots,
                component_manager=component_manager,
            )
            with anyio.move_on_after(0.25) as scope:
                await receive.receive()
            if scope.cancel_called:
                continue


async def _evaluate_dependency_readiness(
    specs: Sequence[ComponentInstanceSpec],
    *,
    feature_snapshots: Mapping[str, Mapping[str, Candidate] | None],
    component_manager: ComponentManager,
) -> None:
    for spec in specs:
        conditions: dict[str, DependencyCondition] = {}
        for dependency in spec.dependencies.values():
            conditions[dependency.name] = await _dependency_condition(
                dependency,
                feature_snapshots=feature_snapshots,
            )
        readiness, reasons, diagnostics = dependency_effective_readiness(conditions)
        await component_manager.report_component_dependency_readiness(
            spec.runtime_name,
            readiness,
            reasons=reasons,
            diagnostics=diagnostics,
        )


async def _dependency_condition(
    dependency: ComponentDependency,
    *,
    feature_snapshots: Mapping[str, Mapping[str, Candidate] | None],
) -> DependencyCondition:
    snapshot = feature_snapshots.get(dependency.feature_id)
    if snapshot is None:
        return DependencyCondition(
            name=dependency.name,
            kind=dependency.kind,
            mode=dependency.mode,
            state=DependencyConditionState.UNKNOWN,
            reason="state_unavailable",
        )
    candidates = tuple(
        sorted(
            (
                candidate
                for candidate in snapshot.values()
                if dependency.endpoint is None
                or candidate.advertisement.endpoint == dependency.endpoint
            ),
            key=lambda candidate: candidate.key,
        )
    )
    diagnostics = {
        "featureId": dependency.feature_id,
        **(
            {"endpoint": str(dependency.endpoint)}
            if dependency.endpoint is not None
            else {}
        ),
    }
    if not candidates:
        return DependencyCondition(
            name=dependency.name,
            kind=dependency.kind,
            mode=dependency.mode,
            state=DependencyConditionState.UNSATISFIED,
            reason="beacon_absent",
            diagnostics=diagnostics,
        )
    diagnostics["candidates"] = len(candidates)
    diagnostics["sessionId"] = candidates[0].advertisement.session_id
    return DependencyCondition(
        name=dependency.name,
        kind=dependency.kind,
        mode=dependency.mode,
        state=DependencyConditionState.SATISFIED,
        diagnostics=diagnostics,
    )
