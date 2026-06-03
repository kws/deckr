from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Literal

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.core.util.anyio import CoalescedTrigger
from deckr.substrates.nats_kv import (
    KvBucketPolicy,
    KvChange,
    KvConflict,
    KvEntry,
    KvUnavailable,
    NatsKvMaterializedBucket,
)

CONCORD_CONTRACT_SCHEMA_ID = "dev.deckr.concord.contract.v1"
CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID = "dev.deckr.concord.participant-token.v1"
CONCORD_STALE_OBSERVATION_SCHEMA_ID = "dev.deckr.concord.stale-observation.v1"
DEFAULT_CONCORD_CONTRACT_BUCKET_NAME = "deckr_concord_contract_v1"
DEFAULT_CONCORD_TOKEN_BUCKET_NAME = "deckr_concord_token_v1"
DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME = "deckr_concord_maintenance_v1"
DEFAULT_CONCORD_TOKEN_TTL_SECONDS = 30
DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS = 15.0
DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS = 15.0
DEFAULT_CONCORD_NOTIFICATION_BATCH_SECONDS = 0.05
DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS = 900
DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS = 3600
DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS = 60
CONCORD_MAINTENANCE_ACTOR = "concord:maintenance"
CONCORD_REAPER_STALE_CONTRACT_REASON = "concord_reaper_stale_contract"
ACTION_PROVIDER_SESSION_PROFILE_ID = "dev.deckr.profile.action_provider_session.v1"
CONCORD_CONTRACT_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    ttl_seconds=None,
    description="Concord contract KV",
)
CONCORD_MAINTENANCE_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    ttl_seconds=None,
    description="Concord maintenance KV",
)
CONCORD_TOKEN_BUCKET_POLICY = KvBucketPolicy(
    bucket=DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
    ttl_seconds=float(DEFAULT_CONCORD_TOKEN_TTL_SECONDS),
    allow_write_ttl=True,
    description="Concord participant token KV",
)

logger = logging.getLogger(__name__)


class ConcordConflict(RuntimeError):
    """Raised when a Concord create or revision-checked write conflicts."""


class ConcordUnavailable(RuntimeError):
    """Raised when Concord KV state cannot be read or written safely."""


def _single_exception_from_group(exc: BaseExceptionGroup) -> BaseException | None:
    if len(exc.exceptions) != 1:
        return None
    child = exc.exceptions[0]
    if isinstance(child, BaseExceptionGroup):
        return _single_exception_from_group(child)
    return child


def _contract_lifecycle_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


def _contract_pending_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


def _contract_invalid_log_level(
    profile: str | None,
    status: ContractValidityStatus | None,
) -> int:
    if (
        status == ContractValidityStatus.MISSING_TOKEN
        and _is_chattery_contract_profile(profile)
    ):
        return logging.DEBUG
    return logging.WARNING


def _is_chattery_contract_profile(profile: str | None) -> bool:
    return profile == ACTION_PROVIDER_SESSION_PROFILE_ID or (
        profile is not None and profile.endswith(".service_use.v1")
    )


class ContractState(StrEnum):
    OPEN = "open"
    CANCELLED = "cancelled"


class ContractValidityStatus(StrEnum):
    VALID = "valid"
    NOT_YET_FULFILLED = "not_yet_fulfilled"
    CANCELLED = "cancelled"
    MISSING_CONTRACT = "missing_contract"
    INVALID_CONTRACT = "invalid_contract"
    INVALID_TOKEN = "invalid_token"
    MISSING_TOKEN = "missing_token"
    GENERATION_MISMATCH = "generation_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    TERMS_HASH_MISMATCH = "terms_hash_mismatch"
    UNAVAILABLE = "unavailable"


class ConcordEventType(StrEnum):
    CONTRACT_PROPOSED = "contract_proposed"
    CONTRACT_UPDATED = "contract_updated"
    CONTRACT_VALID = "contract_valid"
    CONTRACT_PENDING = "contract_pending"
    CONTRACT_INVALID = "contract_invalid"
    CONTRACT_CANCELLED = "contract_cancelled"
    CONTRACT_DELETED = "contract_deleted"
    TOKEN_ATTACHED = "token_attached"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_WITHDRAWN = "token_withdrawn"


class ConcordManagedContractEventType(StrEnum):
    VALID = "valid"
    PENDING = "pending"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    RELEASED = "released"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _concord_token_refresh_interval(
    *,
    requested: float,
    ttl_seconds: int | float,
) -> float:
    ttl = float(ttl_seconds)
    return min(max(float(requested), ttl / 2), ttl * 0.8)


def concord_contract_key(*, contract_id: str, generation: int) -> str:
    return ".".join(
        (
            "contracts",
            encode_key_token(contract_id),
            str(generation),
            "meta",
        )
    )


def parse_concord_contract_key(key: str) -> tuple[str, int] | None:
    parts = key.split(".")
    if len(parts) != 4 or parts[0] != "contracts" or parts[3] != "meta":
        return None
    try:
        generation = int(parts[2])
    except ValueError:
        return None
    return decode_key_token(parts[1]), generation


def concord_participant_token_key(
    *,
    contract_id: str,
    generation: int,
    participant: str | EndpointAddress,
) -> str:
    parsed = parse_endpoint_address(participant)
    return ".".join(
        (
            "contracts",
            encode_key_token(contract_id),
            str(generation),
            "participants",
            encode_key_token(str(parsed)),
        )
    )


def parse_concord_participant_token_key(
    key: str,
) -> tuple[str, int, EndpointAddress] | None:
    parts = key.split(".")
    if len(parts) != 5 or parts[0] != "contracts" or parts[3] != "participants":
        return None
    try:
        generation = int(parts[2])
    except ValueError:
        return None
    return (
        decode_key_token(parts[1]),
        generation,
        parse_endpoint_address(decode_key_token(parts[4])),
    )


def concord_contract_prefix(*, contract_id: str, generation: int) -> str:
    return ".".join(("contracts", encode_key_token(contract_id), str(generation), ""))


def concord_contracts_prefix() -> str:
    return "contracts."


def concord_contract_id_prefix(*, contract_id: str) -> str:
    return ".".join(("contracts", encode_key_token(contract_id), ""))


def concord_stale_observation_key(*, contract_id: str, generation: int) -> str:
    return ".".join(("stale", encode_key_token(contract_id), str(generation)))


def canonical_json_bytes(value: Mapping[str, Any] | DeckrModel) -> bytes:
    if isinstance(value, DeckrModel):
        payload = value.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        payload = thaw_json(freeze_json(value))
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_hash(value: Mapping[str, Any] | DeckrModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ContractPointer(DeckrModel):
    contract_id: str = Field(alias="contractId")
    generation: int

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return _require_text(value, field_name="contract id")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value


class TokenObservation(DeckrModel):
    generation: int
    refresh_seq: int | None = Field(default=None, alias="refreshSeq")
    revision: int | None = None
    token_hash: str | None = Field(default=None, alias="tokenHash")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value

    @field_validator("refresh_seq", "revision")
    @classmethod
    def _validate_optional_non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("token observation values must be non-negative")
        return value


class ConcordStaleObservationRecord(DeckrModel):
    schema_id: Literal[CONCORD_STALE_OBSERVATION_SCHEMA_ID] = Field(
        default=CONCORD_STALE_OBSERVATION_SCHEMA_ID,
        alias="schema",
    )
    contract_id: str = Field(alias="contractId")
    generation: int
    first_observed_stale_at: datetime = Field(alias="firstObservedStaleAt")
    status: ContractValidityStatus
    reason: str | None = None
    contract_revision: int | None = Field(default=None, alias="contractRevision")

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return _require_text(value, field_name="contract id")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value

    @field_validator("first_observed_stale_at")
    @classmethod
    def _validate_first_observed_stale_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("firstObservedStaleAt must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="Concord stale observation reason")

    @field_validator("contract_revision")
    @classmethod
    def _validate_contract_revision(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("contractRevision must be non-negative")
        return value

    @field_serializer("first_observed_stale_at")
    def _serialize_first_observed_stale_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ContractRecord(DeckrModel):
    schema_id: Literal[CONCORD_CONTRACT_SCHEMA_ID] = Field(
        default=CONCORD_CONTRACT_SCHEMA_ID,
        alias="schema",
    )
    contract_id: str = Field(alias="contractId")
    generation: int
    participants: tuple[EndpointAddress, ...]
    attached_participants: tuple[EndpointAddress, ...] = Field(
        alias="attachedParticipants"
    )
    state: ContractState = ContractState.OPEN
    profile: str | None = None
    terms_hash: str | None = Field(default=None, alias="termsHash")
    terms: JsonObject | None = None
    created_by: EndpointAddress | None = Field(default=None, alias="createdBy")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    cancelled_by: EndpointAddress | Literal["concord:maintenance"] | None = Field(
        default=None,
        alias="cancelledBy",
    )
    cancelled_at: datetime | None = Field(default=None, alias="cancelledAt")
    cancel_revision: int | None = Field(default=None, alias="cancelRevision")
    cancel_reason: str | None = Field(default=None, alias="cancelReason")
    supersedes: ContractPointer | None = None

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return _require_text(value, field_name="contract id")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value

    @field_validator("participants", mode="after")
    @classmethod
    def _validate_participants(
        cls,
        value: tuple[EndpointAddress, ...],
    ) -> tuple[EndpointAddress, ...]:
        if not value:
            raise ValueError("Concord contracts require at least one participant")
        strings = [str(item) for item in value]
        if len(strings) != len(set(strings)):
            raise ValueError("Concord contract participants must be unique")
        if strings != sorted(strings):
            raise ValueError("Concord contract participants must be canonicalized")
        return value

    @field_validator("attached_participants", mode="after")
    @classmethod
    def _validate_attached_participants(
        cls,
        value: tuple[EndpointAddress, ...],
    ) -> tuple[EndpointAddress, ...]:
        strings = [str(item) for item in value]
        if len(strings) != len(set(strings)):
            raise ValueError("Concord attached participants must be unique")
        if strings != sorted(strings):
            raise ValueError("Concord attached participants must be canonicalized")
        return value

    @field_validator("profile", "terms_hash", "cancel_reason")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="Concord contract field")

    @field_validator("terms", mode="before")
    @classmethod
    def _thaw_terms(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("terms", mode="after")
    @classmethod
    def _freeze_terms(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("terms")
    def _serialize_terms(self, value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None

    @field_serializer("created_at", "cancelled_at")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def _validate_terms_hash(self) -> ContractRecord:
        if self.terms is None:
            return self
        if self.terms_hash is None:
            raise ValueError("Concord contract terms require termsHash")
        if self.terms_hash != canonical_json_hash(self.terms):
            raise ValueError("Concord contract termsHash does not match terms")
        if self.profile is not None:
            profile = self.terms.get("profile")
            if profile is not None and profile != self.profile:
                raise ValueError("Concord contract profile must match terms.profile")
        return self

    @model_validator(mode="after")
    def _validate_attached_participants_subset(self) -> ContractRecord:
        participants = {str(item) for item in self.participants}
        attached = {str(item) for item in self.attached_participants}
        if not attached <= participants:
            raise ValueError("attachedParticipants must be a subset of participants")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ParticipantTokenRecord(DeckrModel):
    schema_id: Literal[CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID] = Field(
        default=CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
        alias="schema",
    )
    contract_id: str = Field(alias="contractId")
    generation: int
    participant: EndpointAddress
    session_id: str = Field(alias="sessionId")
    token_id: str = Field(alias="tokenId")
    refresh_seq: int = Field(alias="refreshSeq")
    ttl_seconds: int = Field(alias="ttlSeconds")
    terms_hash: str | None = Field(default=None, alias="termsHash")
    contract_hash: str | None = Field(default=None, alias="contractHash")
    observed: Mapping[str, TokenObservation] = Field(default_factory=dict)

    @field_validator("contract_id", "session_id", "token_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _require_text(value, field_name="Concord participant token identity")

    @field_validator("terms_hash", "contract_hash")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="Concord participant token field")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value

    @field_validator("refresh_seq")
    @classmethod
    def _validate_refresh_seq(cls, value: int) -> int:
        if value < 1:
            raise ValueError("refreshSeq must be greater than zero")
        return value

    @field_validator("ttl_seconds")
    @classmethod
    def _validate_ttl_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ttlSeconds must be greater than zero")
        return value

    @field_validator("observed", mode="after")
    @classmethod
    def _freeze_observed(
        cls,
        value: Mapping[str, TokenObservation],
    ) -> Mapping[str, TokenObservation]:
        return freeze_json(value)

    @field_serializer("observed")
    def _serialize_observed(
        self,
        value: Mapping[str, TokenObservation],
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ConcordReaperConfig(DeckrModel):
    stale_grace_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS),
        ge=0,
        alias="staleGraceSeconds",
    )
    cancelled_retention_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS),
        ge=0,
        alias="cancelledRetentionSeconds",
    )
    scan_interval_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS),
        gt=0,
        alias="scanIntervalSeconds",
    )
    log_label: str = Field(default="ConcordReaper", alias="logLabel")

    @field_validator("log_label")
    @classmethod
    def _validate_log_label(cls, value: str) -> str:
        return _require_text(value, field_name="Concord reaper log label")


@dataclass(frozen=True, slots=True)
class ContractHandle:
    key: str
    contract_id: str
    generation: int
    participants: tuple[EndpointAddress, ...]
    attached_participants: tuple[EndpointAddress, ...]
    revision: int
    state: ContractState
    profile: str | None = None
    terms_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ParticipantHandle:
    key: str
    contract_id: str
    generation: int
    participant: EndpointAddress
    session_id: str
    token_id: str
    revision: int
    refresh_seq: int
    ttl_seconds: int
    terms_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ContractValidity:
    status: ContractValidityStatus
    contract: ContractRecord | None = None
    tokens: Mapping[str, ParticipantHandle] = field(default_factory=dict)
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.status == ContractValidityStatus.VALID


@dataclass(frozen=True, slots=True)
class ConcordMaintenanceDeletionResult:
    deleted: bool
    deleted_token_key_count: int = 0


@dataclass(frozen=True, slots=True)
class ConcordReaperScanResult:
    scanned_contract_count: int = 0
    stale_observation_count: int = 0
    stale_observations_created: int = 0
    stale_observations_cleared: int = 0
    contracts_cancelled: int = 0
    contracts_deleted: int = 0
    token_keys_deleted: int = 0


@dataclass(frozen=True, slots=True)
class ConcordEvent:
    event_type: ConcordEventType
    contract: ContractHandle | None = None
    record: ContractRecord | None = None
    validity: ContractValidity | None = None
    token: ParticipantHandle | None = None
    profile: str | None = None
    participant: EndpointAddress | None = None
    reason: str | None = None
    change: KvChange | None = None


@dataclass(frozen=True, slots=True)
class ConcordManagedContract:
    contract: ContractHandle
    record: ContractRecord
    validity: ContractValidity
    token: ParticipantHandle | None = None


@dataclass(frozen=True, slots=True)
class ConcordManagedContractEvent:
    event_type: ConcordManagedContractEventType
    contract: ContractHandle
    record: ContractRecord | None = None
    validity: ContractValidity | None = None
    token: ParticipantHandle | None = None
    reason: str | None = None


class _ConcordBucketAdapter:
    def __init__(self, bucket: NatsKvMaterializedBucket | Any) -> None:
        self._bucket = (
            bucket
            if _is_materialized_bucket(bucket)
            else NatsKvMaterializedBucket(bucket=bucket)
        )

    @property
    def bucket(self) -> str:
        return self._bucket.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._bucket.start(task_group)

    def is_ready(self) -> bool:
        return self._bucket.is_ready()

    def is_current(self) -> bool:
        return self._bucket.is_current()

    async def wait_ready(self) -> None:
        await self._bucket.wait_ready()

    async def wait_current(self) -> None:
        await self._bucket.wait_current()

    def get_cached(self, key: str) -> KvEntry | None:
        return self._bucket.get_cached(key)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return self._bucket.items_cached(prefix)

    def revision_cached(self, key: str) -> int | None:
        revision_cached = getattr(self._bucket, "revision_cached", None)
        if revision_cached is None:
            return None
        return revision_cached(key)

    async def get(self, key: str) -> KvEntry | None:
        try:
            return await self._bucket.get_exact(key)
        except KvUnavailable as exc:
            raise ConcordUnavailable(str(exc)) from exc

    async def get_exact(self, key: str) -> KvEntry | None:
        return await self.get(key)

    async def items(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return self.items_cached(prefix)

    async def items_exact(self, prefix: str = "") -> tuple[KvEntry, ...]:
        try:
            return await self._bucket.items_exact(prefix)
        except KvUnavailable as exc:
            raise ConcordUnavailable(str(exc)) from exc

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        try:
            return await self._bucket.create(key, value, ttl=ttl)
        except KvConflict as exc:
            raise ConcordConflict(str(exc)) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(str(exc)) from exc

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        try:
            return await self._bucket.update(key, value, revision=revision, ttl=ttl)
        except KvConflict as exc:
            raise ConcordConflict(str(exc)) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(str(exc)) from exc

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        try:
            await self._bucket.delete(key, revision=revision)
        except KvConflict as exc:
            raise ConcordConflict(str(exc)) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(str(exc)) from exc

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange]]:
        async with self._bucket.subscribe() as changes:
            yield changes

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange]]:
        send, receive = anyio.create_memory_object_stream[KvChange](100)

        async def filter_loop() -> None:
            async with self.subscribe() as changes:
                async for change in changes:
                    if change.key.startswith(prefix):
                        await send.send(change)

        try:
            async with receive, send, anyio.create_task_group() as task_group:
                task_group.start_soon(filter_loop)
                yield receive
                task_group.cancel_scope.cancel()
        finally:
            await send.aclose()


def _is_materialized_bucket(value: Any) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            "start",
            "wait_ready",
            "is_ready",
            "is_current",
            "wait_current",
            "get_exact",
            "get_cached",
            "items_cached",
            "items_exact",
            "revision_cached",
            "subscribe",
            "create",
            "update",
            "delete",
        )
    )


class _ConcordKvStore:
    def __init__(
        self,
        contract_bucket: NatsKvMaterializedBucket | Any,
        token_bucket: NatsKvMaterializedBucket | Any,
        *,
        token_ttl_seconds: int = DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
    ) -> None:
        if token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be greater than zero")
        self._contract_bucket = _ConcordBucketAdapter(contract_bucket)
        self._token_bucket = _ConcordBucketAdapter(token_bucket)
        self._token_ttl_seconds = token_ttl_seconds

    async def create_contract(
        self,
        participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress],
        *,
        contract_id: str | None = None,
        generation: int = 1,
        profile: str | None = None,
        terms: Mapping[str, Any] | DeckrModel | None = None,
        created_by: str | EndpointAddress | None = None,
        supersedes: ContractPointer | Mapping[str, Any] | None = None,
    ) -> ContractHandle:
        parsed_participants = tuple(
            sorted((parse_endpoint_address(item) for item in participants), key=str)
        )
        dumped_terms = (
            terms.model_dump(by_alias=True, exclude_none=True, mode="json")
            if isinstance(terms, DeckrModel)
            else terms
        )
        terms_hash = canonical_json_hash(dumped_terms) if dumped_terms is not None else None
        record = ContractRecord(
            contractId=contract_id or str(uuid.uuid4()),
            generation=generation,
            participants=parsed_participants,
            attachedParticipants=(),
            state=ContractState.OPEN,
            profile=profile,
            termsHash=terms_hash,
            terms=dumped_terms,
            createdBy=parse_endpoint_address(created_by) if created_by is not None else None,
            createdAt=_now_utc(),
            supersedes=supersedes,
        )
        key = concord_contract_key(
            contract_id=record.contract_id,
            generation=record.generation,
        )
        entry = await self._contract_bucket.create(key, record)
        return _contract_handle(key, record, entry.revision)

    async def get_contract(
        self,
        pointer: ContractPointer | Mapping[str, Any],
    ) -> ContractHandle | None:
        parsed = (
            pointer
            if isinstance(pointer, ContractPointer)
            else ContractPointer.model_validate(pointer)
        )
        key = concord_contract_key(
            contract_id=parsed.contract_id,
            generation=parsed.generation,
        )
        entry = await self._contract_bucket.get(key)
        if entry is None:
            return None
        record = ContractRecord.model_validate(entry.value)
        if (
            record.contract_id != parsed.contract_id
            or record.generation != parsed.generation
        ):
            return None
        return _contract_handle(key, record, entry.revision)

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        entry = await self._contract_bucket.get(contract.key)
        if entry is None:
            return None
        record = ContractRecord.model_validate(entry.value)
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            return None
        return record

    async def attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ParticipantHandle:
        current = await self._contract_bucket.get(contract.key)
        if current is None:
            raise ConcordConflict(f"Concord contract {contract.key!r} is missing")
        record = ContractRecord.model_validate(current.value)
        if record.state == ContractState.CANCELLED:
            raise ConcordConflict(f"Concord contract {contract.key!r} is cancelled")
        parsed_participant = parse_endpoint_address(participant)
        if parsed_participant not in record.participants:
            raise ValueError("participant is not named by the Concord contract")
        if parsed_participant in record.attached_participants:
            raise ConcordConflict("Concord participant is already attached")
        ttl = ttl_seconds or self._token_ttl_seconds
        token = ParticipantTokenRecord(
            contractId=record.contract_id,
            generation=record.generation,
            participant=parsed_participant,
            sessionId=session_id,
            tokenId=token_id or str(uuid.uuid4()),
            refreshSeq=1,
            ttlSeconds=ttl,
            termsHash=record.terms_hash,
        )
        key = concord_participant_token_key(
            contract_id=record.contract_id,
            generation=record.generation,
            participant=parsed_participant,
        )
        try:
            entry = await self._token_bucket.create(key, token, ttl=token.ttl_seconds)
        except ConcordConflict as exc:
            token_entry = await self._token_bucket.get(key)
            if token_entry is None:
                raise ConcordConflict(
                    "Concord participant token changed during attach"
                ) from exc
            token = ParticipantTokenRecord.model_validate(token_entry.value)
            if not _token_matches_attach_request(
                token,
                record=record,
                participant=parsed_participant,
                session_id=session_id,
                token_id=token_id,
            ):
                raise ConcordConflict("Concord participant token already exists") from exc
            entry = token_entry
            await self._mark_participant_attached(
                contract_key=contract.key,
                participant=parsed_participant,
                allow_already_attached=False,
            )
        else:
            await self._mark_participant_attached(
                contract_key=contract.key,
                participant=parsed_participant,
                allow_already_attached=True,
            )
        return _participant_handle(key, token, entry.revision)

    async def _mark_participant_attached(
        self,
        *,
        contract_key: str,
        participant: EndpointAddress,
        allow_already_attached: bool,
    ) -> None:
        while True:
            current = await self._contract_bucket.get(contract_key)
            if current is None:
                raise ConcordConflict(f"Concord contract {contract_key!r} is missing")
            record = ContractRecord.model_validate(current.value)
            if record.state == ContractState.CANCELLED:
                raise ConcordConflict(f"Concord contract {contract_key!r} is cancelled")
            if participant not in record.participants:
                raise ConcordConflict("participant is not named by the Concord contract")
            if participant in record.attached_participants:
                if allow_already_attached:
                    return
                raise ConcordConflict("Concord participant is already attached")
            attached = tuple(sorted((*record.attached_participants, participant), key=str))
            updated = record.model_copy(update={"attached_participants": attached})
            try:
                await self._contract_bucket.update(
                    contract_key,
                    updated,
                    revision=current.revision,
                )
            except ConcordConflict:
                continue
            return

    async def refresh(self, handle: ParticipantHandle) -> ParticipantHandle:
        contract_entry = await self._contract_bucket.get(
            concord_contract_key(
                contract_id=handle.contract_id,
                generation=handle.generation,
            )
        )
        if contract_entry is None:
            raise ConcordConflict("Concord contract is missing")
        contract = ContractRecord.model_validate(contract_entry.value)
        if contract.state == ContractState.CANCELLED:
            raise ConcordConflict("Concord contract is cancelled")
        token_entry = await self._token_bucket.get(handle.key)
        if token_entry is None:
            raise ConcordConflict("Concord participant token is missing")
        token = ParticipantTokenRecord.model_validate(token_entry.value)
        if not _token_matches_handle(token, handle):
            raise ConcordConflict("Concord participant token changed owner")
        refreshed = token.model_copy(update={"refresh_seq": token.refresh_seq + 1})
        try:
            entry = await self._token_bucket.update(
                handle.key,
                refreshed,
                revision=token_entry.revision,
                ttl=refreshed.ttl_seconds,
            )
        except ConcordConflict as exc:
            if not _is_state_revision_conflict(exc):
                raise
            latest_entry = await self._token_bucket.get(handle.key)
            if latest_entry is None:
                raise ConcordConflict("Concord participant token is missing") from exc
            latest = ParticipantTokenRecord.model_validate(latest_entry.value)
            if not _token_matches_handle(latest, handle):
                raise ConcordConflict("Concord participant token changed owner") from exc
            return _participant_handle(handle.key, latest, latest_entry.revision)
        return _participant_handle(handle.key, refreshed, entry.revision)

    async def validate_participant_handle(
        self,
        handle: ParticipantHandle,
    ) -> ParticipantHandle:
        contract_entry = await self._contract_bucket.get(
            concord_contract_key(
                contract_id=handle.contract_id,
                generation=handle.generation,
            )
        )
        if contract_entry is None:
            raise ConcordConflict("Concord contract is missing")
        contract = ContractRecord.model_validate(contract_entry.value)
        if contract.state == ContractState.CANCELLED:
            raise ConcordConflict("Concord contract is cancelled")
        token_entry = await self._token_bucket.get(handle.key)
        if token_entry is None:
            raise ConcordConflict("Concord participant token is missing")
        token = ParticipantTokenRecord.model_validate(token_entry.value)
        if not _token_matches_handle(token, handle):
            raise ConcordConflict("Concord participant token changed owner")
        return _participant_handle(handle.key, token, token_entry.revision)

    async def withdraw(self, handle: ParticipantHandle) -> bool:
        token_entry = await self._token_bucket.get(handle.key)
        if token_entry is None:
            return False
        try:
            token = ParticipantTokenRecord.model_validate(token_entry.value)
        except ValueError as exc:
            raise ConcordConflict("Concord participant token is invalid") from exc
        if not _token_matches_handle(token, handle):
            raise ConcordConflict("Concord participant token changed owner")
        await self._token_bucket.delete(handle.key, revision=token_entry.revision)
        return True

    async def cancel(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
    ) -> bool:
        current = await self._contract_bucket.get(contract.key)
        if current is None:
            return False
        record = ContractRecord.model_validate(current.value)
        if record.state == ContractState.CANCELLED:
            return False
        parsed_participant = parse_endpoint_address(participant)
        if parsed_participant not in record.participants:
            raise ValueError("participant is not named by the Concord contract")
        cancelled = record.model_copy(
            update={
                "state": ContractState.CANCELLED,
                "cancelled_by": parsed_participant,
                "cancelled_at": _now_utc(),
                "cancel_revision": current.revision,
                "cancel_reason": reason,
            }
        )
        await self._contract_bucket.update(
            contract.key,
            cancelled,
            revision=current.revision,
        )
        return True

    async def maintenance_cancel(
        self,
        contract: ContractHandle,
        *,
        reason: str = CONCORD_REAPER_STALE_CONTRACT_REASON,
        cancelled_by: Literal["concord:maintenance"] = CONCORD_MAINTENANCE_ACTOR,
        now: datetime | None = None,
    ) -> bool:
        current = await self._contract_bucket.get(contract.key)
        if current is None:
            return False
        record = ContractRecord.model_validate(current.value)
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            raise ConcordConflict(f"Concord contract {contract.key!r} changed identity")
        if record.state == ContractState.CANCELLED:
            return False
        cancelled = record.model_copy(
            update={
                "state": ContractState.CANCELLED,
                "cancelled_by": cancelled_by,
                "cancelled_at": now or _now_utc(),
                "cancel_revision": current.revision,
                "cancel_reason": reason,
            }
        )
        await self._contract_bucket.update(
            contract.key,
            cancelled,
            revision=current.revision,
        )
        return True

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        try:
            contract_entry = await self._contract_bucket.get(contract.key)
        except ConcordUnavailable:
            return ContractValidity(ContractValidityStatus.UNAVAILABLE)
        if contract_entry is None:
            return ContractValidity(ContractValidityStatus.MISSING_CONTRACT)
        try:
            record = ContractRecord.model_validate(contract_entry.value)
        except ValueError as exc:
            return ContractValidity(
                ContractValidityStatus.INVALID_CONTRACT,
                reason=str(exc),
            )
        if record.state == ContractState.CANCELLED:
            return ContractValidity(ContractValidityStatus.CANCELLED, contract=record)

        attached_participants = {str(item) for item in record.attached_participants}
        pending_participant: str | None = None
        tokens: dict[str, ParticipantHandle] = {}
        for participant in record.participants:
            participant_key = str(participant)
            token_key = concord_participant_token_key(
                contract_id=record.contract_id,
                generation=record.generation,
                participant=participant,
            )
            try:
                token_entry = await self._token_bucket.get(token_key)
            except ConcordUnavailable:
                return ContractValidity(
                    ContractValidityStatus.UNAVAILABLE,
                    contract=record,
                )
            if token_entry is None:
                if participant_key in attached_participants:
                    return ContractValidity(
                        ContractValidityStatus.MISSING_TOKEN,
                        contract=record,
                        tokens=tokens,
                        reason=participant_key,
                    )
                pending_participant = pending_participant or participant_key
                continue
            try:
                token = ParticipantTokenRecord.model_validate(token_entry.value)
            except ValueError as exc:
                return ContractValidity(
                    ContractValidityStatus.INVALID_TOKEN,
                    contract=record,
                    tokens=tokens,
                    reason=str(exc),
                )
            status = _token_validity_status(
                token,
                contract=record,
                participant=participant,
                current_sessions=current_sessions,
            )
            tokens[participant_key] = _participant_handle(
                token_key,
                token,
                token_entry.revision,
            )
            if status is not None:
                return ContractValidity(status, contract=record, tokens=tokens)
            if participant_key not in attached_participants:
                pending_participant = pending_participant or participant_key
        if pending_participant is not None:
            return ContractValidity(
                ContractValidityStatus.NOT_YET_FULFILLED,
                contract=record,
                tokens=tokens,
                reason=pending_participant,
            )
        return ContractValidity(
            ContractValidityStatus.VALID,
            contract=record,
            tokens=tokens,
        )

    def watch(
        self,
        contract: ContractHandle,
    ) -> Any:
        return self._contract_bucket.watch(
            concord_contract_prefix(
                contract_id=contract.contract_id,
                generation=contract.generation,
            )
        )


class ConcordParticipantLease:
    """Owns one participant token and its heartbeat for a Concord contract."""

    def __init__(
        self,
        service: Concord,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        self._service = service
        self.contract = contract
        self.participant = parse_endpoint_address(participant)
        self.session_id = _require_text(session_id, field_name="Concord session id")
        self._token_id = token_id
        self._ttl_seconds = ttl_seconds
        self._requested_refresh_interval = refresh_interval
        self._refresh_interval = _concord_token_refresh_interval(
            requested=refresh_interval,
            ttl_seconds=ttl_seconds or service._coordinator._token_ttl_seconds,  # noqa: SLF001
        )
        self._log_label = log_label
        self._token: ParticipantHandle | None = None
        self._last_refresh_at: float | None = None
        self._lock = anyio.Lock()
        self._started = False
        self._closed = False

    @property
    def token(self) -> ParticipantHandle | None:
        return self._token

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object]) -> None:
        if self._started:
            return
        self._started = True
        start_soon(self.heartbeat_loop)

    async def aclose(self, *, withdraw: bool = True) -> None:
        async with self._lock:
            token = self._token
            self._closed = True
            self._token = None
            self._last_refresh_at = None
        if withdraw and token is not None:
            try:
                await self._service._withdraw_token(  # noqa: SLF001
                    token,
                    log_label=self._log_label,
                )
            except (ConcordConflict, ConcordUnavailable):
                logger.debug(
                    "%s Concord participant token cleanup failed contract=%s "
                    "generation=%s participant=%s session=%s token=%s",
                    self._log_label,
                    token.contract_id,
                    token.generation,
                    token.participant,
                    token.session_id,
                    token.token_id,
                    exc_info=True,
                )
        await self._service._forget_participant_lease(self)  # noqa: SLF001

    def adopt(self, token: ParticipantHandle) -> None:
        if token.contract_id != self.contract.contract_id:
            raise ValueError("participant token belongs to a different contract")
        if token.generation != self.contract.generation:
            raise ValueError("participant token belongs to a different generation")
        if token.participant != self.participant:
            raise ValueError("participant token belongs to a different participant")
        if token.session_id != self.session_id:
            raise ValueError("participant token belongs to a different session")
        if self._token == token:
            return
        self._refresh_interval = _concord_token_refresh_interval(
            requested=self._requested_refresh_interval,
            ttl_seconds=token.ttl_seconds,
        )
        self._token = token
        self._last_refresh_at = monotonic()

    async def attach_or_refresh(self) -> ParticipantHandle:
        async with self._lock:
            if self._closed:
                raise ConcordConflict("Concord participant lease is closed")
            token = self._token
            if token is not None:
                try:
                    self.adopt(await self._service._validate_participant_token(token))
                    token = self._token
                    if token is None:
                        raise ConcordConflict("Concord participant token is missing")
                    if not self._token_refresh_due():
                        return token
                    self._token = await self._service._refresh_token(
                        token,
                        log_label=self._log_label,
                    )
                    self._last_refresh_at = monotonic()
                    return self._token
                except ConcordConflict as exc:
                    self._token = None
                    self._last_refresh_at = None
                    if _is_terminal_participant_conflict(exc):
                        self._closed = True
                    else:
                        logger.warning(
                            "%s Concord participant token refresh conflict; "
                            "authority lost contract=%s generation=%s participant=%s "
                            "session=%s",
                            self._log_label,
                            self.contract.contract_id,
                            self.contract.generation,
                            self.participant,
                            self.session_id,
                            exc_info=True,
                        )
                    raise
            try:
                self._token = await self._service._attach(
                    self.contract,
                    self.participant,
                    self.session_id,
                    token_id=self._token_id,
                    ttl_seconds=self._ttl_seconds,
                    log_label=self._log_label,
                )
                self._refresh_interval = _concord_token_refresh_interval(
                    requested=self._requested_refresh_interval,
                    ttl_seconds=self._token.ttl_seconds,
                )
                self._last_refresh_at = monotonic()
            except ConcordConflict as exc:
                if _is_terminal_participant_conflict(exc):
                    self._closed = True
                raise
            return self._token

    def _token_refresh_due(self) -> bool:
        return (
            self._last_refresh_at is None
            or monotonic() - self._last_refresh_at >= self._refresh_interval
        )

    async def heartbeat_loop(self) -> None:
        while not self._closed:
            await anyio.sleep(self._refresh_interval)
            if self._closed:
                return
            try:
                await self.attach_or_refresh()
            except ConcordConflict:
                if self._closed:
                    return
                logger.warning(
                    "%s Concord participant token conflict; heartbeat will retry "
                    "contract=%s generation=%s participant=%s session=%s",
                    self._log_label,
                    self.contract.contract_id,
                    self.contract.generation,
                    self.participant,
                    self.session_id,
                    exc_info=True,
                )
            except ConcordUnavailable:
                logger.warning(
                    "%s Concord participant token unavailable; heartbeat will retry "
                    "contract=%s generation=%s participant=%s session=%s",
                    self._log_label,
                    self.contract.contract_id,
                    self.contract.generation,
                    self.participant,
                    self.session_id,
                    exc_info=True,
                )


@dataclass(slots=True, eq=False)
class _ConcordSubscriber:
    send: anyio.abc.ObjectSendStream[ConcordEvent]
    profile: str | None
    participant: EndpointAddress | None
    replay_pending: bool = False
    pending_events: list[ConcordEvent] = field(default_factory=list)


class Concord:
    """Runtime-facing Concord API with participant leases and semantic events."""

    def __init__(
        self,
        contract_bucket: NatsKvMaterializedBucket | Any,
        token_bucket: NatsKvMaterializedBucket | Any,
        maintenance_bucket: NatsKvMaterializedBucket | Any,
        *,
        token_ttl_seconds: int = DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
        buffer_size: int = 100,
    ) -> None:
        self._coordinator = _ConcordKvStore(
            contract_bucket,
            token_bucket,
            token_ttl_seconds=token_ttl_seconds,
        )
        self._maintenance_bucket = _ConcordBucketAdapter(maintenance_bucket)
        self._buffer_size = buffer_size
        self._ready = anyio.Event()
        self._started = False
        self._closed = False
        self._task_group: anyio.abc.TaskGroup | None = None
        self._agreements: dict[tuple[Any, ...], ConcordAgreementLease] = {}
        self._agreement_lock = anyio.Lock()
        self._lock = anyio.Lock()
        self._subscribers: set[_ConcordSubscriber] = set()
        self._participant_leases: set[ConcordParticipantLease] = set()
        self._contract_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_contracts_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._contract_records_by_key: dict[str, ContractRecord] = {}
        self._contract_handles_by_key: dict[str, ContractHandle] = {}
        self._contract_keys_by_pointer: dict[tuple[str, int], str] = {}
        self._contract_keys_by_contract_id: dict[str, set[str]] = {}
        self._contract_keys_by_profile: dict[str | None, set[str]] = {}
        self._contract_keys_by_participant: dict[str, set[str]] = {}
        self._contract_keys_by_state: dict[ContractState, set[str]] = {}
        self._contract_revision_by_key: dict[str, int] = {}
        self._token_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_tokens_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._token_records_by_key: dict[str, ParticipantTokenRecord] = {}
        self._tokens_by_key: dict[str, ParticipantHandle] = {}
        self._token_keys_by_contract: dict[tuple[str, int], set[str]] = {}
        self._token_key_by_contract_participant: dict[tuple[str, int, str], str] = {}
        self._token_revision_by_key: dict[str, int] = {}
        self._maintenance_entries_by_key: dict[str, KvEntry] = {}
        self._maintenance_records_by_key: dict[str, ConcordStaleObservationRecord] = {}
        self._maintenance_revision_by_key: dict[str, int] = {}
        self._last_status_by_contract_key: dict[str, ContractValidityStatus] = {}

    @property
    def contract_bucket(self) -> str:
        return self._coordinator._contract_bucket.bucket  # noqa: SLF001

    @property
    def token_bucket(self) -> str:
        return self._coordinator._token_bucket.bucket  # noqa: SLF001

    @property
    def maintenance_bucket(self) -> str:
        return self._maintenance_bucket.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        self._coordinator._contract_bucket.start(task_group)  # noqa: SLF001
        self._coordinator._token_bucket.start(task_group)  # noqa: SLF001
        self._maintenance_bucket.start(task_group)
        if not self._started:
            self._started = True
            task_group.start_soon(self._event_loop)
        for lease in tuple(self._participant_leases):
            lease.start(task_group)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_current(self) -> bool:
        return (
            self._ready.is_set()
            and self._state_views_current()
            and self._state_cache_matches_materialized_buckets()
        )

    async def wait_current(self) -> None:
        await self.wait_ready()
        await self._coordinator._contract_bucket.wait_current()  # noqa: SLF001
        await self._coordinator._token_bucket.wait_current()  # noqa: SLF001
        while not self._state_cache_matches_materialized_buckets():
            await anyio.sleep(0)

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            leases = tuple(self._participant_leases)
            self._participant_leases.clear()
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for lease in leases:
            await lease.aclose()
        for subscriber in subscribers:
            await subscriber.send.aclose()

    async def propose(
        self,
        spec: ConcordAgreementSpec,
        *,
        start_soon: Callable[..., object] | None = None,
    ) -> ConcordAgreementLease:
        """Create, reuse, or supersede an owner-side agreement.

        This method is the production lifecycle entry point for a participant
        that owns the contract. It validates before attaching so a generation
        with lost participant authority is cancelled and superseded instead of
        receiving a replacement token.
        """

        if self._started:
            await self.wait_current()
        async with self._agreement_lock:
            return await self._ensure_agreement_locked(spec, start_soon=start_soon)

    async def _ensure_agreement_locked(
        self,
        spec: ConcordAgreementSpec,
        *,
        start_soon: Callable[..., object] | None = None,
    ) -> ConcordAgreementLease:
        cache_key = _agreement_cache_key(spec)
        if cache_key is not None:
            cached = self._agreements.get(cache_key)
            if cached is not None and not cached.closed:
                validity = await cached.refresh()
                if not _agreement_successor_status(validity.status):
                    return cached
                await self._cancel_agreement(
                    cached,
                    reason=f"concord_agreement_{validity.status.value}",
                )
                self._agreements.pop(cache_key, None)

        while True:
            contract, validity = await self._select_or_create_agreement_contract(spec)
            agreement = self._agreement_from_contract(spec, contract, validity)
            if start_soon is not None:
                agreement._lease.start_soon(start_soon)  # noqa: SLF001
            validity = await agreement.refresh()
            if _agreement_successor_status(validity.status):
                await self._cancel_agreement(
                    agreement,
                    reason=f"concord_agreement_{validity.status.value}",
                )
                if cache_key is not None:
                    self._agreements.pop(cache_key, None)
                continue
            if cache_key is not None:
                self._agreements[cache_key] = agreement
            return agreement

    def participant(
        self,
        *,
        participant: str | EndpointAddress,
        session_id: str,
        accept_contract: ConcordContractPredicate,
        current_sessions: ConcordCurrentSessions | None = None,
        prepare_reconcile: ConcordPrepareReconcile | None = None,
        contract_sort_key: ConcordContractSortKey | None = None,
        profile: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        reconcile_interval: float = DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS,
        cancel_terminal_statuses: Collection[ContractValidityStatus] | None = None,
        log_label: str = "Concord",
    ) -> ConcordParticipant:
        return ConcordParticipant(
            concord=self,
            participant=participant,
            session_id=session_id,
            accept_contract=accept_contract,
            current_sessions=current_sessions,
            prepare_reconcile=prepare_reconcile,
            contract_sort_key=contract_sort_key,
            profile=profile,
            refresh_interval=refresh_interval,
            reconcile_interval=reconcile_interval,
            cancel_terminal_statuses=cancel_terminal_statuses,
            log_label=log_label,
        )

    async def _event_loop(self) -> None:
        contract_bucket = self._coordinator._contract_bucket  # noqa: SLF001
        token_bucket = self._coordinator._token_bucket  # noqa: SLF001
        async with (
            contract_bucket.subscribe() as contract_changes,
            token_bucket.subscribe() as token_changes,
            self._maintenance_bucket.subscribe() as maintenance_changes,
            anyio.create_task_group() as task_group,
        ):
            await contract_bucket.wait_current()
            await token_bucket.wait_current()
            await self._maintenance_bucket.wait_current()
            await self._rebuild_from_buckets()
            self._ready.set()
            task_group.start_soon(self._consume_contract_changes, contract_changes)
            task_group.start_soon(self._consume_token_changes, token_changes)
            task_group.start_soon(
                self._consume_maintenance_changes,
                maintenance_changes,
            )

    async def _consume_contract_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_contract_change(change)

    async def _consume_token_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_token_change(change)

    async def _consume_maintenance_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_maintenance_change(change)

    async def _rebuild_from_buckets(self) -> None:
        contract_entries = self._coordinator._contract_bucket.items_cached(  # noqa: SLF001
            concord_contracts_prefix()
        )
        token_entries = self._coordinator._token_bucket.items_cached(  # noqa: SLF001
            concord_contracts_prefix()
        )
        maintenance_entries = self._maintenance_bucket.items_cached("stale.")
        async with self._lock:
            self._clear_indexes_locked()
            for entry in contract_entries:
                self._index_contract_entry_locked(entry)
            for entry in token_entries:
                self._index_token_entry_locked(entry)
            for entry in maintenance_entries:
                self._index_maintenance_entry_locked(entry)
            self._last_status_by_contract_key = {
                key: self._validate_from_cache_locked(handle).status
                for key, handle in self._contract_handles_by_key.items()
            }

    def _state_views_current(self) -> bool:
        return (
            self._coordinator._contract_bucket.is_current()  # noqa: SLF001
            and self._coordinator._token_bucket.is_current()  # noqa: SLF001
        )

    def _state_cache_matches_materialized_buckets(self) -> bool:
        prefix = concord_contracts_prefix()
        return self._cache_matches_materialized_bucket(
            self._coordinator._contract_bucket,  # noqa: SLF001
            self._contract_revision_by_key,
            prefix=prefix,
        ) and self._cache_matches_materialized_bucket(
            self._coordinator._token_bucket,  # noqa: SLF001
            self._token_revision_by_key,
            prefix=prefix,
        )

    @staticmethod
    def _cache_matches_materialized_bucket(
        bucket: _ConcordBucketAdapter,
        revision_by_key: Mapping[str, int],
        *,
        prefix: str,
    ) -> bool:
        for entry in bucket.items_cached(prefix):
            if revision_by_key.get(entry.key, 0) < entry.revision:
                return False
        for key, revision in revision_by_key.items():
            if not key.startswith(prefix):
                continue
            bucket_revision = bucket.revision_cached(key)
            if bucket_revision is not None and revision < bucket_revision:
                return False
        return True

    def _clear_indexes_locked(self) -> None:
        self._contract_entries_by_key.clear()
        self._invalid_contracts_by_key.clear()
        self._contract_records_by_key.clear()
        self._contract_handles_by_key.clear()
        self._contract_keys_by_pointer.clear()
        self._contract_keys_by_contract_id.clear()
        self._contract_keys_by_profile.clear()
        self._contract_keys_by_participant.clear()
        self._contract_keys_by_state.clear()
        self._contract_revision_by_key.clear()
        self._token_entries_by_key.clear()
        self._invalid_tokens_by_key.clear()
        self._token_records_by_key.clear()
        self._tokens_by_key.clear()
        self._token_keys_by_contract.clear()
        self._token_key_by_contract_participant.clear()
        self._token_revision_by_key.clear()
        self._maintenance_entries_by_key.clear()
        self._maintenance_records_by_key.clear()
        self._maintenance_revision_by_key.clear()

    async def _apply_contract_change(self, change: KvChange) -> None:
        async with self._lock:
            events = self._apply_contract_change_locked(change)
            deliveries = self._subscriber_deliveries_locked(events)
        await self._publish_events(events, deliveries)

    async def _apply_token_change(self, change: KvChange) -> None:
        async with self._lock:
            events = self._apply_token_change_locked(change)
            deliveries = self._subscriber_deliveries_locked(events)
        await self._publish_events(events, deliveries)

    async def _apply_maintenance_change(self, change: KvChange) -> None:
        async with self._lock:
            self._apply_maintenance_change_locked(change)

    def _apply_contract_change_locked(self, change: KvChange) -> tuple[ConcordEvent, ...]:
        current_revision = self._contract_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            return ()
        previous = self._contract_handles_by_key.get(change.key)
        previous_record = self._contract_records_by_key.get(change.key)
        self._remove_contract_key_locked(change.key)
        self._contract_revision_by_key[change.key] = change.revision
        events: list[ConcordEvent] = []
        if change.operation == "put" and change.entry is not None:
            handle = self._index_contract_entry_locked(change.entry)
            record = self._contract_records_by_key.get(change.key)
            if handle is None or record is None:
                parsed = parse_concord_contract_key(change.key)
                contract_id, generation = parsed if parsed is not None else ("", 0)
                events.append(
                    ConcordEvent(
                        ConcordEventType.CONTRACT_INVALID,
                        reason=self._invalid_contracts_by_key[change.key][1],
                        change=change,
                    )
                )
                if contract_id and generation:
                    self._last_status_by_contract_key.pop(change.key, None)
                return tuple(events)
            event_type = (
                ConcordEventType.CONTRACT_PROPOSED
                if previous is None
                else ConcordEventType.CONTRACT_UPDATED
            )
            if record.state == ContractState.CANCELLED:
                event_type = ConcordEventType.CONTRACT_CANCELLED
            events.append(
                ConcordEvent(
                    event_type,
                    contract=handle,
                    record=record,
                    profile=handle.profile,
                    reason=record.cancel_reason if record.state == ContractState.CANCELLED else None,
                    change=change,
                )
            )
            status_event = self._status_event_locked(handle, change=change)
            if status_event is not None:
                events.append(status_event)
            return tuple(events)
        if change.operation in {"delete", "expire"}:
            self._last_status_by_contract_key.pop(change.key, None)
            events.append(
                ConcordEvent(
                    ConcordEventType.CONTRACT_DELETED,
                    contract=previous,
                    record=previous_record,
                    profile=previous.profile if previous is not None else None,
                    reason=change.operation,
                    change=change,
                )
            )
        return tuple(events)

    def _apply_token_change_locked(self, change: KvChange) -> tuple[ConcordEvent, ...]:
        current_revision = self._token_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            return ()
        previous = self._tokens_by_key.get(change.key)
        parsed = parse_concord_participant_token_key(change.key)
        contract_key: str | None = None
        contract: ContractHandle | None = None
        if parsed is not None:
            contract_id, generation, participant = parsed
            contract_key = self._contract_keys_by_pointer.get((contract_id, generation))
            if contract_key is not None:
                contract = self._contract_handles_by_key.get(contract_key)
        else:
            participant = None
        self._remove_token_key_locked(change.key)
        self._token_revision_by_key[change.key] = change.revision
        events: list[ConcordEvent] = []
        if change.operation == "put" and change.entry is not None:
            token = self._index_token_entry_locked(change.entry)
            if token is None:
                events.append(
                    ConcordEvent(
                        ConcordEventType.CONTRACT_INVALID,
                        contract=contract,
                        profile=contract.profile if contract is not None else None,
                        reason=self._invalid_tokens_by_key[change.key][1],
                        change=change,
                    )
                )
            else:
                contract_key = self._contract_keys_by_pointer.get(
                    (token.contract_id, token.generation)
                )
                contract = (
                    self._contract_handles_by_key.get(contract_key)
                    if contract_key is not None
                    else None
                )
                events.append(
                    ConcordEvent(
                        ConcordEventType.TOKEN_ATTACHED
                        if previous is None
                        else ConcordEventType.TOKEN_REFRESHED,
                        contract=contract,
                        token=token,
                        profile=contract.profile if contract is not None else None,
                        participant=token.participant,
                        change=change,
                    )
                )
        elif change.operation in {"delete", "expire"}:
            events.append(
                ConcordEvent(
                    ConcordEventType.TOKEN_EXPIRED
                    if change.operation == "expire"
                    else ConcordEventType.TOKEN_WITHDRAWN,
                    contract=contract,
                    token=previous,
                    profile=contract.profile if contract is not None else None,
                    participant=(
                        previous.participant
                        if previous is not None
                        else participant
                    ),
                    reason=change.operation,
                    change=change,
                )
            )
        if contract is not None:
            status_event = self._status_event_locked(contract, change=change)
            if status_event is not None:
                events.append(status_event)
        return tuple(events)

    def _apply_maintenance_change_locked(self, change: KvChange) -> None:
        current_revision = self._maintenance_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            return
        self._maintenance_entries_by_key.pop(change.key, None)
        self._maintenance_records_by_key.pop(change.key, None)
        self._maintenance_revision_by_key[change.key] = change.revision
        if change.operation == "put" and change.entry is not None:
            self._index_maintenance_entry_locked(change.entry)

    def _index_contract_entry_locked(self, entry: KvEntry) -> ContractHandle | None:
        self._contract_entries_by_key[entry.key] = entry
        self._contract_revision_by_key[entry.key] = entry.revision
        parsed = parse_concord_contract_key(entry.key)
        if parsed is None:
            self._invalid_contracts_by_key[entry.key] = (
                entry,
                "contract key is not a Concord contract key",
            )
            return None
        contract_id, generation = parsed
        try:
            record = ContractRecord.model_validate(entry.value)
        except ValueError as exc:
            self._invalid_contracts_by_key[entry.key] = (entry, str(exc))
            return None
        if record.contract_id != contract_id or record.generation != generation:
            self._invalid_contracts_by_key[entry.key] = (
                entry,
                "contract key and record identity differ",
            )
            return None
        handle = _contract_handle(entry.key, record, entry.revision)
        self._contract_records_by_key[entry.key] = record
        self._contract_handles_by_key[entry.key] = handle
        self._contract_keys_by_pointer[(record.contract_id, record.generation)] = entry.key
        self._contract_keys_by_contract_id.setdefault(record.contract_id, set()).add(
            entry.key
        )
        self._contract_keys_by_profile.setdefault(record.profile, set()).add(entry.key)
        self._contract_keys_by_state.setdefault(record.state, set()).add(entry.key)
        for participant in record.participants:
            self._contract_keys_by_participant.setdefault(str(participant), set()).add(
                entry.key
            )
        return handle

    def _remove_contract_key_locked(self, key: str) -> None:
        record = self._contract_records_by_key.pop(key, None)
        self._contract_entries_by_key.pop(key, None)
        self._invalid_contracts_by_key.pop(key, None)
        self._contract_handles_by_key.pop(key, None)
        if record is None:
            return
        self._contract_keys_by_pointer.pop((record.contract_id, record.generation), None)
        _discard_index_key(self._contract_keys_by_contract_id, record.contract_id, key)
        _discard_index_key(self._contract_keys_by_profile, record.profile, key)
        _discard_index_key(self._contract_keys_by_state, record.state, key)
        for participant in record.participants:
            _discard_index_key(
                self._contract_keys_by_participant,
                str(participant),
                key,
            )

    def _index_token_entry_locked(self, entry: KvEntry) -> ParticipantHandle | None:
        self._token_entries_by_key[entry.key] = entry
        self._token_revision_by_key[entry.key] = entry.revision
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            self._invalid_tokens_by_key[entry.key] = (
                entry,
                "token key is not a Concord participant token key",
            )
            return None
        contract_id, generation, participant = parsed
        try:
            record = ParticipantTokenRecord.model_validate(entry.value)
        except ValueError as exc:
            self._invalid_tokens_by_key[entry.key] = (entry, str(exc))
            return None
        if (
            record.contract_id != contract_id
            or record.generation != generation
            or record.participant != participant
        ):
            self._invalid_tokens_by_key[entry.key] = (
                entry,
                "token key and record identity differ",
            )
            return None
        token = _participant_handle(entry.key, record, entry.revision)
        self._token_records_by_key[entry.key] = record
        self._tokens_by_key[entry.key] = token
        self._token_keys_by_contract.setdefault((contract_id, generation), set()).add(
            entry.key
        )
        self._token_key_by_contract_participant[
            (contract_id, generation, str(participant))
        ] = entry.key
        return token

    def _remove_token_key_locked(self, key: str) -> None:
        record = self._token_records_by_key.pop(key, None)
        self._token_entries_by_key.pop(key, None)
        self._invalid_tokens_by_key.pop(key, None)
        self._tokens_by_key.pop(key, None)
        if record is None:
            return
        _discard_index_key(
            self._token_keys_by_contract,
            (record.contract_id, record.generation),
            key,
        )
        self._token_key_by_contract_participant.pop(
            (record.contract_id, record.generation, str(record.participant)),
            None,
        )

    def _index_maintenance_entry_locked(self, entry: KvEntry) -> None:
        self._maintenance_entries_by_key[entry.key] = entry
        self._maintenance_revision_by_key[entry.key] = entry.revision
        try:
            record = ConcordStaleObservationRecord.model_validate(entry.value)
        except ValueError:
            return
        self._maintenance_records_by_key[entry.key] = record

    def _status_event_locked(
        self,
        contract: ContractHandle,
        *,
        change: KvChange | None,
    ) -> ConcordEvent | None:
        validity = self._validate_from_cache_locked(contract)
        previous = self._last_status_by_contract_key.get(contract.key)
        self._last_status_by_contract_key[contract.key] = validity.status
        if previous == validity.status:
            return None
        return ConcordEvent(
            _concord_event_type(validity),
            contract=contract,
            record=validity.contract,
            validity=validity,
            profile=contract.profile,
            reason=validity.reason,
            change=change,
        )

    def _subscriber_deliveries_locked(
        self,
        events: tuple[ConcordEvent, ...],
    ) -> tuple[tuple[_ConcordSubscriber, ConcordEvent], ...]:
        deliveries: list[tuple[_ConcordSubscriber, ConcordEvent]] = []
        for event in events:
            for subscriber in tuple(self._subscribers):
                if not _event_matches_subscriber(subscriber, event):
                    continue
                if subscriber.replay_pending:
                    subscriber.pending_events.append(event)
                    continue
                deliveries.append((subscriber, event))
        return tuple(deliveries)

    async def _publish_events(
        self,
        events: tuple[ConcordEvent, ...],
        deliveries: tuple[tuple[_ConcordSubscriber, ConcordEvent], ...],
    ) -> None:
        for event in events:
            _log_concord_event(event)
        for subscriber, event in deliveries:
            try:
                subscriber.send.send_nowait(event)
            except anyio.WouldBlock:
                logger.warning(
                    "Concord watcher buffer full profile=%s event=%s",
                    subscriber.profile,
                    event.event_type.value,
                )
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.discard(subscriber)

    def _validate_from_cache_locked(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        record = self._contract_records_by_key.get(contract.key)
        if record is None:
            if contract.key in self._invalid_contracts_by_key:
                return ContractValidity(
                    ContractValidityStatus.INVALID_CONTRACT,
                    reason=self._invalid_contracts_by_key[contract.key][1],
                )
            return ContractValidity(ContractValidityStatus.MISSING_CONTRACT)
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            return ContractValidity(
                ContractValidityStatus.INVALID_CONTRACT,
                contract=record,
                reason="contract handle and cached record identity differ",
            )
        if record.state == ContractState.CANCELLED:
            return ContractValidity(ContractValidityStatus.CANCELLED, contract=record)

        attached_participants = {str(item) for item in record.attached_participants}
        pending_participant: str | None = None
        tokens: dict[str, ParticipantHandle] = {}
        for participant in record.participants:
            participant_key = str(participant)
            token_key = self._token_key_by_contract_participant.get(
                (record.contract_id, record.generation, participant_key)
            )
            if token_key is None:
                token_key = concord_participant_token_key(
                    contract_id=record.contract_id,
                    generation=record.generation,
                    participant=participant,
                )
            token = (
                self._tokens_by_key.get(token_key)
                if token_key is not None
                else None
            )
            if token is None:
                token_entry = (
                    self._token_entries_by_key.get(token_key)
                    if token_key is not None
                    else None
                )
                if token_entry is not None:
                    try:
                        token_record = ParticipantTokenRecord.model_validate(
                            token_entry.value
                        )
                    except ValueError as exc:
                        return ContractValidity(
                            ContractValidityStatus.INVALID_TOKEN,
                            contract=record,
                            tokens=tokens,
                            reason=str(exc),
                        )
                    tokens[participant_key] = _participant_handle(
                        token_key,
                        token_record,
                        token_entry.revision,
                    )
                    status = _token_validity_status(
                        token_record,
                        contract=record,
                        participant=participant,
                        current_sessions=current_sessions,
                    )
                    if status is not None:
                        return ContractValidity(status, contract=record, tokens=tokens)
                    return ContractValidity(
                        ContractValidityStatus.INVALID_TOKEN,
                        contract=record,
                        tokens=tokens,
                        reason="token key and record identity differ",
                    )
                if token_key in self._invalid_tokens_by_key:
                    return ContractValidity(
                        ContractValidityStatus.INVALID_TOKEN,
                        contract=record,
                        tokens=tokens,
                        reason=self._invalid_tokens_by_key[token_key][1],
                    )
                if participant_key in attached_participants:
                    return ContractValidity(
                        ContractValidityStatus.MISSING_TOKEN,
                        contract=record,
                        tokens=tokens,
                        reason=participant_key,
                    )
                pending_participant = pending_participant or participant_key
                continue
            token_record = self._token_records_by_key.get(token.key)
            if token_record is None:
                return ContractValidity(
                    ContractValidityStatus.INVALID_TOKEN,
                    contract=record,
                    tokens=tokens,
                    reason=token.key,
                )
            tokens[participant_key] = token
            status = _token_validity_status(
                token_record,
                contract=record,
                participant=participant,
                current_sessions=current_sessions,
            )
            if status is not None:
                return ContractValidity(status, contract=record, tokens=tokens)
            if participant_key not in attached_participants:
                pending_participant = pending_participant or participant_key
        if pending_participant is not None:
            return ContractValidity(
                ContractValidityStatus.NOT_YET_FULFILLED,
                contract=record,
                tokens=tokens,
                reason=pending_participant,
            )
        return ContractValidity(ContractValidityStatus.VALID, contract=record, tokens=tokens)

    async def _select_or_create_agreement_contract(
        self,
        spec: ConcordAgreementSpec,
    ) -> tuple[ContractHandle, ContractValidity]:
        current_sessions = await _agreement_current_sessions(spec)
        next_generation = 1
        reusable: tuple[ContractHandle, ContractValidity] | None = None
        open_conflicts: list[tuple[ContractHandle, str]] = []
        if spec.stable_contract_id is not None:
            for contract in await self._find_contracts(
                contract_id=spec.stable_contract_id,
            ):
                record = await self._contract_record(contract)
                if record is None:
                    continue
                next_generation = max(next_generation, record.generation + 1)
                if record.state != ContractState.OPEN:
                    continue
                if not _agreement_record_matches_spec(record, spec):
                    open_conflicts.append(
                        (contract, "concord_agreement_conflicting_generation")
                    )
                    continue
                validity = await self._validate(
                    contract,
                    current_sessions=current_sessions,
                    log_label=spec.log_label,
                    log_invalid=False,
                )
                if _agreement_successor_status(validity.status):
                    open_conflicts.append(
                        (
                            contract,
                            f"concord_agreement_{validity.status.value}",
                        )
                    )
                    continue
                if reusable is None or contract.generation > reusable[0].generation:
                    if reusable is not None:
                        open_conflicts.append(
                            (
                                reusable[0],
                                "concord_agreement_superseded_generation",
                            )
                        )
                    reusable = (contract, validity)
                    continue
                open_conflicts.append(
                    (contract, "concord_agreement_superseded_generation")
                )

        for contract, reason in open_conflicts:
            try:
                await self._cancel(
                    contract,
                    spec.local_participant,
                    reason=reason,
                    log_label=spec.log_label,
                )
            except ConcordConflict:
                return await self._select_or_create_agreement_contract(spec)

        if reusable is not None:
            return reusable

        supersedes = (
            ContractPointer(
                contractId=spec.stable_contract_id,
                generation=next_generation - 1,
            )
            if spec.stable_contract_id is not None and next_generation > 1
            else None
        )
        try:
            contract = await self._create_contract(
                spec.participants,
                contract_id=spec.stable_contract_id,
                generation=next_generation,
                profile=spec.profile,
                terms=spec.terms,
                created_by=spec.created_by,
                supersedes=supersedes,
                log_label=spec.log_label,
            )
        except ConcordConflict:
            if spec.stable_contract_id is None:
                raise
            return await self._select_or_create_agreement_contract(spec)
        validity = await self._validate(
            contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
            log_invalid=False,
        )
        return contract, validity

    def _agreement_from_contract(
        self,
        spec: ConcordAgreementSpec,
        contract: ContractHandle,
        validity: ContractValidity,
    ) -> ConcordAgreementLease:
        lease = self._participant_lease(
            contract=contract,
            participant=spec.local_participant,
            session_id=spec.local_session_id,
            refresh_interval=spec.refresh_interval,
            log_label=spec.log_label,
        )
        existing = validity.tokens.get(str(spec.local_participant))
        if existing is not None and existing.session_id == spec.local_session_id:
            lease.adopt(existing)
        return ConcordAgreementLease(
            self,
            spec=spec,
            contract=contract,
            lease=lease,
            validity=validity,
        )

    async def _refresh_agreement(
        self,
        agreement: ConcordAgreementLease,
    ) -> ContractValidity:
        if agreement.closed:
            raise ConcordConflict("Concord agreement is closed")
        spec = agreement.spec
        current_sessions = await _agreement_current_sessions(spec)
        validity = await self._validate(
            agreement.contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
        )
        agreement._validity = validity  # noqa: SLF001
        if validity.status == ContractValidityStatus.UNAVAILABLE:
            return validity
        if _agreement_successor_status(validity.status):
            await agreement._lease.aclose()  # noqa: SLF001
            return validity
        existing = validity.tokens.get(str(spec.local_participant))
        if existing is not None:
            if existing.session_id != spec.local_session_id:
                validity = ContractValidity(
                    ContractValidityStatus.SESSION_MISMATCH,
                    contract=validity.contract,
                    tokens=validity.tokens,
                    reason=str(spec.local_participant),
                )
                agreement._validity = validity  # noqa: SLF001
                await agreement._lease.aclose()  # noqa: SLF001
                return validity
            agreement._lease.adopt(existing)  # noqa: SLF001
        try:
            await agreement._lease.attach_or_refresh()  # noqa: SLF001
        except ConcordConflict:
            validity = await self._validate(
                agreement.contract,
                current_sessions=current_sessions,
                log_label=spec.log_label,
            )
            agreement._validity = validity  # noqa: SLF001
            if _agreement_successor_status(validity.status):
                await agreement._lease.aclose()  # noqa: SLF001
            raise
        validity = await self._validate(
            agreement.contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
        )
        agreement._validity = validity  # noqa: SLF001
        return validity

    async def _cancel_agreement(
        self,
        agreement: ConcordAgreementLease,
        *,
        reason: str | None,
    ) -> bool:
        await agreement.aclose()
        cancelled = await self._cancel(
            agreement.contract,
            agreement.spec.local_participant,
            reason=reason,
            log_label=agreement.spec.log_label,
        )
        cache_key = _agreement_cache_key(agreement.spec)
        if cache_key is not None and self._agreements.get(cache_key) is agreement:
            self._agreements.pop(cache_key, None)
        validity = await self._validate(
            agreement.contract,
            current_sessions=await _agreement_current_sessions(agreement.spec),
            log_label=agreement.spec.log_label,
            log_invalid=False,
        )
        agreement._validity = validity  # noqa: SLF001
        return cancelled

    async def _create_contract(
        self,
        participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress],
        *,
        contract_id: str | None = None,
        generation: int = 1,
        profile: str | None = None,
        terms: Mapping[str, Any] | DeckrModel | None = None,
        created_by: str | EndpointAddress | None = None,
        supersedes: ContractPointer | Mapping[str, Any] | None = None,
        log_label: str = "Concord",
    ) -> ContractHandle:
        contract = await self._coordinator.create_contract(
            participants,
            contract_id=contract_id,
            generation=generation,
            profile=profile,
            terms=terms,
            created_by=created_by,
            supersedes=supersedes,
        )
        entry = self._coordinator._contract_bucket.get_cached(contract.key)  # noqa: SLF001
        if entry is not None:
            await self._apply_contract_change(
                KvChange(self.contract_bucket, contract.key, entry.revision, "put", entry)
            )
        logger.log(
            _contract_lifecycle_log_level(contract.profile),
            "%s Concord contract opened profile=%s contract=%s generation=%s "
            "participants=%s revision=%s created_by=%s",
            log_label,
            contract.profile,
            contract.contract_id,
            contract.generation,
            [str(item) for item in contract.participants],
            contract.revision,
            created_by,
        )
        return contract

    async def get_contract(
        self,
        pointer: ContractPointer | Mapping[str, Any],
    ) -> ContractHandle | None:
        if self._started:
            await self.wait_current()
        parsed = (
            pointer
            if isinstance(pointer, ContractPointer)
            else ContractPointer.model_validate(pointer)
        )
        async with self._lock:
            key = self._contract_keys_by_pointer.get(
                (parsed.contract_id, parsed.generation)
            )
            return (
                self._contract_handles_by_key.get(key)
                if key is not None
                else None
            )

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        if self._started:
            await self.wait_current()
        return await self._contract_record(contract)

    async def contracts(
        self,
        profile: str | None = None,
        *,
        contract_id: str | None = None,
        participant: str | EndpointAddress | None = None,
        state: ContractState | None = None,
    ) -> tuple[ContractHandle, ...]:
        if self._started:
            await self.wait_current()
        return await self._find_contracts(
            profile,
            contract_id=contract_id,
            participant=participant,
            state=state,
        )

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        reason: str | None = None,
        log_label: str = "Concord",
    ) -> bool:
        if self._started:
            await self.wait_ready()
        return await self._cancel(
            contract,
            participant,
            reason=reason,
            log_label=log_label,
        )

    async def attach(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> ConcordParticipantLease:
        if self._started:
            await self.wait_ready()
        lease = self._participant_lease(
            contract=contract,
            participant=participant,
            session_id=session_id,
            token_id=token_id,
            ttl_seconds=ttl_seconds,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )
        await lease.attach_or_refresh()
        async with self._lock:
            self._participant_leases.add(lease)
        if self._task_group is not None:
            lease.start(self._task_group)
        return lease

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        if self._started:
            await self.wait_ready()
        return await self._validate(contract, current_sessions=current_sessions)

    async def validate_exact(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        if self._started:
            await self.wait_ready()
        return await self._coordinator.validate(
            contract,
            current_sessions=current_sessions,
        )

    async def maintenance_cancel_contract(
        self,
        contract: ContractHandle,
        *,
        reason: str = CONCORD_REAPER_STALE_CONTRACT_REASON,
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> bool:
        cancelled = await self._coordinator.maintenance_cancel(
            contract,
            reason=reason,
            now=now,
        )
        if cancelled:
            entry = self._coordinator._contract_bucket.get_cached(contract.key)  # noqa: SLF001
            if entry is not None:
                await self._apply_contract_change(
                    KvChange(
                        self.contract_bucket,
                        contract.key,
                        entry.revision,
                        "put",
                        entry,
                    )
                )
            logger.log(
                _contract_lifecycle_log_level(contract.profile),
                "%s Concord maintenance cancelled contract profile=%s contract=%s "
                "generation=%s cancelled_by=%s reason=%s revision=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                CONCORD_MAINTENANCE_ACTOR,
                reason,
                contract.revision,
            )
        return cancelled

    async def maintenance_delete_cancelled_contract(
        self,
        contract: ContractHandle,
        *,
        retention_seconds: float = float(
            DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS
        ),
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> ConcordMaintenanceDeletionResult:
        now = now or _now_utc()
        current = await self._coordinator._contract_bucket.get(contract.key)  # noqa: SLF001
        if current is None:
            return ConcordMaintenanceDeletionResult(deleted=False)
        record = ContractRecord.model_validate(current.value)
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            raise ConcordConflict(f"Concord contract {contract.key!r} changed identity")
        if record.state != ContractState.CANCELLED:
            return ConcordMaintenanceDeletionResult(deleted=False)
        if record.cancelled_at is None or (
            now - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < retention_seconds:
            return ConcordMaintenanceDeletionResult(deleted=False)

        validity = await self._coordinator.validate(contract)
        token_entries = await _concord_participant_token_entries(
            self._coordinator._token_bucket,  # noqa: SLF001
            contract_id=contract.contract_id,
            generation=contract.generation,
        )
        _log_concord_contract_deletion_audit(
            log_label=log_label,
            contract_key=contract.key,
            record=record,
            state_revision=current.revision,
            validation_status=validity.status,
            token_entries=token_entries,
            deleted_token_key_count=len(token_entries),
        )
        try:
            await self._coordinator._contract_bucket.delete(  # noqa: SLF001
                contract.key,
                revision=current.revision,
            )
        except ConcordConflict:
            logger.info(
                "%s Concord maintenance delete conflict; leaving contract "
                "for next scan contract_key=%s contract=%s generation=%s "
                "revision=%s",
                log_label,
                contract.key,
                contract.contract_id,
                contract.generation,
                current.revision,
                exc_info=True,
            )
            return ConcordMaintenanceDeletionResult(deleted=False)

        deleted_token_count = await _delete_concord_participant_token_entries(
            self._coordinator._token_bucket,  # noqa: SLF001
            token_entries,
        )
        return ConcordMaintenanceDeletionResult(
            deleted=True,
            deleted_token_key_count=deleted_token_count,
        )

    async def _contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        async with self._lock:
            record = self._contract_records_by_key.get(contract.key)
            if record is None:
                return None
            if (
                record.contract_id != contract.contract_id
                or record.generation != contract.generation
            ):
                return None
            return record

    async def _find_contracts(
        self,
        profile: str | None = None,
        *,
        contract_id: str | None = None,
        participant: str | EndpointAddress | None = None,
        state: ContractState | None = None,
    ) -> tuple[ContractHandle, ...]:
        if self._started:
            await self.wait_current()
        if contract_id is not None:
            contract_id = _require_text(
                contract_id,
                field_name="Concord contract id",
            )
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        async with self._lock:
            keys: set[str] = set(self._contract_handles_by_key)
            if profile is not None:
                keys &= set(self._contract_keys_by_profile.get(profile, ()))
            if contract_id is not None:
                keys &= set(self._contract_keys_by_contract_id.get(contract_id, ()))
            if parsed_participant is not None:
                keys &= set(
                    self._contract_keys_by_participant.get(str(parsed_participant), ())
                )
            if state is not None:
                keys &= set(self._contract_keys_by_state.get(state, ()))
            return tuple(
                self._contract_handles_by_key[key]
                for key in sorted(keys)
                if key in self._contract_handles_by_key
            )

    async def _attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
        log_label: str = "Concord",
    ) -> ParticipantHandle:
        token = await self._coordinator.attach(
            contract,
            participant,
            session_id,
            token_id=token_id,
            ttl_seconds=ttl_seconds,
        )
        contract_entry = self._coordinator._contract_bucket.get_cached(  # noqa: SLF001
            contract.key
        )
        if contract_entry is not None:
            await self._apply_contract_change(
                KvChange(
                    self.contract_bucket,
                    contract.key,
                    contract_entry.revision,
                    "put",
                    contract_entry,
                )
            )
        token_entry = self._coordinator._token_bucket.get_cached(token.key)  # noqa: SLF001
        if token_entry is not None:
            await self._apply_token_change(
                KvChange(self.token_bucket, token.key, token_entry.revision, "put", token_entry)
            )
        logger.log(
            _contract_lifecycle_log_level(contract.profile),
            "%s Concord participant token attached profile=%s contract=%s "
            "generation=%s participant=%s session=%s token=%s refresh=%s "
            "revision=%s ttl=%s",
            log_label,
            contract.profile,
            contract.contract_id,
            contract.generation,
            token.participant,
            token.session_id,
            token.token_id,
            token.refresh_seq,
            token.revision,
            token.ttl_seconds,
        )
        return token

    async def _refresh_token(
        self,
        handle: ParticipantHandle,
        *,
        log_label: str = "Concord",
    ) -> ParticipantHandle:
        refreshed = await self._coordinator.refresh(handle)
        token_entry = self._coordinator._token_bucket.get_cached(refreshed.key)  # noqa: SLF001
        if token_entry is not None:
            await self._apply_token_change(
                KvChange(
                    self.token_bucket,
                    refreshed.key,
                    token_entry.revision,
                    "put",
                    token_entry,
                )
            )
        logger.debug(
            "%s Concord participant token heartbeat contract=%s generation=%s "
            "participant=%s session=%s token=%s refresh=%s revision=%s ttl=%s",
            log_label,
            refreshed.contract_id,
            refreshed.generation,
            refreshed.participant,
            refreshed.session_id,
            refreshed.token_id,
            refreshed.refresh_seq,
            refreshed.revision,
            refreshed.ttl_seconds,
        )
        return refreshed

    async def _withdraw_token(
        self,
        handle: ParticipantHandle,
        *,
        log_label: str = "Concord",
    ) -> bool:
        withdrawn = await self._coordinator.withdraw(handle)
        if not withdrawn:
            return False
        marker_revision = self._coordinator._token_bucket.revision_cached(  # noqa: SLF001
            handle.key
        ) or (handle.revision + 1)
        await self._apply_token_change(
            KvChange(self.token_bucket, handle.key, marker_revision, "delete")
        )
        logger.debug(
            "%s Concord participant token withdrawn contract=%s generation=%s "
            "participant=%s session=%s token=%s revision=%s",
            log_label,
            handle.contract_id,
            handle.generation,
            handle.participant,
            handle.session_id,
            handle.token_id,
            marker_revision,
        )
        return True

    async def _validate_participant_token(
        self,
        handle: ParticipantHandle,
    ) -> ParticipantHandle:
        return await self._coordinator.validate_participant_handle(handle)

    async def _cancel(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
        log_label: str = "Concord",
    ) -> bool:
        cancelled = await self._coordinator.cancel(
            contract,
            participant,
            reason=reason,
        )
        if cancelled:
            entry = self._coordinator._contract_bucket.get_cached(contract.key)  # noqa: SLF001
            if entry is not None:
                await self._apply_contract_change(
                    KvChange(
                        self.contract_bucket,
                        contract.key,
                        entry.revision,
                        "put",
                        entry,
                    )
                )
            logger.log(
                _contract_lifecycle_log_level(contract.profile),
                "%s Concord contract cancelled profile=%s contract=%s generation=%s "
                "participant=%s reason=%s revision=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                parse_endpoint_address(participant),
                reason,
                contract.revision,
            )
        return cancelled

    async def _validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
        log_label: str = "Concord",
        log_invalid: bool = True,
    ) -> ContractValidity:
        if self._started and not self.is_current():
            validity = ContractValidity(ContractValidityStatus.UNAVAILABLE)
        else:
            async with self._lock:
                validity = self._validate_from_cache_locked(
                    contract,
                    current_sessions=current_sessions,
                )
        if log_invalid and validity.status in {
            ContractValidityStatus.MISSING_TOKEN,
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityStatus.GENERATION_MISMATCH,
            ContractValidityStatus.SESSION_MISMATCH,
            ContractValidityStatus.TERMS_HASH_MISMATCH,
        }:
            logger.log(
                _contract_invalid_log_level(contract.profile, validity.status),
                "%s Concord contract invalid profile=%s contract=%s generation=%s "
                "status=%s reason=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                validity.status.value,
                validity.reason,
            )
        return validity

    def _participant_lease(
        self,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> ConcordParticipantLease:
        return ConcordParticipantLease(
            self,
            contract=contract,
            participant=participant,
            session_id=session_id,
            token_id=token_id,
            ttl_seconds=ttl_seconds,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )

    async def _forget_participant_lease(self, lease: ConcordParticipantLease) -> None:
        async with self._lock:
            self._participant_leases.discard(lease)

    @asynccontextmanager
    async def watch(
        self,
        profile: str | None = None,
        *,
        participant: str | EndpointAddress | None = None,
        replay_current: bool = True,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ConcordEvent]]:
        if self._started:
            await self.wait_current()
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        send, receive = anyio.create_memory_object_stream[ConcordEvent](
            max_buffer_size=self._buffer_size
        )
        subscriber = _ConcordSubscriber(
            send,
            profile,
            parsed_participant,
            replay_pending=replay_current,
        )
        initial: tuple[ConcordEvent, ...] = ()
        async with self._lock:
            self._subscribers.add(subscriber)
            if replay_current:
                initial = tuple(
                    event
                    for event in (
                        ConcordEvent(
                            _concord_event_type(
                                self._validate_from_cache_locked(contract)
                            ),
                            contract=contract,
                            record=self._contract_records_by_key.get(contract.key),
                            validity=self._validate_from_cache_locked(contract),
                            profile=contract.profile,
                        )
                        for contract in self._contract_handles_by_key.values()
                    )
                    if _event_matches_subscriber(subscriber, event)
                )
        try:
            async with send, receive:
                for event in initial:
                    await send.send(event)
                if replay_current:
                    await self._finish_subscriber_replay(subscriber)
                yield receive
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)

    async def _finish_subscriber_replay(
        self,
        subscriber: _ConcordSubscriber,
    ) -> None:
        while True:
            async with self._lock:
                pending = tuple(subscriber.pending_events)
                subscriber.pending_events.clear()
                if not pending:
                    subscriber.replay_pending = False
                    return
            for event in pending:
                await subscriber.send.send(event)


STALE_OPEN_CONTRACT_STATUSES = frozenset(
    {
        ContractValidityStatus.NOT_YET_FULFILLED,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.INVALID_CONTRACT,
    }
)


class ConcordReaperService:
    """Periodic Concord-only maintenance for stale and cancelled contracts."""

    def __init__(
        self,
        concord: Concord,
        *,
        config: ConcordReaperConfig | Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._concord = concord
        self._contract_bucket = concord._coordinator._contract_bucket  # noqa: SLF001
        self._token_bucket = concord._coordinator._token_bucket  # noqa: SLF001
        self._maintenance_bucket = concord._maintenance_bucket  # noqa: SLF001
        self._config = (
            config
            if isinstance(config, ConcordReaperConfig)
            else ConcordReaperConfig.model_validate(dict(config or {}))
        )
        self._clock = clock
        self._closed = False

    @property
    def config(self) -> ConcordReaperConfig:
        return self._config

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        del task_group

    async def aclose(self) -> None:
        self._closed = True

    async def run(self, *, stop_event: anyio.Event | None = None) -> None:
        while not self._closed:
            try:
                await self.scan_once()
            except ConcordUnavailable:
                logger.warning(
                    "%s Concord reaper state unavailable; scan will retry",
                    self._config.log_label,
                    exc_info=True,
                )
            if self._closed or (stop_event is not None and stop_event.is_set()):
                return
            if stop_event is None:
                await anyio.sleep(self._config.scan_interval_seconds)
                continue
            with anyio.move_on_after(self._config.scan_interval_seconds):
                await stop_event.wait()
            if stop_event.is_set():
                return

    async def scan_once(self) -> ConcordReaperScanResult:
        now = _ensure_utc(self._clock())
        counts: dict[str, int] = {
            "scanned_contract_count": 0,
            "stale_observations_created": 0,
            "stale_observations_cleared": 0,
            "contracts_cancelled": 0,
            "contracts_deleted": 0,
            "token_keys_deleted": 0,
        }
        for entry in await self._contract_bucket.items_exact(concord_contracts_prefix()):
            parsed = parse_concord_contract_key(entry.key)
            if parsed is None:
                continue
            counts["scanned_contract_count"] += 1
            contract_id, generation = parsed
            entry_counts = await self._scan_contract_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
            )
            for key, value in entry_counts.items():
                counts[key] += value
        counts["stale_observations_cleared"] += (
            await self._clear_orphaned_stale_observations()
        )
        stale_observation_count = len(
            await self._maintenance_bucket.items_exact("stale.")
        )
        return ConcordReaperScanResult(
            scanned_contract_count=counts["scanned_contract_count"],
            stale_observation_count=stale_observation_count,
            stale_observations_created=counts["stale_observations_created"],
            stale_observations_cleared=counts["stale_observations_cleared"],
            contracts_cancelled=counts["contracts_cancelled"],
            contracts_deleted=counts["contracts_deleted"],
            token_keys_deleted=counts["token_keys_deleted"],
        )

    async def _scan_contract_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
    ) -> dict[str, int]:
        try:
            record = ContractRecord.model_validate(entry.value)
        except ValueError as exc:
            return await self._scan_invalid_contract_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
                invalid_reason=str(exc),
            )
        if record.contract_id != contract_id or record.generation != generation:
            return await self._scan_invalid_contract_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
                invalid_reason="contract key and record identity differ",
            )
        handle = _contract_handle(entry.key, record, entry.revision)
        if record.state == ContractState.CANCELLED:
            return await self._scan_cancelled_contract(handle, record, now=now)
        return await self._scan_open_contract(handle, now=now)

    async def _scan_open_contract(
        self,
        contract: ContractHandle,
        *,
        now: datetime,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        validity = await self._concord._coordinator.validate(  # noqa: SLF001
            contract,
        )
        if validity.status in STALE_OPEN_CONTRACT_STATUSES:
            first_observed, created = await self._observe_stale(
                contract_id=contract.contract_id,
                generation=contract.generation,
                status=validity.status,
                reason=validity.reason,
                contract_revision=contract.revision,
                now=now,
            )
            if created:
                counts["stale_observations_created"] += 1
            if (
                now - first_observed
            ).total_seconds() >= self._config.stale_grace_seconds:
                try:
                    cancelled = await self._concord.maintenance_cancel_contract(
                        contract,
                        reason=CONCORD_REAPER_STALE_CONTRACT_REASON,
                        log_label=self._config.log_label,
                        now=now,
                    )
                except (ConcordConflict, ValueError):
                    logger.info(
                        "%s Concord reaper stale contract cancel conflict; "
                        "leaving for next scan contract=%s generation=%s "
                        "status=%s",
                        self._config.log_label,
                        contract.contract_id,
                        contract.generation,
                        validity.status.value,
                        exc_info=True,
                    )
                else:
                    if cancelled:
                        counts["contracts_cancelled"] += 1
                    if await self._clear_stale_observation(
                        contract.contract_id,
                        contract.generation,
                    ):
                        counts["stale_observations_cleared"] += 1
            return counts

        if validity.status == ContractValidityStatus.UNAVAILABLE:
            return counts
        if await self._clear_stale_observation(
            contract.contract_id,
            contract.generation,
        ):
            counts["stale_observations_cleared"] += 1
        return counts

    async def _scan_cancelled_contract(
        self,
        contract: ContractHandle,
        record: ContractRecord,
        *,
        now: datetime,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        if await self._clear_stale_observation(contract.contract_id, contract.generation):
            counts["stale_observations_cleared"] += 1
        if record.cancelled_at is None or (
            now - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < self._config.cancelled_retention_seconds:
            return counts
        result = await self._concord.maintenance_delete_cancelled_contract(
            contract,
            retention_seconds=self._config.cancelled_retention_seconds,
            log_label=self._config.log_label,
            now=now,
        )
        if result.deleted:
            counts["contracts_deleted"] += 1
            counts["token_keys_deleted"] += result.deleted_token_key_count
            if await self._clear_stale_observation(
                contract.contract_id,
                contract.generation,
            ):
                counts["stale_observations_cleared"] += 1
        return counts

    async def _scan_invalid_contract_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
        invalid_reason: str,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        raw_state = str(entry.value.get("state", ContractState.OPEN.value))
        if raw_state == ContractState.CANCELLED.value:
            if await self._clear_stale_observation(contract_id, generation):
                counts["stale_observations_cleared"] += 1
            cancelled_at = _datetime_from_raw_value(entry.value.get("cancelledAt"))
            if cancelled_at is None or (
                now - cancelled_at
            ).total_seconds() < self._config.cancelled_retention_seconds:
                return counts
            result = await self._delete_invalid_cancelled_contract(
                entry,
                contract_id=contract_id,
                generation=generation,
                invalid_reason=invalid_reason,
            )
            if result.deleted:
                counts["contracts_deleted"] += 1
                counts["token_keys_deleted"] += result.deleted_token_key_count
            return counts

        first_observed, created = await self._observe_stale(
            contract_id=contract_id,
            generation=generation,
            status=ContractValidityStatus.INVALID_CONTRACT,
            reason=invalid_reason,
            contract_revision=entry.revision,
            now=now,
        )
        if created:
            counts["stale_observations_created"] += 1
        if (
            now - first_observed
        ).total_seconds() < self._config.stale_grace_seconds:
            return counts
        try:
            await self._maintenance_cancel_invalid_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
            )
        except ConcordConflict:
            logger.info(
                "%s Concord reaper invalid contract cancel conflict; "
                "leaving for next scan contract=%s generation=%s",
                self._config.log_label,
                contract_id,
                generation,
                exc_info=True,
            )
            return counts
        counts["contracts_cancelled"] += 1
        if await self._clear_stale_observation(contract_id, generation):
            counts["stale_observations_cleared"] += 1
        return counts

    async def _observe_stale(
        self,
        *,
        contract_id: str,
        generation: int,
        status: ContractValidityStatus,
        reason: str | None,
        contract_revision: int | None,
        now: datetime,
    ) -> tuple[datetime, bool]:
        key = concord_stale_observation_key(
            contract_id=contract_id,
            generation=generation,
        )
        current = await self._maintenance_bucket.get(key)
        if current is not None:
            try:
                record = ConcordStaleObservationRecord.model_validate(current.value)
            except ValueError:
                record = None
            else:
                if (
                    record.contract_id == contract_id
                    and record.generation == generation
                ):
                    return record.first_observed_stale_at, False
        observation = ConcordStaleObservationRecord(
            contractId=contract_id,
            generation=generation,
            firstObservedStaleAt=now,
            status=status,
            reason=reason,
            contractRevision=contract_revision,
        )
        try:
            await self._maintenance_bucket.create(key, observation)
        except ConcordConflict:
            latest = await self._maintenance_bucket.get(key)
            if latest is not None:
                record = ConcordStaleObservationRecord.model_validate(latest.value)
                return record.first_observed_stale_at, False
            await self._maintenance_bucket.create(key, observation)
        return now, True

    async def _clear_stale_observation(
        self,
        contract_id: str,
        generation: int,
    ) -> bool:
        key = concord_stale_observation_key(
            contract_id=contract_id,
            generation=generation,
        )
        current = await self._maintenance_bucket.get(key)
        if current is None:
            return False
        try:
            await self._maintenance_bucket.delete(key, revision=current.revision)
        except ConcordConflict:
            return False
        return True

    async def _clear_orphaned_stale_observations(self) -> int:
        cleared = 0
        for entry in await self._maintenance_bucket.items_exact("stale."):
            try:
                observation = ConcordStaleObservationRecord.model_validate(entry.value)
            except ValueError:
                continue
            contract_entry = await self._contract_bucket.get(
                concord_contract_key(
                    contract_id=observation.contract_id,
                    generation=observation.generation,
                )
            )
            if contract_entry is not None:
                continue
            try:
                await self._maintenance_bucket.delete(entry.key, revision=entry.revision)
            except ConcordConflict:
                continue
            cleared += 1
        return cleared

    async def _maintenance_cancel_invalid_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
    ) -> None:
        value = thaw_json(entry.value)
        updated = dict(value)
        updated.setdefault("contractId", contract_id)
        updated.setdefault("generation", generation)
        updated["state"] = ContractState.CANCELLED.value
        updated["cancelledBy"] = CONCORD_MAINTENANCE_ACTOR
        updated["cancelledAt"] = _datetime_log_value(now)
        updated["cancelRevision"] = entry.revision
        updated["cancelReason"] = CONCORD_REAPER_STALE_CONTRACT_REASON
        await self._contract_bucket.update(entry.key, updated, revision=entry.revision)
        logger.info(
            "%s Concord maintenance cancelled invalid contract contract_key=%s "
            "contract=%s generation=%s cancelled_by=%s reason=%s revision=%s",
            self._config.log_label,
            entry.key,
            contract_id,
            generation,
            CONCORD_MAINTENANCE_ACTOR,
            CONCORD_REAPER_STALE_CONTRACT_REASON,
            entry.revision,
        )

    async def _delete_invalid_cancelled_contract(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        invalid_reason: str,
    ) -> ConcordMaintenanceDeletionResult:
        token_entries = await _concord_participant_token_entries(
            self._token_bucket,
            contract_id=contract_id,
            generation=generation,
        )
        _log_concord_raw_contract_deletion_audit(
            log_label=self._config.log_label,
            contract_key=entry.key,
            contract_id=contract_id,
            generation=generation,
            raw_record=entry.value,
            state_revision=entry.revision,
            validation_status=ContractValidityStatus.INVALID_CONTRACT,
            validation_reason=invalid_reason,
            token_entries=token_entries,
            deleted_token_key_count=len(token_entries),
        )
        try:
            await self._contract_bucket.delete(entry.key, revision=entry.revision)
        except ConcordConflict:
            logger.info(
                "%s Concord maintenance delete conflict; leaving invalid contract "
                "for next scan contract_key=%s contract=%s generation=%s "
                "revision=%s",
                self._config.log_label,
                entry.key,
                contract_id,
                generation,
                entry.revision,
                exc_info=True,
            )
            return ConcordMaintenanceDeletionResult(deleted=False)
        deleted_token_count = await _delete_concord_participant_token_entries(
            self._token_bucket,
            token_entries,
        )
        return ConcordMaintenanceDeletionResult(
            deleted=True,
            deleted_token_key_count=deleted_token_count,
        )


ConcordContractPredicate = Callable[
    [ContractHandle, ContractRecord],
    bool | Awaitable[bool],
]
ConcordCurrentSessions = Callable[
    [ContractHandle],
    Mapping[str, str] | None | Awaitable[Mapping[str, str] | None],
]
ConcordPrepareReconcile = Callable[[], None | Awaitable[None]]
ConcordContractSortKey = Callable[[ContractHandle], Any]
ConcordSessionEvidence = (
    Mapping[str | EndpointAddress, str]
    | Callable[
        [],
        Mapping[str | EndpointAddress, str]
        | Awaitable[Mapping[str | EndpointAddress, str]],
    ]
)


@dataclass(frozen=True, slots=True)
class ConcordAgreementSpec:
    """Owner-side Concord agreement request.

    A stable contract id is used for contracts whose identity is durable across
    generations, such as service-use agreements. Omit it for one-shot claims or
    sessions that should receive a fresh contract id when superseded.
    """

    profile: str | None
    participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress]
    local_participant: str | EndpointAddress
    local_session_id: str
    terms: Mapping[str, Any] | DeckrModel | None = None
    stable_contract_id: str | None = None
    current_sessions: ConcordSessionEvidence | None = None
    refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
    log_label: str = "Concord"
    created_by: str | EndpointAddress | None = None

    def __post_init__(self) -> None:
        participants = tuple(
            sorted(
                (parse_endpoint_address(item) for item in self.participants),
                key=str,
            )
        )
        if not participants:
            raise ValueError("Concord agreements require at least one participant")
        local_participant = parse_endpoint_address(self.local_participant)
        if local_participant not in participants:
            raise ValueError("local_participant must be named by participants")
        if self.refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if self.profile is not None:
            _require_text(self.profile, field_name="Concord agreement profile")
        stable_contract_id = self.stable_contract_id
        if stable_contract_id is not None:
            stable_contract_id = _require_text(
                stable_contract_id,
                field_name="Concord agreement contract id",
            )
        created_by = (
            parse_endpoint_address(self.created_by)
            if self.created_by is not None
            else local_participant
        )
        terms = (
            self.terms.model_dump(by_alias=True, exclude_none=True, mode="json")
            if isinstance(self.terms, DeckrModel)
            else self.terms
        )
        object.__setattr__(self, "participants", participants)
        object.__setattr__(self, "local_participant", local_participant)
        object.__setattr__(
            self,
            "local_session_id",
            _require_text(
                self.local_session_id,
                field_name="Concord agreement session id",
            ),
        )
        object.__setattr__(
            self,
            "terms",
            freeze_json(terms) if terms is not None else None,
        )
        object.__setattr__(self, "stable_contract_id", stable_contract_id)
        object.__setattr__(self, "created_by", created_by)


class ConcordAgreementLease:
    """Core-owned owner-side Concord agreement handle."""

    def __init__(
        self,
        service: Concord,
        *,
        spec: ConcordAgreementSpec,
        contract: ContractHandle,
        lease: ConcordParticipantLease,
        validity: ContractValidity,
    ) -> None:
        self._service = service
        self.spec = spec
        self.contract = contract
        self._lease = lease
        self._validity = validity
        self._closed = False

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    @property
    def generation(self) -> int:
        return self.contract.generation

    @property
    def profile(self) -> str | None:
        return self.contract.profile

    @property
    def validity(self) -> ContractValidity:
        return self._validity

    @property
    def valid(self) -> bool:
        return self._validity.valid

    @property
    def local_token(self) -> ParticipantHandle | None:
        return self._lease.token

    @property
    def closed(self) -> bool:
        return self._closed

    async def refresh(self) -> ContractValidity:
        return await self._service._refresh_agreement(self)  # noqa: SLF001

    async def cancel(self, reason: str | None = None) -> bool:
        return await self._service._cancel_agreement(self, reason=reason)  # noqa: SLF001

    async def aclose(self) -> None:
        self._closed = True
        await self._lease.aclose()


class ConcordParticipant:
    """Owns one local participant's token lifecycle for selected contracts."""

    def __init__(
        self,
        *,
        concord: Concord,
        participant: str | EndpointAddress,
        session_id: str,
        accept_contract: ConcordContractPredicate,
        current_sessions: ConcordCurrentSessions | None = None,
        prepare_reconcile: ConcordPrepareReconcile | None = None,
        contract_sort_key: ConcordContractSortKey | None = None,
        profile: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        reconcile_interval: float = DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS,
        notification_batch_interval: float = DEFAULT_CONCORD_NOTIFICATION_BATCH_SECONDS,
        cancel_terminal_statuses: Collection[ContractValidityStatus] | None = None,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if reconcile_interval <= 0:
            raise ValueError("reconcile_interval must be greater than zero")
        if notification_batch_interval <= 0:
            raise ValueError("notification_batch_interval must be greater than zero")
        self._concord = concord
        self.participant = parse_endpoint_address(participant)
        self.session_id = _require_text(session_id, field_name="Concord session id")
        self.profile = profile
        self._accept_contract = accept_contract
        self._current_sessions = current_sessions
        self._prepare_reconcile = prepare_reconcile
        self._contract_sort_key = contract_sort_key
        self._refresh_interval = refresh_interval
        self._reconcile_interval = reconcile_interval
        self._notifications = CoalescedTrigger(
            batch_interval=notification_batch_interval
        )
        self._cancel_terminal_statuses = frozenset(cancel_terminal_statuses or ())
        self._log_label = log_label
        self._managed: dict[str, ConcordManagedContract] = {}
        self._leases: dict[str, ConcordParticipantLease] = {}
        self._last_status: dict[str, ContractValidityStatus] = {}
        self._subscribers: set[anyio.abc.ObjectSendStream[ConcordManagedContractEvent]] = (
            set()
        )
        self._lock = anyio.Lock()
        self._start_soon: Callable[..., object] | None = None
        self._started = False
        self._closed = False

    @property
    def managed_contracts(self) -> tuple[ConcordManagedContract, ...]:
        return tuple(self._managed[key] for key in sorted(self._managed))

    def managed_contract(self, contract: ContractHandle) -> ConcordManagedContract | None:
        return self._managed.get(contract.key)

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object]) -> None:
        if self._started:
            return
        self._started = True
        self._start_soon = start_soon
        start_soon(self.watch_loop)
        start_soon(self.notification_reconcile_loop)

    @asynccontextmanager
    async def watch(self) -> Any:
        send, receive = anyio.create_memory_object_stream[
            ConcordManagedContractEvent
        ](100)
        self._subscribers.add(send)
        async with send, receive:
            try:
                yield receive
            finally:
                self._subscribers.discard(send)

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            for lease in self._leases.values():
                await lease.aclose()
            self._leases.clear()
            self._managed.clear()
            self._last_status.clear()
        await self._notifications.aclose()

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        reason: str | None = None,
    ) -> bool:
        return await self._concord._cancel(
            contract,
            self.participant,
            reason=reason,
            log_label=self._log_label,
        )

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        sessions: dict[str, str] = {}
        if current_sessions is not None:
            sessions.update(current_sessions)
        sessions[str(self.participant)] = self.session_id
        return await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )

    async def release(
        self,
        contract: ContractHandle | str,
        *,
        reason: str = "released",
    ) -> None:
        key = contract.key if isinstance(contract, ContractHandle) else contract
        async with self._lock:
            await self._release_locked(key, reason=reason)

    async def watch_loop(self) -> None:
        while not self._closed:
            try:
                async with self._concord.watch(
                    self.profile,
                    participant=self.participant,
                ) as stream:
                    await self.reconcile(reason="contract watch warmup")
                    async for event in stream:
                        if self._closed:
                            return
                        await self._notifications.request(
                            f"{event.event_type.value} "
                            f"{event.change.key if event.change else '<snapshot>'}"
                        )
            except ConcordUnavailable:
                await anyio.sleep(self._reconcile_interval)

    async def reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile(reason="periodic reconcile")
            except ConcordUnavailable:
                logger.warning(
                    "%s Concord participant manager unavailable; "
                    "reconciliation will retry profile=%s participant=%s",
                    self._log_label,
                    self.profile,
                    self.participant,
                    exc_info=True,
                )
            await anyio.sleep(self._reconcile_interval)

    async def notification_reconcile_loop(self) -> None:
        await self._notifications.run(
            self._reconcile_notification,
            reason_prefix="contract watch",
        )

    async def _reconcile_notification(self, reason: str) -> None:
        if self._closed:
            return
        try:
            await self.reconcile(reason=reason)
        except ConcordUnavailable:
            logger.warning(
                "%s Concord participant manager unavailable; "
                "notification reconciliation will retry profile=%s participant=%s",
                self._log_label,
                self.profile,
                self.participant,
                exc_info=True,
            )

    async def reconcile(
        self,
        *,
        reason: str = "manual reconcile",
        rebuild_index: bool = False,
    ) -> tuple[ConcordManagedContract, ...]:
        async with self._lock:
            if self._closed:
                return ()
            contracts = await self._reconcile_contract_candidates_locked(
                rebuild_index=rebuild_index,
            )
            if self._prepare_reconcile is not None:
                await _maybe_await(self._prepare_reconcile())
            if self._contract_sort_key is not None:
                contracts = tuple(sorted(contracts, key=self._contract_sort_key))
            next_managed: dict[str, ConcordManagedContract] = {}
            next_leases: dict[str, ConcordParticipantLease] = {}

            for contract in contracts:
                managed = await self._reconcile_contract_locked(
                    contract,
                    reason=reason,
                )
                if managed is None:
                    continue
                next_managed[contract.key] = managed
                lease = self._leases.get(contract.key)
                if lease is not None:
                    next_leases[contract.key] = lease

            for key in tuple(self._leases):
                if key not in next_leases:
                    await self._release_locked(key, reason="not_selected")

            self._managed = next_managed
            self._leases = next_leases
            return self.managed_contracts

    async def _reconcile_contract_candidates_locked(
        self,
        *,
        rebuild_index: bool,
    ) -> tuple[ContractHandle, ...]:
        del rebuild_index
        return await self._concord._find_contracts(
            self.profile,
            participant=self.participant,
        )

    async def _reconcile_contract_locked(
        self,
        contract: ContractHandle,
        *,
        reason: str,
    ) -> ConcordManagedContract | None:
        if self.participant not in contract.participants:
            await self._release_locked(contract.key, reason="participant_not_named")
            return None

        try:
            record = await self._concord._contract_record(contract)
        except ValueError:
            await self._release_locked(
                contract.key,
                reason=ContractValidityStatus.INVALID_CONTRACT.value,
            )
            return None
        if record is None:
            await self._release_locked(
                contract.key,
                reason=ContractValidityStatus.MISSING_CONTRACT.value,
            )
            return None
        if self.profile is not None and record.profile != self.profile:
            await self._release_locked(contract.key, reason="profile_mismatch")
            return None

        if record.state == ContractState.CANCELLED:
            validity = ContractValidity(ContractValidityStatus.CANCELLED, contract=record)
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=None,
                reason=reason,
            )
            await self._release_locked(contract.key, reason=ContractState.CANCELLED.value)
            return None

        if not await _maybe_await(self._accept_contract(contract, record)):
            await self._release_locked(contract.key, reason="policy_rejected")
            return None

        sessions = await self._current_sessions_for(contract)
        validity = await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )
        record = validity.contract or record

        existing = validity.tokens.get(str(self.participant))
        if _terminal_managed_status(validity.status):
            if validity.status in self._cancel_terminal_statuses:
                try:
                    await self.cancel(
                        contract,
                        reason=f"concord_managed_{validity.status.value}",
                    )
                except (ConcordConflict, ConcordUnavailable, ValueError):
                    logger.debug(
                        "%s could not cancel terminal Concord contract %s",
                        self._log_label,
                        contract.contract_id,
                        exc_info=True,
                    )
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=existing,
                reason=reason,
            )
            await self._release_locked(contract.key, reason=validity.status.value)
            return None

        lease = self._leases.get(contract.key)
        if lease is None:
            lease = self._concord._participant_lease(
                contract=contract,
                participant=self.participant,
                session_id=self.session_id,
                refresh_interval=self._refresh_interval,
                log_label=self._log_label,
            )
            if self._start_soon is not None:
                lease.start_soon(self._start_soon)
            self._leases[contract.key] = lease

        if existing is not None:
            if existing.session_id != self.session_id:
                validity = ContractValidity(
                    ContractValidityStatus.SESSION_MISMATCH,
                    contract=record,
                    tokens=validity.tokens,
                    reason=str(self.participant),
                )
                await self._publish_terminal_locked(
                    contract,
                    record=record,
                    validity=validity,
                    token=existing,
                    reason=reason,
                )
                await self._release_locked(
                    contract.key,
                    reason=ContractValidityStatus.SESSION_MISMATCH.value,
                )
                return None
            lease.adopt(existing)

        try:
            token = await lease.attach_or_refresh()
        except ConcordConflict:
            validity = await self._concord._validate(
                contract,
                current_sessions=sessions,
                log_label=self._log_label,
            )
            record = validity.contract or record
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=None,
                reason=reason,
            )
            if _terminal_managed_status(validity.status):
                await self._release_locked(contract.key, reason=validity.status.value)
            return None

        validity = await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )
        record = validity.contract or record
        managed = ConcordManagedContract(
            contract=contract,
            record=record,
            validity=validity,
            token=token,
        )
        if _terminal_managed_status(validity.status):
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=token,
                reason=reason,
            )
            await self._release_locked(contract.key, reason=validity.status.value)
            return None
        self._publish_status(managed, reason=reason)
        return managed

    async def _current_sessions_for(
        self,
        contract: ContractHandle,
    ) -> Mapping[str, str]:
        sessions: dict[str, str] = {}
        if self._current_sessions is not None:
            current = await _maybe_await(self._current_sessions(contract))
            if current is not None:
                sessions.update(current)
        sessions[str(self.participant)] = self.session_id
        return sessions

    async def _release_locked(self, key: str, *, reason: str) -> None:
        managed = self._managed.pop(key, None)
        lease = self._leases.pop(key, None)
        if lease is not None:
            await lease.aclose(withdraw=False)
        self._last_status.pop(key, None)
        if managed is None:
            return
        self._publish(
            ConcordManagedContractEvent(
                ConcordManagedContractEventType.RELEASED,
                managed.contract,
                record=managed.record,
                validity=managed.validity,
                token=managed.token,
                reason=reason,
            )
        )

    async def _publish_terminal_locked(
        self,
        contract: ContractHandle,
        *,
        record: ContractRecord,
        validity: ContractValidity,
        token: ParticipantHandle | None,
        reason: str,
    ) -> None:
        managed = ConcordManagedContract(
            contract=contract,
            record=record,
            validity=validity,
            token=token,
        )
        self._publish_status(managed, reason=reason)

    def _publish_status(
        self,
        managed: ConcordManagedContract,
        *,
        reason: str,
    ) -> None:
        status = managed.validity.status
        previous = self._last_status.get(managed.contract.key)
        self._last_status[managed.contract.key] = status
        if previous == status:
            return
        self._publish(
            ConcordManagedContractEvent(
                _managed_event_type(status),
                managed.contract,
                record=managed.record,
                validity=managed.validity,
                token=managed.token,
                reason=reason,
            )
        )

    def _publish(self, event: ConcordManagedContractEvent) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.send_nowait(event)
            except anyio.WouldBlock:
                continue
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                self._subscribers.discard(subscriber)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _discard_index_key(index: dict[Any, set[str]], value: Any, key: str) -> None:
    keys = index.get(value)
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        index.pop(value, None)


def _event_matches_subscriber(
    subscriber: _ConcordSubscriber,
    event: ConcordEvent,
) -> bool:
    if subscriber.profile is not None and event.profile != subscriber.profile:
        return False
    if subscriber.participant is None:
        return True
    participant = str(subscriber.participant)
    if event.participant is not None and str(event.participant) == participant:
        return True
    if event.contract is not None:
        return participant in {str(item) for item in event.contract.participants}
    if event.record is not None:
        return participant in {str(item) for item in event.record.participants}
    return False


def _agreement_cache_key(spec: ConcordAgreementSpec) -> tuple[Any, ...] | None:
    if spec.stable_contract_id is None:
        return None
    return (
        spec.stable_contract_id,
        spec.profile,
        tuple(str(item) for item in spec.participants),
        str(spec.local_participant),
        spec.local_session_id,
        canonical_json_hash(spec.terms) if spec.terms is not None else None,
    )


async def _agreement_current_sessions(
    spec: ConcordAgreementSpec,
) -> dict[str, str]:
    sessions: dict[str, str] = {}
    evidence = spec.current_sessions
    if evidence is not None:
        raw = evidence() if callable(evidence) else evidence
        current = await _maybe_await(raw)
        sessions.update({str(key): value for key, value in current.items()})
    sessions[str(spec.local_participant)] = spec.local_session_id
    return sessions


def _agreement_record_matches_spec(
    record: ContractRecord,
    spec: ConcordAgreementSpec,
) -> bool:
    if record.profile != spec.profile:
        return False
    if tuple(record.participants) != tuple(spec.participants):
        return False
    if record.terms is None:
        return spec.terms is None
    if spec.terms is None:
        return False
    return thaw_json(record.terms) == thaw_json(spec.terms)


def _agreement_successor_status(status: ContractValidityStatus) -> bool:
    return status in {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }


def _terminal_managed_status(status: ContractValidityStatus) -> bool:
    return status in {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }


def _managed_event_type(
    status: ContractValidityStatus,
) -> ConcordManagedContractEventType:
    if status == ContractValidityStatus.VALID:
        return ConcordManagedContractEventType.VALID
    if status == ContractValidityStatus.CANCELLED:
        return ConcordManagedContractEventType.CANCELLED
    if status == ContractValidityStatus.NOT_YET_FULFILLED:
        return ConcordManagedContractEventType.PENDING
    return ConcordManagedContractEventType.INVALID


def _contract_handle(
    key: str,
    record: ContractRecord,
    revision: int,
) -> ContractHandle:
    return ContractHandle(
        key=key,
        contract_id=record.contract_id,
        generation=record.generation,
        participants=record.participants,
        attached_participants=record.attached_participants,
        revision=revision,
        state=record.state,
        profile=record.profile,
        terms_hash=record.terms_hash,
    )


def _participant_handle(
    key: str,
    record: ParticipantTokenRecord,
    revision: int,
) -> ParticipantHandle:
    return ParticipantHandle(
        key=key,
        contract_id=record.contract_id,
        generation=record.generation,
        participant=record.participant,
        session_id=record.session_id,
        token_id=record.token_id,
        revision=revision,
        refresh_seq=record.refresh_seq,
        ttl_seconds=record.ttl_seconds,
        terms_hash=record.terms_hash,
    )


def _token_matches_attach_request(
    token: ParticipantTokenRecord,
    *,
    record: ContractRecord,
    participant: EndpointAddress,
    session_id: str,
    token_id: str | None,
) -> bool:
    return (
        token.contract_id == record.contract_id
        and token.generation == record.generation
        and token.participant == participant
        and token.session_id == session_id
        and (token_id is None or token.token_id == token_id)
        and token.terms_hash == record.terms_hash
    )


def _token_matches_handle(
    token: ParticipantTokenRecord,
    handle: ParticipantHandle,
) -> bool:
    return (
        token.contract_id == handle.contract_id
        and token.generation == handle.generation
        and token.participant == handle.participant
        and token.session_id == handle.session_id
        and token.token_id == handle.token_id
        and token.terms_hash == handle.terms_hash
    )


def _is_state_revision_conflict(exc: ConcordConflict) -> bool:
    return "revision changed" in str(exc)


def _is_terminal_participant_conflict(exc: ConcordConflict) -> bool:
    message = str(exc)
    if message.startswith("Concord contract ") and (
        " is missing" in message or " is cancelled" in message
    ):
        return True
    return any(
        part in message
        for part in (
            "Concord contract is missing",
            "Concord contract is cancelled",
            "Concord participant token is missing",
            "Concord participant token changed owner",
            "Concord participant is already attached",
        )
    )


def _token_validity_status(
    token: ParticipantTokenRecord,
    *,
    contract: ContractRecord,
    participant: EndpointAddress,
    current_sessions: Mapping[str, str] | None,
) -> ContractValidityStatus | None:
    if token.contract_id != contract.contract_id:
        return ContractValidityStatus.INVALID_TOKEN
    if token.generation != contract.generation:
        return ContractValidityStatus.GENERATION_MISMATCH
    if token.participant != participant:
        return ContractValidityStatus.INVALID_TOKEN
    if contract.terms_hash is not None and token.terms_hash != contract.terms_hash:
        return ContractValidityStatus.TERMS_HASH_MISMATCH
    if current_sessions is not None:
        current_session = current_sessions.get(str(participant))
        if current_session is not None and token.session_id != current_session:
            return ContractValidityStatus.SESSION_MISMATCH
    return None


def _concord_event_type(validity: ContractValidity) -> ConcordEventType:
    if validity.status == ContractValidityStatus.VALID:
        return ConcordEventType.CONTRACT_VALID
    if validity.status == ContractValidityStatus.CANCELLED:
        return ConcordEventType.CONTRACT_CANCELLED
    if validity.status in {
        ContractValidityStatus.NOT_YET_FULFILLED,
    }:
        return ConcordEventType.CONTRACT_PENDING
    return ConcordEventType.CONTRACT_INVALID


def _log_concord_event(event: ConcordEvent) -> None:
    contract = event.contract
    if contract is None:
        return
    status = event.validity.status.value if event.validity is not None else None
    if event.event_type == ConcordEventType.CONTRACT_VALID:
        logger.log(
            _contract_lifecycle_log_level(event.profile),
            "Concord contract valid profile=%s contract=%s generation=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.CONTRACT_CANCELLED:
        logger.log(
            _contract_lifecycle_log_level(event.profile),
            "Concord contract cancelled profile=%s contract=%s generation=%s "
            "status=%s reason=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            status,
            event.reason,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.TOKEN_EXPIRED:
        event_status = event.validity.status if event.validity is not None else None
        logger.log(
            _contract_invalid_log_level(event.profile, event_status),
            "Concord participant token expired profile=%s contract=%s generation=%s "
            "participant=%s status=%s reason=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            event.participant,
            status,
            event.reason,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.CONTRACT_PENDING:
        level = _contract_pending_log_level(event.profile)
    else:
        event_status = event.validity.status if event.validity is not None else None
        level = _contract_invalid_log_level(event.profile, event_status)
    logger.log(
        level,
        "Concord contract %s profile=%s contract=%s generation=%s status=%s "
        "reason=%s revision=%s",
        event.event_type.value,
        event.profile,
        contract.contract_id,
        contract.generation,
        status,
        event.reason,
        contract.revision,
    )


ConcordTokenMaintenanceEntry = tuple[KvEntry, ParticipantTokenRecord | None]


def _empty_reaper_counts() -> dict[str, int]:
    return {
        "stale_observations_created": 0,
        "stale_observations_cleared": 0,
        "contracts_cancelled": 0,
        "contracts_deleted": 0,
        "token_keys_deleted": 0,
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_log_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _datetime_from_raw_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not isinstance(value, str):
        return None
    source = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _actor_log_value(value: EndpointAddress | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


async def _concord_participant_token_entries(
    token_bucket: _ConcordBucketAdapter,
    *,
    contract_id: str,
    generation: int,
) -> tuple[ConcordTokenMaintenanceEntry, ...]:
    token_entries: list[ConcordTokenMaintenanceEntry] = []
    for entry in await token_bucket.items_exact(
        concord_contract_prefix(contract_id=contract_id, generation=generation)
    ):
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            continue
        parsed_contract_id, parsed_generation, _participant = parsed
        if parsed_contract_id != contract_id or parsed_generation != generation:
            continue
        try:
            token = ParticipantTokenRecord.model_validate(entry.value)
        except ValueError:
            token = None
        token_entries.append((entry, token))
    return tuple(sorted(token_entries, key=lambda item: item[0].key))


async def _delete_concord_participant_token_entries(
    token_bucket: _ConcordBucketAdapter,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
) -> int:
    deleted = 0
    for entry, _token in token_entries:
        try:
            await token_bucket.delete(entry.key, revision=entry.revision)
        except ConcordConflict:
            await token_bucket.delete(entry.key)
        deleted += 1
    return deleted


def _token_log_summary(
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
) -> tuple[list[str], dict[str, str], dict[str, int | None]]:
    participants: list[str] = []
    sessions: dict[str, str] = {}
    refresh_sequences: dict[str, int | None] = {}
    for entry, token in token_entries:
        if token is None:
            participants.append(f"<invalid:{entry.key}>")
            refresh_sequences[entry.key] = None
            continue
        participant = str(token.participant)
        participants.append(participant)
        sessions[participant] = token.session_id
        refresh_sequences[participant] = token.refresh_seq
    return participants, sessions, refresh_sequences


def _log_concord_contract_deletion_audit(
    *,
    log_label: str,
    contract_key: str,
    record: ContractRecord,
    state_revision: int,
    validation_status: ContractValidityStatus,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
    deleted_token_key_count: int,
) -> None:
    token_participants, token_sessions, token_refresh_sequences = _token_log_summary(
        token_entries
    )
    logger.info(
        "%s Concord maintenance deleting cancelled contract contract_key=%s "
        "contract=%s generation=%s profile=%s state=%s participants=%s "
        "attached_participants=%s created_by=%s created_at=%s "
        "cancelled_by=%s cancelled_at=%s cancel_reason=%s cancel_revision=%s "
        "supersedes=%s terms_hash=%s validation_status=%s "
        "state_revision=%s token_participants=%s token_sessions=%s "
        "token_refresh_sequences=%s deleted_token_key_count=%s",
        log_label,
        contract_key,
        record.contract_id,
        record.generation,
        record.profile,
        record.state.value,
        [str(item) for item in record.participants],
        [str(item) for item in record.attached_participants],
        _actor_log_value(record.created_by),
        _datetime_log_value(record.created_at),
        _actor_log_value(record.cancelled_by),
        _datetime_log_value(record.cancelled_at),
        record.cancel_reason,
        record.cancel_revision,
        (
            record.supersedes.model_dump(by_alias=True, mode="json")
            if record.supersedes is not None
            else None
        ),
        record.terms_hash,
        validation_status.value,
        state_revision,
        token_participants,
        token_sessions,
        token_refresh_sequences,
        deleted_token_key_count,
    )


def _log_concord_raw_contract_deletion_audit(
    *,
    log_label: str,
    contract_key: str,
    contract_id: str,
    generation: int,
    raw_record: Mapping[str, Any],
    state_revision: int,
    validation_status: ContractValidityStatus,
    validation_reason: str,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
    deleted_token_key_count: int,
) -> None:
    record = thaw_json(raw_record)
    token_participants, token_sessions, token_refresh_sequences = _token_log_summary(
        token_entries
    )
    logger.info(
        "%s Concord maintenance deleting cancelled contract contract_key=%s "
        "contract=%s generation=%s profile=%s state=%s participants=%s "
        "attached_participants=%s created_by=%s created_at=%s "
        "cancelled_by=%s cancelled_at=%s cancel_reason=%s cancel_revision=%s "
        "supersedes=%s terms_hash=%s validation_status=%s validation_reason=%s "
        "state_revision=%s token_participants=%s token_sessions=%s "
        "token_refresh_sequences=%s deleted_token_key_count=%s",
        log_label,
        contract_key,
        contract_id,
        generation,
        record.get("profile"),
        record.get("state"),
        record.get("participants"),
        record.get("attachedParticipants"),
        record.get("createdBy"),
        record.get("createdAt"),
        record.get("cancelledBy"),
        record.get("cancelledAt"),
        record.get("cancelReason"),
        record.get("cancelRevision"),
        record.get("supersedes"),
        record.get("termsHash"),
        validation_status.value,
        validation_reason,
        state_revision,
        token_participants,
        token_sessions,
        token_refresh_sequences,
        deleted_token_key_count,
    )


__all__ = [
    "CONCORD_CONTRACT_SCHEMA_ID",
    "CONCORD_CONTRACT_BUCKET_POLICY",
    "CONCORD_MAINTENANCE_ACTOR",
    "CONCORD_MAINTENANCE_BUCKET_POLICY",
    "CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID",
    "CONCORD_REAPER_STALE_CONTRACT_REASON",
    "CONCORD_STALE_OBSERVATION_SCHEMA_ID",
    "CONCORD_TOKEN_BUCKET_POLICY",
    "DEFAULT_CONCORD_CONTRACT_BUCKET_NAME",
    "DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME",
    "DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS",
    "DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS",
    "DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS",
    "DEFAULT_CONCORD_TOKEN_BUCKET_NAME",
    "DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS",
    "DEFAULT_CONCORD_TOKEN_TTL_SECONDS",
    "ContractHandle",
    "ContractPointer",
    "ContractRecord",
    "ContractState",
    "ContractValidity",
    "ContractValidityStatus",
    "Concord",
    "ConcordAgreementLease",
    "ConcordAgreementSpec",
    "ConcordConflict",
    "ConcordEvent",
    "ConcordEventType",
    "ConcordMaintenanceDeletionResult",
    "ConcordManagedContract",
    "ConcordManagedContractEvent",
    "ConcordManagedContractEventType",
    "ConcordParticipant",
    "ConcordReaperConfig",
    "ConcordReaperScanResult",
    "ConcordReaperService",
    "ConcordStaleObservationRecord",
    "ConcordUnavailable",
    "ParticipantHandle",
    "ParticipantTokenRecord",
    "STALE_OPEN_CONTRACT_STATUSES",
    "TokenObservation",
    "canonical_json_bytes",
    "canonical_json_hash",
    "concord_contract_id_prefix",
    "concord_contract_key",
    "concord_contract_prefix",
    "concord_contracts_prefix",
    "concord_participant_token_key",
    "concord_stale_observation_key",
    "parse_concord_contract_key",
    "parse_concord_participant_token_key",
]
