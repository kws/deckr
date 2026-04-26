from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from deckr.contracts.messages import (
    BUILTIN_ACTION_PROVIDER_ID,
    CORE_LANE_SCHEMA_IDS,
    HARDWARE_EVENTS_LANE,
    LEGACY_BUILTIN_ACTION_PROVIDER_ID,
    PLUGIN_MESSAGES_LANE,
)

EndpointFamilies = frozenset[str]
BroadcastTargets = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class LaneRoutePolicy:
    remote_claim_endpoint_families: EndpointFamilies = frozenset()
    allowed_sender_families: EndpointFamilies | None = None
    allowed_recipient_families: EndpointFamilies | None = None
    broadcast_targets: BroadcastTargets = field(default_factory=dict)
    default_broadcast_hop_limit: int | None = None
    bridgeable: bool | None = None
    local_only_message_types: frozenset[str] = frozenset()
    delivery_semantics: str | None = None
    reserved_endpoint_ids: Mapping[str, EndpointFamilies] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LaneContract:
    lane: str
    schema_id: str | None = None
    route_policy: LaneRoutePolicy = field(default_factory=LaneRoutePolicy)


class LaneContractRegistry:
    def __init__(self, contracts: Iterable[LaneContract] = ()) -> None:
        self._contracts = {contract.lane: contract for contract in contracts}

    def contract_for(self, lane: str) -> LaneContract:
        contract = self._contracts.get(lane)
        if contract is not None:
            return contract
        return LaneContract(lane=lane)

    @property
    def contracts(self) -> Mapping[str, LaneContract]:
        return dict(self._contracts)


CORE_LANE_CONTRACTS: Mapping[str, LaneContract] = {
    PLUGIN_MESSAGES_LANE: LaneContract(
        lane=PLUGIN_MESSAGES_LANE,
        schema_id=CORE_LANE_SCHEMA_IDS[PLUGIN_MESSAGES_LANE],
        route_policy=LaneRoutePolicy(
            remote_claim_endpoint_families=frozenset({"controller", "host"}),
            allowed_sender_families=frozenset({"controller", "host"}),
            allowed_recipient_families=frozenset({"controller", "host"}),
            broadcast_targets={
                "plugin_hosts": "host",
                "controllers": "controller",
            },
            default_broadcast_hop_limit=1,
            bridgeable=False,
            reserved_endpoint_ids={
                BUILTIN_ACTION_PROVIDER_ID: frozenset({"host"}),
                LEGACY_BUILTIN_ACTION_PROVIDER_ID: frozenset({"host"}),
            },
        ),
    ),
    HARDWARE_EVENTS_LANE: LaneContract(
        lane=HARDWARE_EVENTS_LANE,
        schema_id=CORE_LANE_SCHEMA_IDS[HARDWARE_EVENTS_LANE],
        route_policy=LaneRoutePolicy(
            remote_claim_endpoint_families=frozenset(
                {"controller", "hardware_manager"}
            ),
            allowed_sender_families=frozenset({"controller", "hardware_manager"}),
            allowed_recipient_families=frozenset(
                {"controller", "hardware_manager"}
            ),
            broadcast_targets={
                "controllers": "controller",
            },
            default_broadcast_hop_limit=1,
            bridgeable=False,
        ),
    ),
}

DEFAULT_LANE_CONTRACT_REGISTRY = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
