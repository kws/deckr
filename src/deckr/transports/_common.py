from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from deckr.core.components import ResolvedLaneSet

CORE_LANE_SCHEMA_IDS = {
    "hardware_events": "deckr.hardware.transport_message",
    "plugin_messages": "deckr.pluginhost.host_message",
}


class TransportDirection(StrEnum):
    INGRESS = "ingress"
    EGRESS = "egress"
    BIDIRECTIONAL = "bidirectional"


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def resolved_schema_id(*, lane: str, explicit_schema_id: str | None) -> str:
    core_schema_id = CORE_LANE_SCHEMA_IDS.get(lane)
    if core_schema_id is not None:
        return core_schema_id
    if explicit_schema_id:
        return explicit_schema_id
    raise ValueError(f"Transport binding for lane {lane!r} requires schema_id")


def transport_id_for(*, configured: str | None, runtime_name: str) -> str:
    candidate = (configured or "").strip()
    return candidate or runtime_name


class TransportBindingConfigBase(_StrictConfigModel):
    lane: str
    direction: TransportDirection = TransportDirection.BIDIRECTIONAL
    schema_id: str | None = None
    enabled: bool = True

    def resolved_schema_id(self) -> str:
        return resolved_schema_id(lane=self.lane, explicit_schema_id=self.schema_id)


def lanes_for_bindings(
    bindings: dict[str, TransportBindingConfigBase],
) -> ResolvedLaneSet:
    consumes: set[str] = set()
    publishes: set[str] = set()
    for binding in bindings.values():
        if not binding.enabled:
            continue
        if binding.direction in {
            TransportDirection.EGRESS,
            TransportDirection.BIDIRECTIONAL,
        }:
            consumes.add(binding.lane)
        if binding.direction in {
            TransportDirection.INGRESS,
            TransportDirection.BIDIRECTIONAL,
        }:
            publishes.add(binding.lane)
    return ResolvedLaneSet(
        consumes=tuple(sorted(consumes)),
        publishes=tuple(sorted(publishes)),
    )
