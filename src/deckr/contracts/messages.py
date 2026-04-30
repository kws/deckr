from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, RootModel, field_serializer, field_validator

from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json

HARDWARE_MESSAGES_LANE = "hardware_messages"
PLUGIN_MESSAGES_LANE = "plugin_messages"
CORE_LANE_NAMES = (HARDWARE_MESSAGES_LANE, PLUGIN_MESSAGES_LANE)

DECKR_MESSAGE_PROTOCOL_VERSION = "1"
HARDWARE_MESSAGES_SCHEMA_ID = "deckr.message.hardware_messages.v1"
PLUGIN_MESSAGES_SCHEMA_ID = "deckr.message.plugin_messages.v1"
CORE_LANE_SCHEMA_IDS = {
    HARDWARE_MESSAGES_LANE: HARDWARE_MESSAGES_SCHEMA_ID,
    PLUGIN_MESSAGES_LANE: PLUGIN_MESSAGES_SCHEMA_ID,
}

BUILTIN_ACTION_PROVIDER_ID = "deckr.controller.builtin"
RESERVED_BUILTIN_PROVIDER_IDS = frozenset(
    {
        BUILTIN_ACTION_PROVIDER_ID,
    }
)

CORE_ENDPOINT_FAMILIES = frozenset(
    {
        "controller",
        "host",
        "hardware_manager",
    }
)

BroadcastScope = str


def _require_identity_part(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value.strip() != value:
        raise ValueError(
            f"{field_name} must not contain leading or trailing whitespace"
        )
    if not value.strip():
        raise ValueError(f"{field_name} must not be whitespace")
    return value


def _require_endpoint_family(value: str, *, field_name: str) -> str:
    family = _require_identity_part(value, field_name=field_name)
    if family not in CORE_ENDPOINT_FAMILIES:
        raise ValueError(f"Unknown endpoint family {family!r}")
    return family


def _new_message_id() -> str:
    return str(uuid.uuid4())


def _now_utc() -> datetime:
    return datetime.now(UTC)


class EndpointAddress(RootModel[str]):
    """A typed Deckr endpoint address serialized as ``<family>:<endpoint_id>``."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("Endpoint address must be a string")
        if value.strip() != value:
            raise ValueError(
                "Endpoint address must not contain leading or trailing whitespace"
            )
        family, sep, endpoint_id = value.partition(":")
        if not sep:
            raise ValueError(
                "Endpoint address must have shape '<endpoint_family>:<endpoint_id>'"
            )
        _require_endpoint_family(family, field_name="Endpoint family")
        _require_identity_part(endpoint_id, field_name="Endpoint id")
        if ":" in endpoint_id:
            raise ValueError("Endpoint id must not contain ':'")
        return value

    @property
    def family(self) -> str:
        return self.root.split(":", 1)[0]

    @property
    def endpoint_id(self) -> str:
        return self.root.split(":", 1)[1]

    def __str__(self) -> str:
        return self.root


def endpoint_address(endpoint_family: str, endpoint_id: str) -> EndpointAddress:
    return EndpointAddress.model_validate(f"{endpoint_family}:{endpoint_id}")


def controller_address(controller_id: str) -> EndpointAddress:
    return endpoint_address("controller", controller_id)


def host_address(host_id: str) -> EndpointAddress:
    return endpoint_address("host", host_id)


def hardware_manager_address(manager_id: str) -> EndpointAddress:
    return endpoint_address("hardware_manager", manager_id)


def parse_endpoint_address(address: str | EndpointAddress) -> EndpointAddress:
    return (
        address
        if isinstance(address, EndpointAddress)
        else EndpointAddress.model_validate(address)
    )


def parse_controller_address(address: str | EndpointAddress) -> str | None:
    try:
        parsed = parse_endpoint_address(address)
    except ValueError:
        return None
    if parsed.family != "controller":
        return None
    return parsed.endpoint_id


def parse_host_address(address: str | EndpointAddress) -> str | None:
    try:
        parsed = parse_endpoint_address(address)
    except ValueError:
        return None
    if parsed.family != "host":
        return None
    return parsed.endpoint_id


def parse_hardware_manager_address(address: str | EndpointAddress) -> str | None:
    try:
        parsed = parse_endpoint_address(address)
    except ValueError:
        return None
    if parsed.family != "hardware_manager":
        return None
    return parsed.endpoint_id


class EndpointTarget(DeckrModel):
    target_type: Literal["endpoint"] = Field(default="endpoint", alias="targetType")
    endpoint: EndpointAddress


class BroadcastTarget(DeckrModel):
    target_type: Literal["broadcast"] = Field(default="broadcast", alias="targetType")
    scope: BroadcastScope
    endpoint_family: str
    domain: str | None = None
    hop_limit: int | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: BroadcastScope) -> BroadcastScope:
        return _require_identity_part(value, field_name="Broadcast scope")

    @field_validator("endpoint_family")
    @classmethod
    def _validate_endpoint_family(cls, value: str) -> str:
        return _require_endpoint_family(value, field_name="Broadcast endpoint family")

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_identity_part(value, field_name="Broadcast domain")

    @field_validator("hop_limit")
    @classmethod
    def _validate_hop_limit(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("Broadcast hop limit must be non-negative")
        return value


MessageTarget = Annotated[
    EndpointTarget | BroadcastTarget, Field(discriminator="target_type")
]


def endpoint_target(endpoint: str | EndpointAddress) -> EndpointTarget:
    return EndpointTarget(endpoint=parse_endpoint_address(endpoint))


def broadcast_target(
    *,
    scope: BroadcastScope,
    endpoint_family: str,
    domain: str | None = None,
    hop_limit: int | None = None,
) -> BroadcastTarget:
    return BroadcastTarget(
        scope=scope,
        endpoint_family=endpoint_family,
        domain=domain,
        hop_limit=hop_limit,
    )


def plugin_hosts_broadcast(
    *,
    domain: str | None = None,
    hop_limit: int | None = None,
) -> BroadcastTarget:
    return broadcast_target(
        scope="plugin_hosts",
        endpoint_family="host",
        domain=domain,
        hop_limit=hop_limit,
    )


def controllers_broadcast(
    *,
    domain: str | None = None,
    hop_limit: int | None = None,
) -> BroadcastTarget:
    return broadcast_target(
        scope="controllers",
        endpoint_family="controller",
        domain=domain,
        hop_limit=hop_limit,
    )


def hardware_managers_broadcast(
    *,
    domain: str | None = None,
    hop_limit: int | None = None,
) -> BroadcastTarget:
    return broadcast_target(
        scope="hardware_managers",
        endpoint_family="hardware_manager",
        domain=domain,
        hop_limit=hop_limit,
    )


class EntitySubject(DeckrModel):
    kind: str
    identifiers: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return _require_identity_part(value, field_name="Entity subject kind")

    @field_validator("identifiers", mode="after")
    @classmethod
    def _freeze_identifiers(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(
            {
                _require_identity_part(key, field_name="Entity subject id field"): (
                    _require_identity_part(
                        item,
                        field_name=f"Entity subject id {key!r}",
                    )
                )
                for key, item in value.items()
            }
        )

    @field_serializer("identifiers")
    def _serialize_identifiers(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)


def entity_subject(kind: str, **identifiers: str) -> EntitySubject:
    return EntitySubject(
        kind=kind,
        identifiers=identifiers,
    )


class TraceContext(DeckrModel):
    trace_parent: str | None = None
    trace_state: str | None = None


class DeckrMessage(DeckrModel):
    """Transport-neutral Deckr logical message envelope."""

    message_id: str = Field(default_factory=_new_message_id, alias="messageId")
    protocol_version: Literal["1"] = Field(
        default=DECKR_MESSAGE_PROTOCOL_VERSION,
        alias="protocolVersion",
    )
    schema_version: str = Field(default="1", alias="schemaVersion")
    lane: str
    message_type: str = Field(alias="messageType")
    sender: EndpointAddress
    recipient: MessageTarget
    subject: EntitySubject
    created_at: datetime = Field(default_factory=_now_utc, alias="createdAt")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    ttl_ms: int | None = Field(default=None, alias="ttlMs")
    in_reply_to: str | None = Field(default=None, alias="inReplyTo")
    causation_id: str | None = Field(default=None, alias="causationId")
    trace: TraceContext | None = None
    body: JsonObject

    @field_validator("ttl_ms")
    @classmethod
    def _validate_ttl_ms(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("ttlMs must be non-negative")
        return value

    @field_validator("body", mode="after")
    @classmethod
    def _freeze_body(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("body")
    def _serialize_body(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeckrMessage:
        return cls.model_validate(dict(data))

    @classmethod
    def schema_dict(cls) -> dict[str, Any]:
        return cls.model_json_schema(by_alias=True)


def message_targets_endpoint(
    message: DeckrMessage,
    endpoint: str | EndpointAddress,
) -> bool:
    parsed = parse_endpoint_address(endpoint)
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        return recipient.endpoint == parsed
    return recipient.endpoint_family == parsed.family


def is_direct_message(message: DeckrMessage) -> bool:
    return isinstance(message.recipient, EndpointTarget)


def message_schema_id_for_lane(lane: str) -> str | None:
    return CORE_LANE_SCHEMA_IDS.get(lane)


def message_expires_at(message: DeckrMessage) -> datetime | None:
    expiries: list[datetime] = []
    if message.expires_at is not None:
        expiries.append(message.expires_at)
    if message.ttl_ms is not None:
        expiries.append(message.created_at + timedelta(milliseconds=message.ttl_ms))
    if not expiries:
        return None
    return min(expiries)


def message_is_expired(
    message: DeckrMessage,
    *,
    now: datetime | None = None,
) -> bool:
    expires_at = message_expires_at(message)
    if expires_at is None:
        return False
    current = now or _now_utc()
    if expires_at.tzinfo is None and current.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=current.tzinfo)
    elif expires_at.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=expires_at.tzinfo)
    return expires_at <= current
