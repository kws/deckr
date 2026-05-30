from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.state import (
    PERSISTENT_STATE_STORE_POLICY,
    StateChange,
    StateConflict,
    StateStore,
    StateStorePolicy,
    StateUnavailable,
)

CONCORD_CONTRACT_SCHEMA_ID = "dev.deckr.concord.contract.v1"
CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID = "dev.deckr.concord.participant-token.v1"
DEFAULT_CONCORD_CONTRACT_STORE_NAME = "deckr_concord_contract_v1"
DEFAULT_CONCORD_TOKEN_STORE_NAME = "deckr_concord_token_v1"
DEFAULT_CONCORD_TOKEN_TTL_SECONDS = 30
ACTION_PROVIDER_SESSION_PROFILE_ID = "dev.deckr.profile.action_provider_session.v1"
CONCORD_CONTRACT_STORE_POLICY = PERSISTENT_STATE_STORE_POLICY
CONCORD_TOKEN_STORE_POLICY = StateStorePolicy(
    broker_ttl_seconds=float(DEFAULT_CONCORD_TOKEN_TTL_SECONDS),
    allow_write_ttl=True,
    description="Concord participant token state",
)

logger = logging.getLogger(__name__)


def _contract_lifecycle_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


def _contract_pending_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


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
    OPENED = "opened"
    VALID = "valid"
    PENDING = "pending"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    TOKEN_EXPIRED = "token_expired"


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
    cancelled_by: EndpointAddress | None = Field(default=None, alias="cancelledBy")
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
class ConcordContractEvent:
    event_type: ConcordEventType
    contract: ContractHandle | None
    validity: ContractValidity | None = None
    profile: str | None = None
    participant: EndpointAddress | None = None
    reason: str | None = None
    change: StateChange | None = None


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


class ConcordCoordinator:
    def __init__(
        self,
        contract_state: StateStore,
        token_state: StateStore,
        *,
        token_ttl_seconds: int = DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
    ) -> None:
        if token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be greater than zero")
        self._contract_state = contract_state
        self._token_state = token_state
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
        entry = await self._contract_state.create(key, record)
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
        entry = await self._contract_state.get(key)
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
        entry = await self._contract_state.get(contract.key)
        if entry is None:
            return None
        record = ContractRecord.model_validate(entry.value)
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            return None
        return record

    async def find_contracts(self, profile: str | None = None) -> tuple[ContractHandle, ...]:
        contracts: list[ContractHandle] = []
        for entry in await self._contract_state.items(concord_contracts_prefix()):
            parsed = parse_concord_contract_key(entry.key)
            if parsed is None:
                continue
            try:
                record = ContractRecord.model_validate(entry.value)
            except ValueError:
                continue
            if profile is not None and record.profile != profile:
                continue
            contract_id, generation = parsed
            if record.contract_id != contract_id or record.generation != generation:
                continue
            contracts.append(_contract_handle(entry.key, record, entry.revision))
        return tuple(sorted(contracts, key=lambda contract: contract.key))

    def watch_contracts(
        self,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[StateChange]]:
        return self._contract_state.watch(concord_contracts_prefix())

    async def attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ParticipantHandle:
        current = await self._contract_state.get(contract.key)
        if current is None:
            raise StateConflict(f"Concord contract {contract.key!r} is missing")
        record = ContractRecord.model_validate(current.value)
        if record.state == ContractState.CANCELLED:
            raise StateConflict(f"Concord contract {contract.key!r} is cancelled")
        parsed_participant = parse_endpoint_address(participant)
        if parsed_participant not in record.participants:
            raise ValueError("participant is not named by the Concord contract")
        if parsed_participant in record.attached_participants:
            raise StateConflict("Concord participant is already attached")
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
            entry = await self._token_state.create(key, token, ttl=token.ttl_seconds)
        except StateConflict as exc:
            token_entry = await self._token_state.get(key)
            if token_entry is None:
                raise StateConflict(
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
                raise StateConflict("Concord participant token already exists") from exc
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
            current = await self._contract_state.get(contract_key)
            if current is None:
                raise StateConflict(f"Concord contract {contract_key!r} is missing")
            record = ContractRecord.model_validate(current.value)
            if record.state == ContractState.CANCELLED:
                raise StateConflict(f"Concord contract {contract_key!r} is cancelled")
            if participant not in record.participants:
                raise StateConflict("participant is not named by the Concord contract")
            if participant in record.attached_participants:
                if allow_already_attached:
                    return
                raise StateConflict("Concord participant is already attached")
            attached = tuple(sorted((*record.attached_participants, participant), key=str))
            updated = record.model_copy(update={"attached_participants": attached})
            try:
                await self._contract_state.update(
                    contract_key,
                    updated,
                    revision=current.revision,
                )
            except StateConflict:
                continue
            return

    async def refresh(self, handle: ParticipantHandle) -> ParticipantHandle:
        contract_entry = await self._contract_state.get(
            concord_contract_key(
                contract_id=handle.contract_id,
                generation=handle.generation,
            )
        )
        if contract_entry is None:
            raise StateConflict("Concord contract is missing")
        contract = ContractRecord.model_validate(contract_entry.value)
        if contract.state == ContractState.CANCELLED:
            raise StateConflict("Concord contract is cancelled")
        token_entry = await self._token_state.get(handle.key)
        if token_entry is None:
            raise StateConflict("Concord participant token is missing")
        token = ParticipantTokenRecord.model_validate(token_entry.value)
        if not _token_matches_handle(token, handle):
            raise StateConflict("Concord participant token changed owner")
        refreshed = token.model_copy(update={"refresh_seq": token.refresh_seq + 1})
        try:
            entry = await self._token_state.update(
                handle.key,
                refreshed,
                revision=token_entry.revision,
                ttl=refreshed.ttl_seconds,
            )
        except StateConflict as exc:
            if not _is_state_revision_conflict(exc):
                raise
            latest_entry = await self._token_state.get(handle.key)
            if latest_entry is None:
                raise StateConflict("Concord participant token is missing") from exc
            latest = ParticipantTokenRecord.model_validate(latest_entry.value)
            if not _token_matches_handle(latest, handle):
                raise StateConflict("Concord participant token changed owner") from exc
            return _participant_handle(handle.key, latest, latest_entry.revision)
        return _participant_handle(handle.key, refreshed, entry.revision)

    async def cancel(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
    ) -> bool:
        current = await self._contract_state.get(contract.key)
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
        await self._contract_state.update(
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
            contract_entry = await self._contract_state.get(contract.key)
        except StateUnavailable:
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
                token_entry = await self._token_state.get(token_key)
            except StateUnavailable:
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
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[StateChange]]:
        return self._contract_state.watch(
            concord_contract_prefix(
                contract_id=contract.contract_id,
                generation=contract.generation,
            )
        )


class ConcordParticipantLease:
    """Owns one participant token and its heartbeat for a Concord contract."""

    def __init__(
        self,
        service: ConcordService,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        refresh_interval: float = 5.0,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        self._service = service
        self.contract = contract
        self.participant = parse_endpoint_address(participant)
        self.session_id = _require_text(session_id, field_name="Concord session id")
        self._refresh_interval = refresh_interval
        self._log_label = log_label
        self._token: ParticipantHandle | None = None
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

    async def aclose(self) -> None:
        self._closed = True

    def adopt(self, token: ParticipantHandle) -> None:
        if token.contract_id != self.contract.contract_id:
            raise ValueError("participant token belongs to a different contract")
        if token.generation != self.contract.generation:
            raise ValueError("participant token belongs to a different generation")
        if token.participant != self.participant:
            raise ValueError("participant token belongs to a different participant")
        if token.session_id != self.session_id:
            raise ValueError("participant token belongs to a different session")
        self._token = token

    async def attach_or_refresh(self) -> ParticipantHandle:
        async with self._lock:
            if self._closed:
                raise StateConflict("Concord participant lease is closed")
            token = self._token
            if token is not None:
                try:
                    self._token = await self._service.refresh_token(
                        token,
                        log_label=self._log_label,
                    )
                    return self._token
                except StateConflict as exc:
                    self._token = None
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
                self._token = await self._service.attach(
                    self.contract,
                    self.participant,
                    self.session_id,
                    log_label=self._log_label,
                )
            except StateConflict as exc:
                if _is_terminal_participant_conflict(exc):
                    self._closed = True
                raise
            return self._token

    async def heartbeat_loop(self) -> None:
        while not self._closed:
            await anyio.sleep(self._refresh_interval)
            if self._closed:
                return
            try:
                await self.attach_or_refresh()
            except StateConflict:
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
            except StateUnavailable:
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


class ConcordService:
    """Runtime-facing Concord API with participant leases and semantic events."""

    def __init__(self, coordinator: ConcordCoordinator) -> None:
        self._coordinator = coordinator

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
        return await self._coordinator.get_contract(pointer)

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        return await self._coordinator.contract_record(contract)

    async def find_contracts(
        self,
        profile: str | None = None,
    ) -> tuple[ContractHandle, ...]:
        return await self._coordinator.find_contracts(profile)

    async def attach(
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

    async def refresh_token(
        self,
        handle: ParticipantHandle,
        *,
        log_label: str = "Concord",
    ) -> ParticipantHandle:
        refreshed = await self._coordinator.refresh(handle)
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

    async def cancel(
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

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
        log_label: str = "Concord",
    ) -> ContractValidity:
        validity = await self._coordinator.validate(
            contract,
            current_sessions=current_sessions,
        )
        if validity.status in {
            ContractValidityStatus.MISSING_TOKEN,
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityStatus.GENERATION_MISMATCH,
            ContractValidityStatus.SESSION_MISMATCH,
            ContractValidityStatus.TERMS_HASH_MISMATCH,
        }:
            logger.warning(
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

    def participant_lease(
        self,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        refresh_interval: float = 5.0,
        log_label: str = "Concord",
    ) -> ConcordParticipantLease:
        return ConcordParticipantLease(
            self,
            contract=contract,
            participant=participant,
            session_id=session_id,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )

    @asynccontextmanager
    async def watch_contracts(
        self,
        profile: str | None = None,
        *,
        log_events: bool = True,
    ) -> Any:
        send, receive = anyio.create_memory_object_stream[ConcordContractEvent](100)
        last_status: dict[tuple[str, int], ContractValidityStatus] = {}

        async def publish_validity(contract: ContractHandle, change: StateChange) -> None:
            if profile is not None and contract.profile != profile:
                return
            validity = await self._coordinator.validate(contract)
            key = (contract.contract_id, contract.generation)
            previous = last_status.get(key)
            last_status[key] = validity.status
            if previous == validity.status and validity.status == ContractValidityStatus.VALID:
                return
            event_type = _concord_event_type(validity)
            event = ConcordContractEvent(
                event_type,
                contract,
                validity=validity,
                profile=contract.profile,
                reason=validity.reason,
                change=change,
            )
            if log_events:
                _log_concord_event(event)
            await send.send(event)

        async def contract_loop() -> None:
            try:
                async with self._coordinator.watch_contracts() as changes:
                    async for change in changes:
                        contract = _contract_handle_from_change(change)
                        if contract is None:
                            continue
                        await publish_validity(contract, change)
            finally:
                await send.aclose()

        async def token_loop() -> None:
            async with self._coordinator._token_state.watch(  # noqa: SLF001
                concord_contracts_prefix()
            ) as changes:
                async for change in changes:
                    parsed = parse_concord_participant_token_key(change.key)
                    if parsed is None:
                        continue
                    contract_id, generation, participant = parsed
                    contract = await self.get_contract(
                        ContractPointer(contractId=contract_id, generation=generation)
                    )
                    if contract is None:
                        continue
                    if profile is not None and contract.profile != profile:
                        continue
                    if change.operation == "expire":
                        event = ConcordContractEvent(
                            ConcordEventType.TOKEN_EXPIRED,
                            contract,
                            validity=await self._coordinator.validate(contract),
                            profile=contract.profile,
                            participant=participant,
                            reason="token_expired",
                            change=change,
                        )
                        if log_events:
                            _log_concord_event(event)
                        await send.send(event)
                        continue
                    await publish_validity(contract, change)

        async with receive, send, anyio.create_task_group() as task_group:
            task_group.start_soon(contract_loop)
            task_group.start_soon(token_loop)
            try:
                yield receive
            finally:
                task_group.cancel_scope.cancel()


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


class ConcordParticipantManager:
    """Owns one local participant's token lifecycle for selected contracts."""

    def __init__(
        self,
        *,
        concord: ConcordService,
        participant: str | EndpointAddress,
        session_id: str,
        accept_contract: ConcordContractPredicate,
        current_sessions: ConcordCurrentSessions | None = None,
        prepare_reconcile: ConcordPrepareReconcile | None = None,
        contract_sort_key: ConcordContractSortKey | None = None,
        profile: str | None = None,
        refresh_interval: float = 5.0,
        reconcile_interval: float = 1.0,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if reconcile_interval <= 0:
            raise ValueError("reconcile_interval must be greater than zero")
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
        start_soon(self.reconcile_loop)

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

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        reason: str | None = None,
    ) -> bool:
        return await self._concord.cancel(
            contract,
            self.participant,
            reason=reason,
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
                async with self._concord.watch_contracts(
                    self.profile,
                    log_events=False,
                ) as stream:
                    async for event in stream:
                        if self._closed:
                            return
                        await self.reconcile(
                            reason=f"contract watch {event.event_type.value}"
                        )
            except StateUnavailable:
                await anyio.sleep(self._reconcile_interval)

    async def reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile(reason="periodic reconcile")
            except StateUnavailable:
                logger.warning(
                    "%s Concord participant manager unavailable; "
                    "reconciliation will retry profile=%s participant=%s",
                    self._log_label,
                    self.profile,
                    self.participant,
                    exc_info=True,
                )
            await anyio.sleep(self._reconcile_interval)

    async def reconcile(
        self,
        *,
        reason: str = "manual reconcile",
    ) -> tuple[ConcordManagedContract, ...]:
        async with self._lock:
            if self._closed:
                return ()
            contracts = await self._concord.find_contracts(self.profile)
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
            record = await self._concord.contract_record(contract)
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
        validity = await self._concord.validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )
        record = validity.contract or record

        existing = validity.tokens.get(str(self.participant))
        if _terminal_managed_status(validity.status):
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
            lease = self._concord.participant_lease(
                contract=contract,
                participant=self.participant,
                session_id=self.session_id,
                refresh_interval=self._refresh_interval,
                log_label=self._log_label,
            )
            if self._start_soon is not None:
                lease.start_soon(self._start_soon)
            self._leases[contract.key] = lease

        if lease.token is None and existing is not None:
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
        except StateConflict:
            validity = await self._concord.validate(
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

        validity = await self._concord.validate(
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
            await lease.aclose()
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


def _is_state_revision_conflict(exc: StateConflict) -> bool:
    return "revision changed" in str(exc)


def _is_terminal_participant_conflict(exc: StateConflict) -> bool:
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


def _contract_handle_from_change(change: StateChange) -> ContractHandle | None:
    if change.entry is None:
        return None
    parsed = parse_concord_contract_key(change.key)
    if parsed is None:
        return None
    try:
        record = ContractRecord.model_validate(change.entry.value)
    except ValueError:
        return None
    contract_id, generation = parsed
    if record.contract_id != contract_id or record.generation != generation:
        return None
    return _contract_handle(change.key, record, change.entry.revision)


def _concord_event_type(validity: ContractValidity) -> ConcordEventType:
    if validity.status == ContractValidityStatus.VALID:
        return ConcordEventType.VALID
    if validity.status == ContractValidityStatus.CANCELLED:
        return ConcordEventType.CANCELLED
    if validity.status in {
        ContractValidityStatus.NOT_YET_FULFILLED,
    }:
        return ConcordEventType.PENDING
    return ConcordEventType.INVALID


def _log_concord_event(event: ConcordContractEvent) -> None:
    contract = event.contract
    if contract is None:
        return
    status = event.validity.status.value if event.validity is not None else None
    if event.event_type == ConcordEventType.VALID:
        logger.log(
            _contract_lifecycle_log_level(event.profile),
            "Concord contract valid profile=%s contract=%s generation=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.CANCELLED:
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
        logger.warning(
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
    level = (
        _contract_pending_log_level(event.profile)
        if event.event_type == ConcordEventType.PENDING
        else logging.WARNING
    )
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


__all__ = [
    "CONCORD_CONTRACT_SCHEMA_ID",
    "CONCORD_CONTRACT_STORE_POLICY",
    "CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID",
    "CONCORD_TOKEN_STORE_POLICY",
    "DEFAULT_CONCORD_CONTRACT_STORE_NAME",
    "DEFAULT_CONCORD_TOKEN_STORE_NAME",
    "DEFAULT_CONCORD_TOKEN_TTL_SECONDS",
    "ContractHandle",
    "ContractPointer",
    "ContractRecord",
    "ContractState",
    "ContractValidity",
    "ContractValidityStatus",
    "ConcordCoordinator",
    "ConcordContractEvent",
    "ConcordEventType",
    "ConcordManagedContract",
    "ConcordManagedContractEvent",
    "ConcordManagedContractEventType",
    "ConcordParticipantLease",
    "ConcordParticipantManager",
    "ConcordService",
    "ParticipantHandle",
    "ParticipantTokenRecord",
    "TokenObservation",
    "canonical_json_bytes",
    "canonical_json_hash",
    "concord_contract_key",
    "concord_contract_prefix",
    "concord_contracts_prefix",
    "concord_participant_token_key",
    "parse_concord_contract_key",
    "parse_concord_participant_token_key",
]
