from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio

from deckr.components._defs import BaseComponent, Component
from deckr.components._runner import ComponentManager
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    BackpressureHandling,
    DeliveryDurability,
    DeliveryGuarantee,
    DeliveryOrdering,
    DeliveryPersistence,
    DeliveryReplay,
    DeliverySemantics,
    ExpiryHandling,
    IdempotencySemantics,
    LaneContract,
    LaneContractRegistry,
    LaneRoutePolicy,
    MalformedMessageHandling,
    MessageFamily,
    MessageFamilyDelivery,
    MqttDeliveryConstraints,
)
from deckr.contracts.messages import CORE_LANE_NAMES
from deckr.core.config import ConfigDocument
from deckr.transports.bus import EventBus
from deckr.transports.routes import RouteTable

if TYPE_CHECKING:
    from deckr.runtime import Deckr

COMPONENT_ENTRYPOINT_GROUP = "deckr.components"


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
    lane_contracts: tuple[LaneContract, ...] = ()


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


class LaneBindingValidator(Protocol):
    def __call__(
        self,
        *,
        raw_config: Mapping[str, Any],
        instance_id: str,
        lane_contracts: LaneContractRegistry,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    manifest: ComponentManifest
    factory: ComponentFactory
    resolve_lanes: LaneResolver | None = None
    validate_lane_bindings: LaneBindingValidator | None = None

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

    def validate_resolved_lane_bindings(
        self,
        *,
        raw_config: Mapping[str, Any],
        instance_id: str,
        lane_contracts: LaneContractRegistry,
    ) -> None:
        if self.validate_lane_bindings is None:
            return
        self.validate_lane_bindings(
            raw_config=raw_config,
            instance_id=instance_id,
            lane_contracts=lane_contracts,
        )


ComponentDefinitions = Mapping[str, ComponentDefinition] | Sequence[ComponentDefinition]


@dataclass(frozen=True, slots=True)
class ComponentInstanceSpec:
    component_id: str
    instance_id: str
    runtime_name: str
    raw_config: Mapping[str, Any]
    definition: ComponentDefinition
    lanes: ResolvedLaneSet


@dataclass(frozen=True, slots=True)
class ComponentHostPlan:
    specs: tuple[ComponentInstanceSpec, ...]
    lane_contracts: LaneContractRegistry
    lane_names: tuple[str, ...]
    base_dir: Path

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

    def get_lane(self, name: str) -> EventBus | None:
        return self.lanes.get(name)

    async def stop(self) -> None:
        await self.component_manager.stop()


class LaneRegistry:
    def __init__(self, buses: Mapping[str, EventBus]) -> None:
        self._buses = dict(buses)

    @classmethod
    def from_names(
        cls,
        lane_names: set[str] | frozenset[str] | tuple[str, ...],
        *,
        route_table: RouteTable,
    ) -> LaneRegistry:
        names = set(CORE_LANE_NAMES)
        names.update(lane_names)
        return cls(
            {name: EventBus(name, route_table=route_table) for name in sorted(names)}
        )

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
    discovered_component_ids: list[str] | tuple[str, ...] | None = None,
    definitions: ComponentDefinitions | None = None,
) -> list[ComponentInstanceSpec]:
    if definitions is not None:
        definition_map = _definition_mapping(definitions)
        component_ids = sorted(definition_map)
        definition_for = definition_map.get
    else:
        component_ids = sorted(set(discovered_component_ids or ()))
        definition_for = load_component_definition

    specs: list[ComponentInstanceSpec] = []
    for component_id in component_ids:
        definition = definition_for(component_id)
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


def _definition_mapping(definitions: ComponentDefinitions) -> dict[str, ComponentDefinition]:
    if isinstance(definitions, Mapping):
        return dict(definitions)
    return {definition.manifest.component_id: definition for definition in definitions}


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


def _optional_bool(value: Any, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


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


def _reserved_endpoint_ids(
    value: Any, *, field_name: str
) -> Mapping[str, frozenset[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a table of endpoint id = families")
    reserved: dict[str, frozenset[str]] = {}
    for endpoint_id, families in value.items():
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError(f"{field_name} endpoint ids must be non-empty strings")
        reserved[endpoint_id] = _string_set(
            families,
            field_name=f"{field_name}.{endpoint_id}",
        )
    return reserved


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


def _mqtt_delivery_constraints_from_mapping(
    value: Any,
    *,
    field_name: str,
) -> MqttDeliveryConstraints:
    if value is None:
        return MqttDeliveryConstraints()
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a table")
    max_qos = value.get("max_qos", 0)
    if not isinstance(max_qos, int) or max_qos < 0 or max_qos > 2:
        raise ValueError(f"{field_name}.max_qos must be an integer from 0 to 2")
    retain = value.get("retain", False)
    if not isinstance(retain, bool):
        raise ValueError(f"{field_name}.retain must be a boolean")
    persistent_session = value.get("persistent_session", False)
    if not isinstance(persistent_session, bool):
        raise ValueError(f"{field_name}.persistent_session must be a boolean")
    return MqttDeliveryConstraints(
        max_qos=max_qos,
        retain=retain,
        persistent_session=persistent_session,
    )


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
    return DeliverySemantics(
        persistence=(
            _optional_enum_value(
                DeliveryPersistence,
                source.get("persistence"),
                field_name="delivery.persistence",
            )
            or DeliveryPersistence.EPHEMERAL
        ),
        durability=(
            _optional_enum_value(
                DeliveryDurability,
                source.get("durability"),
                field_name="delivery.durability",
            )
            or DeliveryDurability.NON_DURABLE
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
            or BackpressureHandling.DISCONNECT_OR_WITHDRAW_ROUTE
        ),
        malformed_messages=(
            _optional_enum_value(
                MalformedMessageHandling,
                source.get("malformed_messages"),
                field_name="delivery.malformed_messages",
            )
            or MalformedMessageHandling.DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION
        ),
        mqtt=_mqtt_delivery_constraints_from_mapping(
            source.get("mqtt"),
            field_name="delivery.mqtt",
        ),
        message_families=_message_family_deliveries(
            source.get("message_families"),
            field_name="delivery.message_families",
        ),
    )


def _route_policy_from_mapping(source: Mapping[str, Any]) -> LaneRoutePolicy:
    if source.get("delivery_semantics") is not None:
        raise ValueError(
            "route_policy.delivery_semantics has been replaced by delivery"
        )
    return LaneRoutePolicy(
        remote_claim_endpoint_families=_string_set(
            source.get("remote_claim_endpoint_families"),
            field_name="remote_claim_endpoint_families",
        ),
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
        bridgeable=_optional_bool(source.get("bridgeable"), field_name="bridgeable"),
        local_only_message_types=_string_set(
            source.get("local_only_message_types"),
            field_name="local_only_message_types",
        ),
        reserved_endpoint_ids=_reserved_endpoint_ids(
            source.get("reserved_endpoint_ids"),
            field_name="reserved_endpoint_ids",
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
    route_policy_source = source.get("route_policy", {})
    if not isinstance(route_policy_source, Mapping):
        raise ValueError(f"Lane contract {lane!r} route_policy must be a table")
    return LaneContract(
        lane=lane,
        schema_id=schema_id,
        message_types=_string_set(
            source.get("message_types"),
            field_name=f"Lane contract {lane!r} message_types",
        ),
        delivery=_delivery_from_mapping(source.get("delivery"), lane=lane),
        route_policy=_route_policy_from_mapping(route_policy_source),
    )


def _merge_route_policy(
    base: LaneRoutePolicy,
    override: LaneRoutePolicy,
) -> LaneRoutePolicy:
    return LaneRoutePolicy(
        remote_claim_endpoint_families=(
            override.remote_claim_endpoint_families
            or base.remote_claim_endpoint_families
        ),
        allowed_sender_families=(
            override.allowed_sender_families
            if override.allowed_sender_families is not None
            else base.allowed_sender_families
        ),
        allowed_recipient_families=(
            override.allowed_recipient_families
            if override.allowed_recipient_families is not None
            else base.allowed_recipient_families
        ),
        broadcast_targets=override.broadcast_targets or base.broadcast_targets,
        default_broadcast_hop_limit=(
            override.default_broadcast_hop_limit
            if override.default_broadcast_hop_limit is not None
            else base.default_broadcast_hop_limit
        ),
        bridgeable=(
            override.bridgeable if override.bridgeable is not None else base.bridgeable
        ),
        local_only_message_types=(
            override.local_only_message_types or base.local_only_message_types
        ),
        reserved_endpoint_ids={
            **base.reserved_endpoint_ids,
            **override.reserved_endpoint_ids,
        },
    )


def _merge_lane_contract(base: LaneContract, override: LaneContract) -> LaneContract:
    return LaneContract(
        lane=base.lane,
        schema_id=override.schema_id or base.schema_id,
        message_types=override.message_types or base.message_types,
        delivery=override.delivery or base.delivery,
        route_policy=_merge_route_policy(base.route_policy, override.route_policy),
    )


def _narrow_families(
    base: frozenset[str],
    override: frozenset[str],
    *,
    field_name: str,
    lane: str,
) -> frozenset[str]:
    if not override:
        return base
    if not base or not override.issubset(base):
        raise ValueError(
            f"Deployment lane contract {lane!r} must not widen {field_name}"
        )
    return override


def _narrow_optional_families(
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


def _narrow_bridgeable(
    base: bool | None,
    override: bool | None,
    *,
    lane: str,
) -> bool | None:
    if override is None:
        return base
    base_allows_bridging = base is True
    if override is True and not base_allows_bridging:
        raise ValueError(f"Deployment lane contract {lane!r} must not widen bridgeable")
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


def _narrow_route_policy(
    base: LaneRoutePolicy,
    override: LaneRoutePolicy,
    *,
    lane: str,
) -> LaneRoutePolicy:
    reserved_endpoint_ids = {
        endpoint_id: families
        for endpoint_id, families in base.reserved_endpoint_ids.items()
    }
    for endpoint_id, families in override.reserved_endpoint_ids.items():
        reserved_endpoint_ids[endpoint_id] = (
            reserved_endpoint_ids.get(endpoint_id, frozenset()) | families
        )
    return LaneRoutePolicy(
        remote_claim_endpoint_families=_narrow_families(
            base.remote_claim_endpoint_families,
            override.remote_claim_endpoint_families,
            field_name="remote_claim_endpoint_families",
            lane=lane,
        ),
        allowed_sender_families=_narrow_optional_families(
            base.allowed_sender_families,
            override.allowed_sender_families,
            field_name="allowed_sender_families",
            lane=lane,
        ),
        allowed_recipient_families=_narrow_optional_families(
            base.allowed_recipient_families,
            override.allowed_recipient_families,
            field_name="allowed_recipient_families",
            lane=lane,
        ),
        broadcast_targets=_narrow_broadcast_targets(
            base.broadcast_targets,
            override.broadcast_targets,
            lane=lane,
        ),
        default_broadcast_hop_limit=_narrow_default_broadcast_hop_limit(
            base.default_broadcast_hop_limit,
            override.default_broadcast_hop_limit,
            lane=lane,
        ),
        bridgeable=_narrow_bridgeable(
            base.bridgeable,
            override.bridgeable,
            lane=lane,
        ),
        local_only_message_types=(
            base.local_only_message_types | override.local_only_message_types
        ),
        reserved_endpoint_ids=reserved_endpoint_ids,
    )


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
        route_policy=_narrow_route_policy(
            base.route_policy,
            override.route_policy,
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


def build_lane_contract_registry(
    instance_specs: Sequence[ComponentInstanceSpec],
    document: ConfigDocument,
) -> LaneContractRegistry:
    return _build_lane_contract_registry(instance_specs, document=document)


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


def build_lane_registry(
    instance_specs: Sequence[ComponentInstanceSpec],
    *,
    route_table: RouteTable,
) -> LaneRegistry:
    return LaneRegistry.from_names(
        _lane_names_for_specs(instance_specs),
        route_table=route_table,
    )


def _validate_component_lane_bindings(
    specs: Sequence[ComponentInstanceSpec],
    lane_contracts: LaneContractRegistry,
) -> None:
    for spec in specs:
        spec.definition.validate_resolved_lane_bindings(
            raw_config=spec.raw_config,
            instance_id=spec.instance_id,
            lane_contracts=lane_contracts,
        )


def resolve_component_host_plan(
    document: ConfigDocument,
    *,
    definitions: ComponentDefinitions | None = None,
) -> ComponentHostPlan:
    specs = tuple(
        configured_component_instance_specs(document)
        if definitions is None
        else resolve_component_instance_specs(document, definitions=definitions)
    )
    lane_contracts = _build_lane_contract_registry(specs, document=document)
    _validate_component_lane_bindings(specs, lane_contracts)
    return ComponentHostPlan(
        specs=specs,
        lane_contracts=lane_contracts,
        lane_names=_lane_names_for_specs(specs, lane_contracts=lane_contracts),
        base_dir=document.base_dir,
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
            raw_config=spec.raw_config,
            base_dir=plan.base_dir,
            lanes=deckr.lanes,
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

    return ComponentHost(
        component_manager=component_manager,
        components=tuple(created),
        lane_names=plan.lane_names,
        lanes=deckr.lanes,
    )


@asynccontextmanager
async def start_components(
    deckr: Deckr,
    plan: ComponentHostPlan,
) -> AsyncIterator[ComponentHost]:
    _validate_runtime_for_plan(deckr, plan)
    component_manager = ComponentManager()
    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        host = await _activate_component_plan(deckr, plan, component_manager)
        try:
            yield host
        finally:
            await host.stop()
            tg.cancel_scope.cancel()
