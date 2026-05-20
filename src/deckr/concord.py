from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
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
CONCORD_CONTRACT_STORE_POLICY = PERSISTENT_STATE_STORE_POLICY
CONCORD_TOKEN_STORE_POLICY = StateStorePolicy(
    broker_ttl_seconds=float(DEFAULT_CONCORD_TOKEN_TTL_SECONDS),
    allow_write_ttl=True,
    description="Concord participant token state",
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
    tokens: Mapping[str, ParticipantTokenRecord] = field(default_factory=dict)
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.status == ContractValidityStatus.VALID


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
        entry = await self._token_state.create(key, token, ttl=token.ttl_seconds)
        return _participant_handle(key, token, entry.revision)

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
        entry = await self._token_state.update(
            handle.key,
            refreshed,
            revision=token_entry.revision,
            ttl=refreshed.ttl_seconds,
        )
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

        tokens: dict[str, ParticipantTokenRecord] = {}
        for participant in record.participants:
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
                return ContractValidity(
                    ContractValidityStatus.MISSING_TOKEN,
                    contract=record,
                    tokens=tokens,
                    reason=str(participant),
                )
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
            tokens[str(participant)] = token
            if status is not None:
                return ContractValidity(status, contract=record, tokens=tokens)
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
