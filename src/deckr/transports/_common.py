from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from deckr.components import ResolvedLaneSet
from deckr.contracts.lanes import LaneContractRegistry
from deckr.contracts.messages import CORE_LANE_SCHEMA_IDS


class TransportDirection(StrEnum):
    INGRESS = "ingress"
    EGRESS = "egress"
    BIDIRECTIONAL = "bidirectional"


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def resolved_schema_id(*, lane: str, explicit_schema_id: str | None) -> str:
    explicit = explicit_schema_id.strip() if explicit_schema_id is not None else None
    core_schema_id = CORE_LANE_SCHEMA_IDS.get(lane)
    if core_schema_id is not None:
        if explicit:
            raise ValueError(
                f"Transport binding for core lane {lane!r} must not define schema_id"
            )
        return core_schema_id
    if explicit:
        return explicit
    raise ValueError(f"Transport binding for lane {lane!r} requires schema_id")


def transport_id_for(*, configured: str | None, runtime_name: str) -> str:
    candidate = (configured or "").strip()
    return candidate or runtime_name


class TransportBindingConfigBase(_StrictConfigModel):
    lane: str
    direction: TransportDirection = TransportDirection.BIDIRECTIONAL
    schema_id: str | None = None
    trusted_bridge: bool = False
    authority_id: str | None = None
    enabled: bool = True

    def resolved_schema_id(self) -> str:
        return resolved_schema_id(lane=self.lane, explicit_schema_id=self.schema_id)

    def allows_ingress(self) -> bool:
        return self.direction in {
            TransportDirection.INGRESS,
            TransportDirection.BIDIRECTIONAL,
        }

    def allows_egress(self) -> bool:
        return self.direction in {
            TransportDirection.EGRESS,
            TransportDirection.BIDIRECTIONAL,
        }


def validate_binding_schema_ids(
    bindings: dict[str, TransportBindingConfigBase],
) -> None:
    for binding in bindings.values():
        if binding.enabled:
            binding.resolved_schema_id()


def validate_binding_contracts(
    bindings: dict[str, TransportBindingConfigBase],
    lane_contracts: LaneContractRegistry,
) -> None:
    for binding_id, binding in bindings.items():
        if not binding.enabled:
            continue
        schema_id = binding.resolved_schema_id()
        contract = lane_contracts.contract_for(binding.lane)
        if contract.schema_id is None:
            raise ValueError(
                f"Transport binding {binding_id!r} for extension lane "
                f"{binding.lane!r} has no resolved lane contract"
            )
        if contract.schema_id != schema_id:
            raise ValueError(
                f"Transport binding {binding_id!r} schema_id {schema_id!r} "
                f"must match resolved lane contract schema_id "
                f"{contract.schema_id!r} for lane {binding.lane!r}"
            )


def lanes_for_bindings(
    bindings: dict[str, TransportBindingConfigBase],
) -> ResolvedLaneSet:
    validate_binding_schema_ids(bindings)
    consumes: set[str] = set()
    publishes: set[str] = set()
    for binding in bindings.values():
        if not binding.enabled:
            continue
        if binding.allows_egress():
            consumes.add(binding.lane)
        if binding.allows_ingress():
            publishes.add(binding.lane)
    return ResolvedLaneSet(
        consumes=tuple(sorted(consumes)),
        publishes=tuple(sorted(publishes)),
    )
