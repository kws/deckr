from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from deckr._concord._keys import (
    canonical_json_hash,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json

CONCORD_CONTRACT_SCHEMA_ID = "dev.deckr.concord.contract.v1"
CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID = "dev.deckr.concord.participant-token.v1"


class ConcordConflictCode(StrEnum):
    KEY_ALREADY_EXISTS = "key_already_exists"
    REVISION_CHANGED = "revision_changed"
    CONTRACT_MISSING = "contract_missing"
    CONTRACT_CANCELLED = "contract_cancelled"
    CONTRACT_INVALID = "contract_invalid"
    CONTRACT_IDENTITY_MISMATCH = "contract_identity_mismatch"
    PARTICIPANT_NOT_NAMED = "participant_not_named"
    PARTICIPANT_ALREADY_ATTACHED = "participant_already_attached"
    TOKEN_ALREADY_EXISTS = "token_already_exists"
    TOKEN_MISSING = "token_missing"
    TOKEN_INVALID = "token_invalid"
    TOKEN_IDENTITY_MISMATCH = "token_identity_mismatch"
    LEASE_CLOSED = "lease_closed"
    AGREEMENT_CLOSED = "agreement_closed"


class ConcordUnavailableCode(StrEnum):
    STORE_UNAVAILABLE = "store_unavailable"
    SOURCE_STALE = "source_stale"
    TTL_METADATA_MISSING = "ttl_metadata_missing"
    TTL_INVALID = "ttl_invalid"


class ConcordConflict(RuntimeError):
    """A typed terminal conflict while reading or mutating Concord authority."""

    def __init__(
        self,
        code: ConcordConflictCode,
        message: str,
        *,
        key: str | None = None,
        expected_pointer: ContractPointer | None = None,
        observed_pointer: ContractPointer | None = None,
    ) -> None:
        if not isinstance(code, ConcordConflictCode):
            raise TypeError("ConcordConflict code must be ConcordConflictCode")
        super().__init__(message)
        self.code = code
        self.message = message
        self.key = key
        self.expected_pointer = expected_pointer
        self.observed_pointer = observed_pointer


class ConcordUnavailable(RuntimeError):
    """A typed failure to obtain safe Concord store or source state."""

    def __init__(
        self,
        code: ConcordUnavailableCode,
        message: str,
        *,
        bucket: str | None = None,
        key: str | None = None,
    ) -> None:
        if not isinstance(code, ConcordUnavailableCode):
            raise TypeError("ConcordUnavailable code must be ConcordUnavailableCode")
        super().__init__(message)
        self.code = code
        self.message = message
        self.bucket = bucket
        self.key = key


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


class ContractValidityReason(StrEnum):
    SOURCE_UNAVAILABLE = "source_unavailable"
    CONTRACT_MISSING = "contract_missing"
    CONTRACT_MALFORMED = "contract_malformed"
    CONTRACT_KEY_MISMATCH = "contract_key_mismatch"
    CONTRACT_POINTER_MISMATCH = "contract_pointer_mismatch"
    CONTRACT_CANCELLED = "contract_cancelled"
    PARTICIPANT_NOT_ATTACHED = "participant_not_attached"
    TOKEN_MISSING = "token_missing"
    TOKEN_MALFORMED = "token_malformed"
    TOKEN_KEY_MISMATCH = "token_key_mismatch"
    TOKEN_CONTRACT_MISMATCH = "token_contract_mismatch"
    TOKEN_GENERATION_MISMATCH = "token_generation_mismatch"
    TOKEN_PARTICIPANT_MISMATCH = "token_participant_mismatch"
    TOKEN_TERMS_HASH_MISMATCH = "token_terms_hash_mismatch"
    TOKEN_SESSION_MISMATCH = "token_session_mismatch"
    TOKEN_LOCAL_AUTHORITY_LOST = "token_local_authority_lost"


def require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
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
        return require_text(value, field_name="contract id")

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
        return require_text(value, field_name="Concord contract field")

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
        return require_text(value, field_name="Concord participant token identity")

    @field_validator("terms_hash", "contract_hash")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_text(value, field_name="Concord participant token field")

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

    @property
    def pointer(self) -> ContractPointer:
        return ContractPointer(contractId=self.contract_id, generation=self.generation)


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
    reason_code: ContractValidityReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", MappingProxyType(dict(self.tokens)))

    @property
    def valid(self) -> bool:
        return self.status == ContractValidityStatus.VALID


def contract_handle(key: str, record: ContractRecord, revision: int) -> ContractHandle:
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


def participant_handle(
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


def contract_handle_has_canonical_identity(handle: ContractHandle) -> bool:
    return handle.key == concord_contract_key(
        contract_id=handle.contract_id,
        generation=handle.generation,
    )


def participant_handle_has_canonical_identity(handle: ParticipantHandle) -> bool:
    return handle.key == concord_participant_token_key(
        contract_id=handle.contract_id,
        generation=handle.generation,
        participant=handle.participant,
    )


def token_matches_handle(
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


def participant_handle_matches(
    current: ParticipantHandle,
    updated: ParticipantHandle,
) -> bool:
    return (
        updated.key == current.key
        and updated.contract_id == current.contract_id
        and updated.generation == current.generation
        and updated.participant == current.participant
        and updated.session_id == current.session_id
        and updated.token_id == current.token_id
        and updated.terms_hash == current.terms_hash
    )
