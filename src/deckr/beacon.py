from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.state import (
    StateChange,
    StateConflict,
    StateEntry,
    StateStore,
    StateStorePolicy,
    StateUnavailable,
)

BEACON_ADVERTISEMENT_SCHEMA_ID = "dev.deckr.beacon.advertisement.v1"
DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME = "deckr_beacon_advertisement_v1"
DEFAULT_BEACON_TTL_SECONDS = 30
BEACON_ADVERTISEMENT_STORE_POLICY = StateStorePolicy(
    broker_ttl_seconds=float(DEFAULT_BEACON_TTL_SECONDS),
    allow_write_ttl=True,
    description="Beacon advertisement state",
)


class CandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    MISSING = "missing"
    SCHEMA_INVALID = "schema_invalid"
    FEATURE_MISMATCH = "feature_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    UNAVAILABLE = "unavailable"


class AdvertisementSelector(Protocol):
    def accepts(self, advertisement: AdvertisementRecord) -> bool: ...


AdvertisementFilter = Callable[["AdvertisementRecord"], bool] | AdvertisementSelector


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


def beacon_advertisement_key(*, feature_id: str, advertisement_id: str) -> str:
    return ".".join(
        (
            "advertisements",
            "by_feature",
            encode_key_token(feature_id),
            encode_key_token(advertisement_id),
        )
    )


def parse_beacon_advertisement_key(key: str) -> tuple[str, str] | None:
    parts = key.split(".")
    if len(parts) != 4 or parts[:2] != ["advertisements", "by_feature"]:
        return None
    return decode_key_token(parts[2]), decode_key_token(parts[3])


def beacon_feature_prefix(feature_id: str) -> str:
    return ".".join(
        ("advertisements", "by_feature", encode_key_token(feature_id), "")
    )


class BeaconProtocol(DeckrModel):
    namespace: str
    version: str

    @field_validator("namespace", "version")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="Beacon protocol field")


class AdvertisementRecord(DeckrModel):
    schema_id: Literal[BEACON_ADVERTISEMENT_SCHEMA_ID] = Field(
        default=BEACON_ADVERTISEMENT_SCHEMA_ID,
        alias="schema",
    )
    advertisement_id: str = Field(alias="advertisementId")
    feature_id: str = Field(alias="featureId")
    advertiser: EndpointAddress
    endpoint: EndpointAddress
    session_id: str = Field(alias="sessionId")
    refresh_seq: int = Field(alias="refreshSeq")
    ttl_seconds: int = Field(alias="ttlSeconds")
    protocol: BeaconProtocol | None = None
    operations: tuple[str, ...] = Field(default_factory=tuple)
    labels: Mapping[str, str] = Field(default_factory=dict)
    hints: JsonObject = Field(default_factory=dict)
    payload: JsonObject | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")

    @field_validator(
        "advertisement_id",
        "feature_id",
        "session_id",
    )
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _require_text(value, field_name="Beacon advertisement identity")

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

    @field_validator("operations")
    @classmethod
    def _validate_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_text(item, field_name="Beacon operation") for item in value)

    @field_validator("labels", mode="after")
    @classmethod
    def _freeze_labels(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(
            {
                _require_text(key, field_name="Beacon label key"): _require_text(
                    item,
                    field_name="Beacon label value",
                )
                for key, item in value.items()
            }
        )

    @field_validator("hints", "payload", mode="before")
    @classmethod
    def _thaw_json_object(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("hints", "payload", mode="after")
    @classmethod
    def _freeze_json_object(
        cls,
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        return freeze_json(value) if value is not None else None

    @field_serializer("labels")
    def _serialize_labels(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)

    @field_serializer("hints", "payload")
    def _serialize_json_object(
        self,
        value: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        return thaw_json(value) if value is not None else None

    @field_serializer("created_at", "updated_at")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @model_validator(mode="after")
    def _validate_keyable_identity(self) -> AdvertisementRecord:
        expected = beacon_advertisement_key(
            feature_id=self.feature_id,
            advertisement_id=self.advertisement_id,
        )
        if not expected:
            raise ValueError("Beacon advertisement identity is not keyable")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


@dataclass(frozen=True, slots=True)
class AdvertisementHandle:
    key: str
    advertisement_id: str
    feature_id: str
    advertiser: EndpointAddress
    endpoint: EndpointAddress
    session_id: str
    revision: int
    refresh_seq: int


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    advertisement: AdvertisementRecord
    revision: int
    observed_at: datetime

    @property
    def endpoint(self) -> EndpointAddress:
        return self.advertisement.endpoint


@dataclass(frozen=True, slots=True)
class BeaconEvent:
    change: StateChange
    candidate: Candidate | None = None


class BeaconDiscovery:
    def __init__(
        self,
        state: StateStore,
        *,
        default_ttl_seconds: int = DEFAULT_BEACON_TTL_SECONDS,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be greater than zero")
        self._state = state
        self._default_ttl_seconds = default_ttl_seconds

    async def advertise(
        self,
        feature_id: str,
        endpoint: str | EndpointAddress,
        session_id: str,
        *,
        advertiser: str | EndpointAddress | None = None,
        advertisement_id: str | None = None,
        protocol: Mapping[str, str] | BeaconProtocol | None = None,
        operations: tuple[str, ...] | list[str] = (),
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> AdvertisementHandle:
        parsed_endpoint = parse_endpoint_address(endpoint)
        parsed_advertiser = (
            parse_endpoint_address(advertiser)
            if advertiser is not None
            else parsed_endpoint
        )
        ttl = ttl_seconds or self._default_ttl_seconds
        record = AdvertisementRecord(
            advertisementId=advertisement_id or str(uuid.uuid4()),
            featureId=feature_id,
            advertiser=parsed_advertiser,
            endpoint=parsed_endpoint,
            sessionId=session_id,
            refreshSeq=1,
            ttlSeconds=ttl,
            protocol=protocol,
            operations=tuple(operations),
            labels=labels or {},
            hints=hints or {},
            payload=payload,
            createdAt=_now_utc(),
            updatedAt=_now_utc(),
        )
        key = beacon_advertisement_key(
            feature_id=record.feature_id,
            advertisement_id=record.advertisement_id,
        )
        entry = await self._state.create(key, record, ttl=record.ttl_seconds)
        return _advertisement_handle(key, record, entry.revision)

    async def refresh(
        self,
        handle: AdvertisementHandle,
        *,
        hints: Mapping[str, Any] | None = None,
        labels: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AdvertisementHandle:
        current = await self._state.get(handle.key)
        if current is None:
            raise StateConflict(f"Beacon advertisement {handle.key!r} is missing")
        record = AdvertisementRecord.model_validate(current.value)
        if not _advertisement_matches_handle(record, handle):
            raise StateConflict(f"Beacon advertisement {handle.key!r} changed owner")
        refreshed = record.model_copy(
            update={
                "refresh_seq": record.refresh_seq + 1,
                "hints": freeze_json(hints) if hints is not None else record.hints,
                "labels": freeze_json(labels) if labels is not None else record.labels,
                "payload": freeze_json(payload) if payload is not None else record.payload,
                "updated_at": _now_utc(),
            }
        )
        entry = await self._state.update(
            handle.key,
            refreshed,
            revision=current.revision,
            ttl=refreshed.ttl_seconds,
        )
        return _advertisement_handle(handle.key, refreshed, entry.revision)

    async def withdraw(self, handle: AdvertisementHandle) -> bool:
        current = await self._state.get(handle.key)
        if current is None:
            return False
        record = AdvertisementRecord.model_validate(current.value)
        if not _advertisement_matches_handle(record, handle):
            raise StateConflict(f"Beacon advertisement {handle.key!r} changed owner")
        await self._state.delete(handle.key, revision=current.revision)
        return True

    async def find(
        self,
        feature_id: str,
        selector: AdvertisementFilter | None = None,
    ) -> tuple[Candidate, ...]:
        candidates: list[Candidate] = []
        for entry in await self._state.items(beacon_feature_prefix(feature_id)):
            candidate = _candidate_from_entry(entry)
            if candidate is None:
                continue
            if candidate.advertisement.feature_id != feature_id:
                continue
            if selector is not None and not _selector_accepts(selector, candidate.advertisement):
                continue
            candidates.append(candidate)
        return tuple(sorted(candidates, key=lambda item: item.key))

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus:
        try:
            entry = await self._state.get(candidate.key)
        except StateUnavailable:
            return CandidateStatus.UNAVAILABLE
        if entry is None:
            return CandidateStatus.MISSING
        try:
            advertisement = AdvertisementRecord.model_validate(entry.value)
        except ValueError:
            return CandidateStatus.SCHEMA_INVALID
        if advertisement.feature_id != candidate.advertisement.feature_id:
            return CandidateStatus.FEATURE_MISMATCH
        if current_sessions is not None:
            current_session = current_sessions.get(str(advertisement.advertiser))
            if current_session is not None and advertisement.session_id != current_session:
                return CandidateStatus.SESSION_MISMATCH
        return CandidateStatus.CANDIDATE

    def watch(
        self,
        feature_id: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[StateChange]]:
        return self._state.watch(beacon_feature_prefix(feature_id))


def _advertisement_handle(
    key: str,
    record: AdvertisementRecord,
    revision: int,
) -> AdvertisementHandle:
    return AdvertisementHandle(
        key=key,
        advertisement_id=record.advertisement_id,
        feature_id=record.feature_id,
        advertiser=record.advertiser,
        endpoint=record.endpoint,
        session_id=record.session_id,
        revision=revision,
        refresh_seq=record.refresh_seq,
    )


def _advertisement_matches_handle(
    record: AdvertisementRecord,
    handle: AdvertisementHandle,
) -> bool:
    return (
        record.advertisement_id == handle.advertisement_id
        and record.feature_id == handle.feature_id
        and record.advertiser == handle.advertiser
        and record.endpoint == handle.endpoint
        and record.session_id == handle.session_id
    )


def _candidate_from_entry(entry: StateEntry) -> Candidate | None:
    try:
        advertisement = AdvertisementRecord.model_validate(entry.value)
    except ValueError:
        return None
    parsed = parse_beacon_advertisement_key(entry.key)
    if parsed is None:
        return None
    feature_id, advertisement_id = parsed
    if (
        feature_id != advertisement.feature_id
        or advertisement_id != advertisement.advertisement_id
    ):
        return None
    return Candidate(
        key=entry.key,
        advertisement=advertisement,
        revision=entry.revision,
        observed_at=_now_utc(),
    )


def _selector_accepts(
    selector: AdvertisementFilter,
    advertisement: AdvertisementRecord,
) -> bool:
    accepts = getattr(selector, "accepts", None)
    if accepts is not None:
        return bool(accepts(advertisement))
    return bool(selector(advertisement))


__all__ = [
    "BEACON_ADVERTISEMENT_SCHEMA_ID",
    "BEACON_ADVERTISEMENT_STORE_POLICY",
    "DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME",
    "DEFAULT_BEACON_TTL_SECONDS",
    "AdvertisementHandle",
    "AdvertisementRecord",
    "BeaconDiscovery",
    "BeaconEvent",
    "BeaconProtocol",
    "Candidate",
    "CandidateStatus",
    "beacon_advertisement_key",
    "beacon_feature_prefix",
    "parse_beacon_advertisement_key",
]
