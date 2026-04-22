from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from deckr.core.components import ResolvedLaneSet

CORE_LANE_SCHEMA_IDS = {
    "hardware_events": "deckr.hardware.transport_message",
    "plugin_messages": "deckr.plugin.host_message",
}


class BridgeDirection(StrEnum):
    INGRESS = "ingress"
    EGRESS = "egress"
    BIDIRECTIONAL = "bidirectional"


class StrictBridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def resolved_schema_id(*, lane: str, explicit_schema_id: str | None) -> str:
    core_schema_id = CORE_LANE_SCHEMA_IDS.get(lane)
    if core_schema_id is not None:
        return core_schema_id
    if explicit_schema_id:
        return explicit_schema_id
    raise ValueError(f"Bridge binding for lane {lane!r} requires schema_id")


def bridge_id_for(*, configured: str | None, runtime_name: str) -> str:
    candidate = (configured or "").strip()
    return candidate or runtime_name


class BridgeBindingConfigBase(StrictBridgeModel):
    lane: str
    direction: BridgeDirection = BridgeDirection.BIDIRECTIONAL
    schema_id: str | None = None
    enabled: bool = True

    def resolved_schema_id(self) -> str:
        return resolved_schema_id(lane=self.lane, explicit_schema_id=self.schema_id)


def lanes_for_bindings(bindings: dict[str, BridgeBindingConfigBase]) -> ResolvedLaneSet:
    consumes: set[str] = set()
    publishes: set[str] = set()
    for binding in bindings.values():
        if not binding.enabled:
            continue
        if binding.direction in {
            BridgeDirection.EGRESS,
            BridgeDirection.BIDIRECTIONAL,
        }:
            consumes.add(binding.lane)
        if binding.direction in {
            BridgeDirection.INGRESS,
            BridgeDirection.BIDIRECTIONAL,
        }:
            publishes.add(binding.lane)
    return ResolvedLaneSet(
        consumes=tuple(sorted(consumes)),
        publishes=tuple(sorted(publishes)),
    )
