from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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

logger = logging.getLogger(__name__)


def _single_exception_from_group(exc: BaseExceptionGroup) -> BaseException | None:
    if len(exc.exceptions) != 1:
        return None
    child = exc.exceptions[0]
    if isinstance(child, BaseExceptionGroup):
        return _single_exception_from_group(child)
    return child


def _beacon_lifecycle_log_level(feature_id: str) -> int:
    if feature_id == "dev.deckr.hardware":
        return logging.INFO
    return logging.DEBUG


class CandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    MISSING = "missing"
    SCHEMA_INVALID = "schema_invalid"
    FEATURE_MISMATCH = "feature_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    UNAVAILABLE = "unavailable"


class BeaconFeatureEventType(StrEnum):
    ADVERTISED = "advertised"
    UPDATED = "updated"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    INVALID = "invalid"


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


@dataclass(frozen=True, slots=True)
class BeaconFeatureEvent:
    event_type: BeaconFeatureEventType
    feature_id: str
    key: str
    candidate: Candidate | None = None
    previous: Candidate | None = None
    reason: str | None = None
    change: StateChange | None = None


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


class BeaconAdvertiser:
    """Owns one refreshable Beacon advertisement for a running participant."""

    def __init__(
        self,
        service: BeaconService,
        *,
        feature_id: str,
        endpoint: str | EndpointAddress,
        session_id: str,
        advertiser: str | EndpointAddress | None = None,
        advertisement_id: str | None = None,
        protocol: Mapping[str, str] | BeaconProtocol | None = None,
        operations: tuple[str, ...] | list[str] = (),
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        ttl_seconds: int | None = None,
        refresh_interval: float = 5.0,
        log_label: str = "Beacon",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        self._service = service
        self.feature_id = _require_text(feature_id, field_name="Beacon feature id")
        self.endpoint = parse_endpoint_address(endpoint)
        self.session_id = _require_text(session_id, field_name="Beacon session id")
        self.advertiser = (
            parse_endpoint_address(advertiser)
            if advertiser is not None
            else self.endpoint
        )
        self._advertisement_id = advertisement_id
        self._protocol = protocol
        self._operations = tuple(operations)
        self._labels = dict(labels or {})
        self._hints = dict(hints or {})
        self._payload = dict(payload) if payload is not None else None
        self._ttl_seconds = ttl_seconds
        self._refresh_interval = refresh_interval
        self._log_label = log_label
        self._handle: AdvertisementHandle | None = None
        self._lock = anyio.Lock()
        self._started = False

    @property
    def handle(self) -> AdvertisementHandle | None:
        return self._handle

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        if self._started:
            return
        self._started = True
        task_group.start_soon(self.heartbeat_loop)

    async def publish(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        operations: tuple[str, ...] | list[str] | None = None,
    ) -> AdvertisementHandle:
        async with self._lock:
            if payload is not None:
                self._payload = dict(payload)
            if labels is not None:
                self._labels = dict(labels)
            if hints is not None:
                self._hints = dict(hints)
            if operations is not None:
                self._operations = tuple(operations)
            return await self._publish_locked()

    async def heartbeat_loop(self) -> None:
        while True:
            await anyio.sleep(self._refresh_interval)
            try:
                await self.publish()
            except StateUnavailable:
                logger.warning(
                    "%s Beacon advertisement unavailable; heartbeat will retry "
                    "feature=%s endpoint=%s session=%s advertisement=%s",
                    self._log_label,
                    self.feature_id,
                    self.endpoint,
                    self.session_id,
                    self._handle.advertisement_id if self._handle is not None else None,
                    exc_info=True,
                )

    async def withdraw(self) -> bool:
        async with self._lock:
            handle = self._handle
            self._handle = None
        if handle is None:
            return False
        return await self._service.withdraw(handle, log_label=self._log_label)

    async def _publish_locked(self) -> AdvertisementHandle:
        handle = self._handle
        try:
            if handle is None:
                self._handle = await self._service.advertise(
                    self.feature_id,
                    self.endpoint,
                    self.session_id,
                    advertiser=self.advertiser,
                    advertisement_id=self._advertisement_id,
                    protocol=self._protocol,
                    operations=self._operations,
                    labels=self._labels,
                    hints=self._hints,
                    payload=self._payload,
                    ttl_seconds=self._ttl_seconds,
                    log_label=self._log_label,
                )
            else:
                self._handle = await self._service.refresh(
                    handle,
                    hints=self._hints,
                    labels=self._labels,
                    payload=self._payload,
                    log_label=self._log_label,
                )
            return self._handle
        except StateConflict:
            logger.warning(
                "%s Beacon advertisement refresh conflict; republishing "
                "feature=%s endpoint=%s session=%s advertisement=%s",
                self._log_label,
                self.feature_id,
                self.endpoint,
                self.session_id,
                handle.advertisement_id if handle is not None else self._advertisement_id,
                exc_info=True,
            )
            self._handle = None
            self._handle = await self._service.advertise(
                self.feature_id,
                self.endpoint,
                self.session_id,
                advertiser=self.advertiser,
                advertisement_id=self._advertisement_id,
                protocol=self._protocol,
                operations=self._operations,
                labels=self._labels,
                hints=self._hints,
                payload=self._payload,
                ttl_seconds=self._ttl_seconds,
                log_label=self._log_label,
            )
            return self._handle


class BeaconService:
    """Runtime-facing Beacon API with heartbeat helpers and semantic events."""

    def __init__(
        self,
        discovery: BeaconDiscovery,
    ) -> None:
        self._discovery = discovery

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
        log_label: str = "Beacon",
    ) -> AdvertisementHandle:
        handle = await self._discovery.advertise(
            feature_id,
            endpoint,
            session_id,
            advertiser=advertiser,
            advertisement_id=advertisement_id,
            protocol=protocol,
            operations=operations,
            labels=labels,
            hints=hints,
            payload=payload,
            ttl_seconds=ttl_seconds,
        )
        logger.log(
            _beacon_lifecycle_log_level(handle.feature_id),
            "%s Beacon advertisement announced feature=%s endpoint=%s "
            "session=%s advertisement=%s refresh=%s revision=%s",
            log_label,
            handle.feature_id,
            handle.endpoint,
            handle.session_id,
            handle.advertisement_id,
            handle.refresh_seq,
            handle.revision,
        )
        return handle

    async def refresh(
        self,
        handle: AdvertisementHandle,
        *,
        hints: Mapping[str, Any] | None = None,
        labels: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        log_label: str = "Beacon",
    ) -> AdvertisementHandle:
        refreshed = await self._discovery.refresh(
            handle,
            hints=hints,
            labels=labels,
            payload=payload,
        )
        logger.debug(
            "%s Beacon advertisement heartbeat feature=%s endpoint=%s "
            "session=%s advertisement=%s refresh=%s revision=%s",
            log_label,
            refreshed.feature_id,
            refreshed.endpoint,
            refreshed.session_id,
            refreshed.advertisement_id,
            refreshed.refresh_seq,
            refreshed.revision,
        )
        return refreshed

    async def withdraw(
        self,
        handle: AdvertisementHandle,
        *,
        log_label: str = "Beacon",
    ) -> bool:
        withdrawn = await self._discovery.withdraw(handle)
        if withdrawn:
            logger.log(
                _beacon_lifecycle_log_level(handle.feature_id),
                "%s Beacon advertisement withdrawn feature=%s endpoint=%s "
                "session=%s advertisement=%s revision=%s",
                log_label,
                handle.feature_id,
                handle.endpoint,
                handle.session_id,
                handle.advertisement_id,
                handle.revision,
            )
        return withdrawn

    def advertiser(
        self,
        *,
        feature_id: str,
        endpoint: str | EndpointAddress,
        session_id: str,
        advertiser: str | EndpointAddress | None = None,
        advertisement_id: str | None = None,
        protocol: Mapping[str, str] | BeaconProtocol | None = None,
        operations: tuple[str, ...] | list[str] = (),
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        ttl_seconds: int | None = None,
        refresh_interval: float = 5.0,
        log_label: str = "Beacon",
    ) -> BeaconAdvertiser:
        return BeaconAdvertiser(
            self,
            feature_id=feature_id,
            endpoint=endpoint,
            session_id=session_id,
            advertiser=advertiser,
            advertisement_id=advertisement_id,
            protocol=protocol,
            operations=operations,
            labels=labels,
            hints=hints,
            payload=payload,
            ttl_seconds=ttl_seconds,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )

    async def find(
        self,
        feature_id: str,
        selector: AdvertisementFilter | None = None,
    ) -> tuple[Candidate, ...]:
        return await self._discovery.find(feature_id, selector=selector)

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus:
        return await self._discovery.validate(
            candidate,
            current_sessions=current_sessions,
        )

    @asynccontextmanager
    async def watch_feature(
        self,
        feature_id: str,
    ) -> Any:
        send, receive = anyio.create_memory_object_stream[BeaconFeatureEvent](100)
        known: dict[str, Candidate] = {}

        async def run() -> None:
            try:
                async with self._discovery.watch(feature_id) as changes:
                    async for change in changes:
                        event = _beacon_feature_event(feature_id, change, known)
                        if event is None:
                            continue
                        _log_beacon_feature_event(event)
                        await send.send(event)
            finally:
                await send.aclose()

        caller_exception: BaseException | None = None
        try:
            async with receive, send, anyio.create_task_group() as task_group:
                task_group.start_soon(run)
                try:
                    yield receive
                except BaseException as exc:
                    caller_exception = exc
                finally:
                    task_group.cancel_scope.cancel()
        except BaseExceptionGroup as exc:
            unwrapped = _single_exception_from_group(exc)
            if caller_exception is not None and not isinstance(
                caller_exception, anyio.EndOfStream
            ):
                raise caller_exception from None
            if unwrapped is not None:
                raise unwrapped from exc
            if caller_exception is not None:
                raise caller_exception from None
            raise
        if caller_exception is not None:
            raise caller_exception


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


def _beacon_feature_event(
    feature_id: str,
    change: StateChange,
    known: dict[str, Candidate],
) -> BeaconFeatureEvent | None:
    if change.operation == "put" and change.entry is not None:
        previous = known.get(change.key)
        candidate = _candidate_from_entry(change.entry)
        if candidate is None or candidate.advertisement.feature_id != feature_id:
            known.pop(change.key, None)
            return BeaconFeatureEvent(
                BeaconFeatureEventType.INVALID,
                feature_id,
                change.key,
                previous=previous,
                reason="invalid_advertisement",
                change=change,
            )
        known[change.key] = candidate
        return BeaconFeatureEvent(
            (
                BeaconFeatureEventType.ADVERTISED
                if previous is None
                else BeaconFeatureEventType.UPDATED
            ),
            feature_id,
            change.key,
            candidate=candidate,
            previous=previous,
            change=change,
        )
    if change.operation in {"delete", "expire"}:
        previous = known.pop(change.key, None)
        return BeaconFeatureEvent(
            (
                BeaconFeatureEventType.EXPIRED
                if change.operation == "expire"
                else BeaconFeatureEventType.WITHDRAWN
            ),
            feature_id,
            change.key,
            previous=previous,
            reason=change.operation,
            change=change,
        )
    return None


def _log_beacon_feature_event(event: BeaconFeatureEvent) -> None:
    candidate = event.candidate or event.previous
    advertisement = candidate.advertisement if candidate is not None else None
    if event.event_type == BeaconFeatureEventType.UPDATED:
        return
    if event.event_type == BeaconFeatureEventType.INVALID:
        logger.warning(
            "Beacon advertisement invalid feature=%s key=%s reason=%s",
            event.feature_id,
            event.key,
            event.reason,
        )
        return
    message = "Beacon advertisement %s feature=%s key=%s"
    args: tuple[Any, ...] = (
        event.event_type.value,
        event.feature_id,
        event.key,
    )
    if advertisement is not None:
        message += " endpoint=%s session=%s advertisement=%s refresh=%s revision=%s"
        args += (
            advertisement.endpoint,
            advertisement.session_id,
            advertisement.advertisement_id,
            advertisement.refresh_seq,
            candidate.revision if candidate is not None else None,
        )
    logger.log(_beacon_lifecycle_log_level(event.feature_id), message, *args)


__all__ = [
    "BEACON_ADVERTISEMENT_SCHEMA_ID",
    "BEACON_ADVERTISEMENT_STORE_POLICY",
    "DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME",
    "DEFAULT_BEACON_TTL_SECONDS",
    "AdvertisementHandle",
    "AdvertisementRecord",
    "BeaconAdvertiser",
    "BeaconDiscovery",
    "BeaconEvent",
    "BeaconFeatureEvent",
    "BeaconFeatureEventType",
    "BeaconProtocol",
    "BeaconService",
    "Candidate",
    "CandidateStatus",
    "beacon_advertisement_key",
    "beacon_feature_prefix",
    "parse_beacon_advertisement_key",
]
