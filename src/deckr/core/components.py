from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

import anyio

from deckr.core.component import BaseComponent, Component, ComponentManager
from deckr.core.config import ConfigDocument
from deckr.transports.bus import EventBus

COMPONENT_ENTRYPOINT_GROUP = "deckr.components"
CORE_LANE_NAMES = ("hardware_events", "plugin_messages")


class ComponentCardinality(StrEnum):
    SINGLETON = "singleton"
    MULTI_INSTANCE = "multi_instance"


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    component_id: str
    config_prefix: str
    consumes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    cardinality: ComponentCardinality = ComponentCardinality.SINGLETON


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
    raw_config: Mapping[str, Any]
    base_dir: Path
    lanes: LaneRegistry

    def require_lane(self, name: str) -> EventBus:
        return self.lanes.require(name)


class ComponentFactory(Protocol):
    def __call__(self, context: ComponentContext) -> Component: ...


class LaneResolver(Protocol):
    def __call__(
        self,
        *,
        manifest: ComponentManifest,
        raw_config: Mapping[str, Any],
        instance_id: str,
    ) -> ResolvedLaneSet: ...


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    manifest: ComponentManifest
    factory: ComponentFactory
    resolve_lanes: LaneResolver | None = None

    def lanes_for(
        self,
        *,
        raw_config: Mapping[str, Any],
        instance_id: str,
    ) -> ResolvedLaneSet:
        if self.resolve_lanes is not None:
            return self.resolve_lanes(
                manifest=self.manifest,
                raw_config=raw_config,
                instance_id=instance_id,
            )
        return ResolvedLaneSet(
            consumes=self.manifest.consumes,
            publishes=self.manifest.publishes,
        )


@dataclass(frozen=True, slots=True)
class ComponentInstanceSpec:
    component_id: str
    instance_id: str
    runtime_name: str
    raw_config: Mapping[str, Any]
    definition: ComponentDefinition
    lanes: ResolvedLaneSet


@dataclass(frozen=True, slots=True)
class ComponentActivationResult:
    components: tuple[Component, ...]
    lane_names: tuple[str, ...]
    lanes: LaneRegistry

    def get_lane(self, name: str) -> EventBus | None:
        return self.lanes.get(name)


class LaneRegistry:
    def __init__(self, buses: Mapping[str, EventBus]) -> None:
        self._buses = dict(buses)

    @classmethod
    def from_names(cls, lane_names: set[str] | frozenset[str]) -> LaneRegistry:
        names = set(CORE_LANE_NAMES)
        names.update(lane_names)
        return cls({name: EventBus() for name in sorted(names)})

    def get(self, name: str) -> EventBus | None:
        return self._buses.get(name)

    def require(self, name: str) -> EventBus:
        bus = self.get(name)
        if bus is None:
            raise LookupError(f"Required lane {name!r} is not available")
        return bus

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._buses))


class InactiveComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


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


def _singleton_spec(
    document: ConfigDocument,
    definition: ComponentDefinition,
) -> ComponentInstanceSpec | None:
    raw_config = document.namespace(definition.manifest.config_prefix)
    if raw_config is None:
        return None
    instance_id = "default"
    return ComponentInstanceSpec(
        component_id=definition.manifest.component_id,
        instance_id=instance_id,
        runtime_name=runtime_name_for(definition.manifest.component_id, instance_id),
        raw_config=raw_config,
        definition=definition,
        lanes=definition.lanes_for(raw_config=raw_config, instance_id=instance_id),
    )


def _multi_instance_specs(
    document: ConfigDocument,
    definition: ComponentDefinition,
) -> list[ComponentInstanceSpec]:
    instances_path = f"{definition.manifest.config_prefix}.instances"
    instances = document.children(instances_path)
    specs: list[ComponentInstanceSpec] = []
    for instance_id, raw_config in sorted(instances.items()):
        specs.append(
            ComponentInstanceSpec(
                component_id=definition.manifest.component_id,
                instance_id=instance_id,
                runtime_name=runtime_name_for(
                    definition.manifest.component_id,
                    instance_id,
                ),
                raw_config=raw_config,
                definition=definition,
                lanes=definition.lanes_for(
                    raw_config=raw_config,
                    instance_id=instance_id,
                ),
            )
        )
    return specs


def resolve_component_instance_specs(
    document: ConfigDocument,
    *,
    discovered_component_ids: list[str] | tuple[str, ...],
) -> list[ComponentInstanceSpec]:
    specs: list[ComponentInstanceSpec] = []
    for component_id in sorted(set(discovered_component_ids)):
        definition = load_component_definition(component_id)
        if definition is None:
            continue
        if definition.manifest.cardinality == ComponentCardinality.SINGLETON:
            spec = _singleton_spec(document, definition)
            if spec is not None:
                specs.append(spec)
            continue
        specs.extend(_multi_instance_specs(document, definition))
    return specs


def configured_component_instance_specs(
    document: ConfigDocument,
) -> list[ComponentInstanceSpec]:
    return resolve_component_instance_specs(
        document,
        discovered_component_ids=available_component_ids(),
    )


def build_lane_registry(
    instance_specs: list[ComponentInstanceSpec],
) -> LaneRegistry:
    lane_names: set[str] = set()
    for spec in instance_specs:
        lane_names.update(spec.lanes.consumes)
        lane_names.update(spec.lanes.publishes)
    return LaneRegistry.from_names(lane_names)


async def activate_components(
    document: ConfigDocument,
    component_manager: ComponentManager,
) -> ComponentActivationResult:
    specs = configured_component_instance_specs(document)
    lanes = build_lane_registry(specs)

    created: list[Component] = []
    for spec in specs:
        context = ComponentContext(
            component_id=spec.component_id,
            instance_id=spec.instance_id,
            runtime_name=spec.runtime_name,
            manifest=spec.definition.manifest,
            raw_config=spec.raw_config,
            base_dir=document.base_dir,
            lanes=lanes,
        )
        component = spec.definition.factory(context)
        if not isinstance(component, Component):
            raise TypeError(
                f"Component {spec.component_id!r} did not return a Component"
            )
        if isinstance(component, InactiveComponent):
            continue
        created.append(component)

    for component in created:
        await component_manager.add_component(component)

    return ComponentActivationResult(
        components=tuple(created),
        lane_names=lanes.names,
        lanes=lanes,
    )


async def run_components(
    document: ConfigDocument,
) -> None:
    component_manager = ComponentManager()
    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await activate_components(
            document,
            component_manager,
        )
        await anyio.sleep_forever()
