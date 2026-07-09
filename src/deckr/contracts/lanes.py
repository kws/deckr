from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from deckr.contracts.messages import (
    CORE_LANE_SCHEMA_IDS,
    HARDWARE_MESSAGES_LANE,
    SERVICE_MESSAGES_SCHEMA_ID,
    SERVICES_LANE,
)

EndpointFamilies = frozenset[str]
BroadcastTargets = Mapping[str, str]


class ContractRequirement(StrEnum):
    OPTIONAL = "optional"
    REQUIRED = "required"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class MessageContract:
    lane: str
    schema_id: str | None = None
    message_types: frozenset[str] = frozenset()
    allowed_sender_families: EndpointFamilies | None = None
    allowed_recipient_families: EndpointFamilies | None = None
    broadcast_targets: BroadcastTargets = field(default_factory=dict)
    default_broadcast_hop_limit: int | None = None
    default_contract_requirement: ContractRequirement = ContractRequirement.OPTIONAL
    contract_requirements: Mapping[str, ContractRequirement] = field(
        default_factory=dict
    )

    def contract_requirement_for(self, message_type: str) -> ContractRequirement:
        return self.contract_requirements.get(
            message_type,
            self.default_contract_requirement,
        )


class MessageContractRegistry:
    def __init__(self, contracts: Iterable[MessageContract] = ()) -> None:
        self._contracts: dict[str, MessageContract] = {}
        for contract in contracts:
            if contract.lane in self._contracts:
                raise ValueError(f"Duplicate message contract {contract.lane!r}")
            self._contracts[contract.lane] = contract

    def contract_for(self, lane: str) -> MessageContract:
        contract = self._contracts.get(lane)
        if contract is not None:
            return contract
        raise LookupError(f"Message contract {lane!r} is not registered")

    @property
    def contracts(self) -> Mapping[str, MessageContract]:
        return dict(self._contracts)


ACTION_MESSAGE_TYPES = frozenset(
    {
        "actionExtension",
        "actionInstanceCreated",
        "actionInstanceDestroyed",
        "actionLifecycleRejected",
        "bindingAttached",
        "bindingDetached",
        "bindingOutput",
        "bindingOverlay",
        "bindingOverlayClear",
        "capabilityInput",
        "closePage",
        "openPage",
        "pageSessionClosed",
        "pageSessionOpened",
        "replacePage",
        "settingsRequest",
        "settingsSnapshot",
    }
)

HARDWARE_MESSAGE_TYPES = frozenset(
    {
        "capabilityStateChanged",
        "capabilityStateReply",
        "capabilityStateRequest",
        "commandAccepted",
        "commandRejected",
        "commandReply",
        "controlCommand",
        "controlInput",
    }
)

SERVICE_MESSAGE_TYPES = frozenset(
    {
        "serviceMessage",
    }
)

SERVICE_LANE_CONTRACT = MessageContract(
    lane=SERVICES_LANE,
    schema_id=SERVICE_MESSAGES_SCHEMA_ID,
    message_types=SERVICE_MESSAGE_TYPES,
    allowed_sender_families=frozenset({"action_provider", "controller", "service"}),
    allowed_recipient_families=frozenset(
        {"action_provider", "controller", "service"}
    ),
    default_contract_requirement=ContractRequirement.REQUIRED,
)

ACTION_NO_CONTRACT_MESSAGE_TYPES = frozenset(
    {}
)

ACTION_OPTIONAL_CONTRACT_MESSAGE_TYPES = frozenset(
    {
        "actionExtension",
    }
)

CORE_LANE_CONTRACTS: Mapping[str, MessageContract] = {
    SERVICES_LANE: SERVICE_LANE_CONTRACT,
    HARDWARE_MESSAGES_LANE: MessageContract(
        lane=HARDWARE_MESSAGES_LANE,
        schema_id=CORE_LANE_SCHEMA_IDS[HARDWARE_MESSAGES_LANE],
        message_types=HARDWARE_MESSAGE_TYPES,
        allowed_sender_families=frozenset({"controller", "hardware_manager"}),
        allowed_recipient_families=frozenset({"controller", "hardware_manager"}),
        broadcast_targets={
            "controllers": "controller",
        },
        default_broadcast_hop_limit=1,
        default_contract_requirement=ContractRequirement.REQUIRED,
    ),
}

DEFAULT_MESSAGE_CONTRACT_REGISTRY = MessageContractRegistry(
    CORE_LANE_CONTRACTS.values()
)
