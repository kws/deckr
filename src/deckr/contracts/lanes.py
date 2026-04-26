from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from deckr.contracts.messages import (
    BUILTIN_ACTION_PROVIDER_ID,
    CORE_LANE_SCHEMA_IDS,
    HARDWARE_EVENTS_LANE,
    LEGACY_BUILTIN_ACTION_PROVIDER_ID,
    PLUGIN_MESSAGES_LANE,
)

EndpointFamilies = frozenset[str]
BroadcastTargets = Mapping[str, str]


class DeliveryPersistence(StrEnum):
    EPHEMERAL = "ephemeral"
    DURABLE = "durable"


class DeliveryDurability(StrEnum):
    NON_DURABLE = "non_durable"
    DURABLE = "durable"


class DeliveryGuarantee(StrEnum):
    BEST_EFFORT = "best_effort"
    AT_MOST_ONCE = "at_most_once"
    AT_LEAST_ONCE = "at_least_once"


class DeliveryReplay(StrEnum):
    NONE = "none"
    BROKER = "broker"
    LANE = "lane"


class DeliveryOrdering(StrEnum):
    LOCAL_OR_CONNECTION_FIFO = "local_or_connection_fifo"
    GLOBAL = "global"


class ExpiryHandling(StrEnum):
    DROP_AND_REPORT = "drop_and_report"
    DROP = "drop"
    DEAD_LETTER = "dead_letter"


class BackpressureHandling(StrEnum):
    DROP_SUBSCRIBER = "drop_subscriber"
    DISCONNECT_OR_WITHDRAW_ROUTE = "disconnect_or_withdraw_route"
    BLOCK = "block"


class MalformedMessageHandling(StrEnum):
    DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION = (
        "drop_unparseable_log_parseable_rejection"
    )
    DEAD_LETTER = "dead_letter"


class MessageFamily(StrEnum):
    LIFECYCLE = "lifecycle"
    DISCOVERY = "discovery"
    INPUT = "input"
    COMMAND = "command"
    REPLY = "reply"


class IdempotencySemantics(StrEnum):
    IDEMPOTENT_LATEST_BY_SUBJECT = "idempotent_latest_by_subject"
    NOT_REPLAYED_SEQUENCE_DUPLICATE_SUPPRESSION = (
        "not_replayed_sequence_duplicate_suppression"
    )
    DUPLICATE_REJECT_OR_LAST_WRITE_WINS = "duplicate_reject_or_last_write_wins"
    CORRELATE_IN_REPLY_TO_TIMEOUT = "correlate_in_reply_to_timeout"


@dataclass(frozen=True, slots=True)
class MqttDeliveryConstraints:
    max_qos: int = 0
    retain: bool = False
    persistent_session: bool = False


@dataclass(frozen=True, slots=True)
class MessageFamilyDelivery:
    family: MessageFamily
    message_types: frozenset[str] = frozenset()
    idempotency: IdempotencySemantics | None = None
    ordering_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliverySemantics:
    persistence: DeliveryPersistence = DeliveryPersistence.EPHEMERAL
    durability: DeliveryDurability = DeliveryDurability.NON_DURABLE
    guarantee: DeliveryGuarantee = DeliveryGuarantee.AT_MOST_ONCE
    replay: DeliveryReplay = DeliveryReplay.NONE
    ordering: DeliveryOrdering = DeliveryOrdering.LOCAL_OR_CONNECTION_FIFO
    ordering_keys: tuple[str, ...] = ()
    expiry: ExpiryHandling = ExpiryHandling.DROP_AND_REPORT
    local_backpressure: BackpressureHandling = BackpressureHandling.DROP_SUBSCRIBER
    remote_backpressure: BackpressureHandling = (
        BackpressureHandling.DISCONNECT_OR_WITHDRAW_ROUTE
    )
    malformed_messages: MalformedMessageHandling = (
        MalformedMessageHandling.DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION
    )
    mqtt: MqttDeliveryConstraints = field(default_factory=MqttDeliveryConstraints)
    message_families: tuple[MessageFamilyDelivery, ...] = ()


@dataclass(frozen=True, slots=True)
class LaneRoutePolicy:
    remote_claim_endpoint_families: EndpointFamilies = frozenset()
    allowed_sender_families: EndpointFamilies | None = None
    allowed_recipient_families: EndpointFamilies | None = None
    broadcast_targets: BroadcastTargets = field(default_factory=dict)
    default_broadcast_hop_limit: int | None = None
    bridgeable: bool | None = None
    local_only_message_types: frozenset[str] = frozenset()
    reserved_endpoint_ids: Mapping[str, EndpointFamilies] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LaneContract:
    lane: str
    schema_id: str | None = None
    message_types: frozenset[str] = frozenset()
    delivery: DeliverySemantics | None = None
    route_policy: LaneRoutePolicy = field(default_factory=LaneRoutePolicy)


def unsupported_delivery_reason(delivery: DeliverySemantics | None) -> str | None:
    if delivery is None:
        return None
    if delivery.persistence != DeliveryPersistence.EPHEMERAL:
        return "only ephemeral delivery is implemented"
    if delivery.durability != DeliveryDurability.NON_DURABLE:
        return "only non-durable delivery is implemented"
    if delivery.guarantee != DeliveryGuarantee.AT_MOST_ONCE:
        return "only at-most-once delivery is implemented"
    if delivery.replay != DeliveryReplay.NONE:
        return "replay delivery is not implemented"
    if delivery.ordering != DeliveryOrdering.LOCAL_OR_CONNECTION_FIFO:
        return "only local or transport-connection FIFO ordering is implemented"
    if delivery.expiry != ExpiryHandling.DROP_AND_REPORT:
        return "only drop-and-report expiry handling is implemented"
    if delivery.local_backpressure != BackpressureHandling.DROP_SUBSCRIBER:
        return "only drop-subscriber local backpressure is implemented"
    if (
        delivery.remote_backpressure
        != BackpressureHandling.DISCONNECT_OR_WITHDRAW_ROUTE
    ):
        return "only disconnect-or-withdraw remote backpressure is implemented"
    if (
        delivery.malformed_messages
        != MalformedMessageHandling.DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION
    ):
        return "only drop/log malformed-message handling is implemented"
    if delivery.mqtt.max_qos != 0:
        return "only MQTT QoS 0 is implemented"
    if delivery.mqtt.retain:
        return "retained MQTT delivery is not implemented"
    if delivery.mqtt.persistent_session:
        return "persistent MQTT sessions are not implemented"
    return None


class LaneContractRegistry:
    def __init__(self, contracts: Iterable[LaneContract] = ()) -> None:
        self._contracts: dict[str, LaneContract] = {}
        for contract in contracts:
            reason = unsupported_delivery_reason(contract.delivery)
            if reason is not None:
                raise ValueError(
                    f"Lane contract {contract.lane!r} delivery profile is not "
                    f"supported: {reason}"
                )
            self._contracts[contract.lane] = contract

    def contract_for(self, lane: str) -> LaneContract:
        contract = self._contracts.get(lane)
        if contract is not None:
            return contract
        return LaneContract(lane=lane)

    @property
    def contracts(self) -> Mapping[str, LaneContract]:
        return dict(self._contracts)


PLUGIN_MESSAGE_TYPES = frozenset(
    {
        "actionsRegistered",
        "actionsUnregistered",
        "closePage",
        "dialRotate",
        "hereAreSettings",
        "hostOffline",
        "hostOnline",
        "keyDown",
        "keyUp",
        "openPage",
        "pageAppear",
        "pageDisappear",
        "requestActions",
        "requestSettings",
        "setImage",
        "setPage",
        "setSettings",
        "setTitle",
        "showAlert",
        "showOk",
        "sleepScreen",
        "touchSwipe",
        "touchTap",
        "wakeScreen",
        "willAppear",
        "willDisappear",
    }
)

HARDWARE_MESSAGE_TYPES = frozenset(
    {
        "clearSlot",
        "deviceConnected",
        "deviceDisconnected",
        "dialRotate",
        "keyDown",
        "keyUp",
        "setImage",
        "sleepScreen",
        "touchSwipe",
        "touchTap",
        "wakeScreen",
    }
)

CORE_EPHEMERAL_DELIVERY = DeliverySemantics(
    persistence=DeliveryPersistence.EPHEMERAL,
    durability=DeliveryDurability.NON_DURABLE,
    guarantee=DeliveryGuarantee.AT_MOST_ONCE,
    replay=DeliveryReplay.NONE,
    ordering=DeliveryOrdering.LOCAL_OR_CONNECTION_FIFO,
    expiry=ExpiryHandling.DROP_AND_REPORT,
    local_backpressure=BackpressureHandling.DROP_SUBSCRIBER,
    remote_backpressure=BackpressureHandling.DISCONNECT_OR_WITHDRAW_ROUTE,
    malformed_messages=(
        MalformedMessageHandling.DROP_UNPARSEABLE_LOG_PARSEABLE_REJECTION
    ),
    mqtt=MqttDeliveryConstraints(max_qos=0, retain=False, persistent_session=False),
)

PLUGIN_MESSAGES_DELIVERY = replace(
    CORE_EPHEMERAL_DELIVERY,
    ordering_keys=(
        "sender",
        "recipient",
        "subject.contextId",
        "subject.actionUuid",
        "subject.hostId",
    ),
    message_families=(
        MessageFamilyDelivery(
            family=MessageFamily.LIFECYCLE,
            message_types=frozenset(
                {
                    "hostOnline",
                    "hostOffline",
                    "pageAppear",
                    "pageDisappear",
                }
            ),
            idempotency=IdempotencySemantics.IDEMPOTENT_LATEST_BY_SUBJECT,
            ordering_keys=(
                "sender",
                "recipient",
                "subject.hostId",
                "subject.contextId",
            ),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.DISCOVERY,
            message_types=frozenset(
                {
                    "actionsRegistered",
                    "actionsUnregistered",
                    "requestActions",
                }
            ),
            idempotency=IdempotencySemantics.IDEMPOTENT_LATEST_BY_SUBJECT,
            ordering_keys=("sender", "subject.hostId"),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.INPUT,
            message_types=frozenset(
                {
                    "dialRotate",
                    "keyDown",
                    "keyUp",
                    "touchSwipe",
                    "touchTap",
                    "willAppear",
                    "willDisappear",
                }
            ),
            idempotency=(
                IdempotencySemantics.NOT_REPLAYED_SEQUENCE_DUPLICATE_SUPPRESSION
            ),
            ordering_keys=("sender", "recipient", "subject.contextId"),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.COMMAND,
            message_types=frozenset(
                {
                    "closePage",
                    "openPage",
                    "requestSettings",
                    "setImage",
                    "setPage",
                    "setSettings",
                    "setTitle",
                    "showAlert",
                    "showOk",
                    "sleepScreen",
                    "wakeScreen",
                }
            ),
            idempotency=(IdempotencySemantics.DUPLICATE_REJECT_OR_LAST_WRITE_WINS),
            ordering_keys=("sender", "recipient", "subject.contextId"),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.REPLY,
            message_types=frozenset({"hereAreSettings"}),
            idempotency=IdempotencySemantics.CORRELATE_IN_REPLY_TO_TIMEOUT,
            ordering_keys=("inReplyTo",),
        ),
    ),
)

HARDWARE_EVENTS_DELIVERY = replace(
    CORE_EPHEMERAL_DELIVERY,
    ordering_keys=(
        "sender",
        "subject.managerId",
        "subject.deviceId",
        "subject.controlId",
        "messageType",
        "body.sequence",
    ),
    message_families=(
        MessageFamilyDelivery(
            family=MessageFamily.DISCOVERY,
            message_types=frozenset({"deviceConnected", "deviceDisconnected"}),
            idempotency=IdempotencySemantics.IDEMPOTENT_LATEST_BY_SUBJECT,
            ordering_keys=("sender", "subject.managerId", "subject.deviceId"),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.INPUT,
            message_types=frozenset(
                {
                    "dialRotate",
                    "keyDown",
                    "keyUp",
                    "touchSwipe",
                    "touchTap",
                }
            ),
            idempotency=(
                IdempotencySemantics.NOT_REPLAYED_SEQUENCE_DUPLICATE_SUPPRESSION
            ),
            ordering_keys=(
                "sender",
                "subject.managerId",
                "subject.deviceId",
                "subject.controlId",
                "body.sequence",
            ),
        ),
        MessageFamilyDelivery(
            family=MessageFamily.COMMAND,
            message_types=frozenset(
                {"clearSlot", "setImage", "sleepScreen", "wakeScreen"}
            ),
            idempotency=(IdempotencySemantics.DUPLICATE_REJECT_OR_LAST_WRITE_WINS),
            ordering_keys=(
                "sender",
                "recipient",
                "subject.managerId",
                "subject.deviceId",
                "subject.controlId",
            ),
        ),
    ),
)

CORE_LANE_CONTRACTS: Mapping[str, LaneContract] = {
    PLUGIN_MESSAGES_LANE: LaneContract(
        lane=PLUGIN_MESSAGES_LANE,
        schema_id=CORE_LANE_SCHEMA_IDS[PLUGIN_MESSAGES_LANE],
        message_types=PLUGIN_MESSAGE_TYPES,
        delivery=PLUGIN_MESSAGES_DELIVERY,
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
        message_types=HARDWARE_MESSAGE_TYPES,
        delivery=HARDWARE_EVENTS_DELIVERY,
        route_policy=LaneRoutePolicy(
            remote_claim_endpoint_families=frozenset(
                {"controller", "hardware_manager"}
            ),
            allowed_sender_families=frozenset({"controller", "hardware_manager"}),
            allowed_recipient_families=frozenset({"controller", "hardware_manager"}),
            broadcast_targets={
                "controllers": "controller",
            },
            default_broadcast_hop_limit=1,
            bridgeable=False,
        ),
    ),
}

DEFAULT_LANE_CONTRACT_REGISTRY = LaneContractRegistry(CORE_LANE_CONTRACTS.values())
