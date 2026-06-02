from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Literal, Protocol

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.substrates.nats_kv import (
    KvBucketPolicy,
    KvChange,
    KvConflict,
    KvEntry,
    KvUnavailable,
    NatsKvMaterializedBucket,
)

BEACON_ADVERTISEMENT_SCHEMA_ID = "dev.deckr.beacon.advertisement.v1"
DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME = "deckr_beacon_advertisement_v1"
DEFAULT_BEACON_TTL_SECONDS = 30
DEFAULT_BEACON_REFRESH_SECONDS = 5.0
BEACON_ADVERTISEMENT_STORE_POLICY = KvBucketPolicy(
    bucket=DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    ttl_seconds=float(DEFAULT_BEACON_TTL_SECONDS),
    allow_write_ttl=True,
    description="Beacon advertisement KV",
)

logger = logging.getLogger(__name__)


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


def _beacon_refresh_interval(*, requested: float, ttl_seconds: int | float) -> float:
    ttl = float(ttl_seconds)
    return min(max(float(requested), ttl / 6), ttl * 0.8)


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

    @field_validator("advertisement_id", "feature_id", "session_id")
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
    change: KvChange
    candidate: Candidate | None = None


@dataclass(frozen=True, slots=True)
class BeaconFeatureEvent:
    event_type: BeaconFeatureEventType
    feature_id: str
    key: str
    candidate: Candidate | None = None
    previous: Candidate | None = None
    reason: str | None = None
    change: KvChange | None = None


@dataclass(frozen=True, slots=True)
class BeaconAdvertisementSpec:
    """Input specification for a core-owned Beacon advertisement lifecycle."""

    feature_id: str
    endpoint: str | EndpointAddress
    session_id: str
    advertiser: str | EndpointAddress | None = None
    advertisement_id: str | None = None
    protocol: Mapping[str, str] | BeaconProtocol | None = None
    operations: Sequence[str] = ()
    labels: Mapping[str, str] | None = None
    hints: Mapping[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    ttl_seconds: int | None = None
    refresh_interval: float = DEFAULT_BEACON_REFRESH_SECONDS
    log_label: str = "Beacon"

    def __post_init__(self) -> None:
        feature_id = _require_text(self.feature_id, field_name="Beacon feature id")
        endpoint = parse_endpoint_address(self.endpoint)
        session_id = _require_text(self.session_id, field_name="Beacon session id")
        advertiser = (
            parse_endpoint_address(self.advertiser)
            if self.advertiser is not None
            else endpoint
        )
        if self.refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        operations = tuple(
            _require_text(item, field_name="Beacon operation")
            for item in self.operations
        )
        advertisement_id = (
            _require_text(
                self.advertisement_id,
                field_name="Beacon advertisement id",
            )
            if self.advertisement_id is not None
            else None
        )
        object.__setattr__(self, "feature_id", feature_id)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "advertiser", advertiser)
        object.__setattr__(self, "advertisement_id", advertisement_id)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "labels", None if self.labels is None else dict(self.labels))
        object.__setattr__(self, "hints", None if self.hints is None else dict(self.hints))
        object.__setattr__(self, "payload", None if self.payload is None else dict(self.payload))


@dataclass(slots=True, eq=False)
class _BeaconSubscriber:
    send: anyio.abc.ObjectSendStream[BeaconFeatureEvent]
    feature_id: str | None
    selector: AdvertisementFilter | None
    known_keys: set[str] = field(default_factory=set)


class Beacon:
    """Direct KV-backed Beacon runtime with a materialized advertisement view."""

    def __init__(
        self,
        bucket: NatsKvMaterializedBucket | Any,
        *,
        default_ttl_seconds: int = DEFAULT_BEACON_TTL_SECONDS,
        buffer_size: int = 100,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be greater than zero")
        self._bucket = (
            bucket
            if _is_materialized_bucket(bucket)
            else NatsKvMaterializedBucket(bucket=bucket, buffer_size=buffer_size)
        )
        self._default_ttl_seconds = default_ttl_seconds
        self._buffer_size = buffer_size
        self._ready = anyio.Event()
        self._started = False
        self._closed = False
        self._task_group: anyio.abc.TaskGroup | None = None
        self._entries_by_key: dict[str, Candidate] = {}
        self._revision_by_key: dict[str, int] = {}
        self._invalid_by_key: dict[str, tuple[int, str]] = {}
        self._keys_by_feature: dict[str, set[str]] = {}
        self._keys_by_feature_endpoint: dict[tuple[str, str, str], set[str]] = {}
        self._subscribers: set[_BeaconSubscriber] = set()
        self._leases: set[BeaconAdvertisementLease] = set()
        self._lock = anyio.Lock()

    @property
    def bucket(self) -> str:
        return self._bucket.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        self._bucket.start(task_group)
        if not self._started:
            self._started = True
            task_group.start_soon(self._event_loop)
        for lease in tuple(self._leases):
            lease.start(task_group)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            leases = tuple(self._leases)
            self._leases.clear()
        for lease in leases:
            await lease.aclose()

    async def advertise(
        self,
        spec: BeaconAdvertisementSpec,
        *,
        cleanup_stale_same_endpoint: bool = True,
    ) -> BeaconAdvertisementLease:
        if self._closed:
            raise KvUnavailable("Beacon is closed")
        if self._started:
            await self.wait_ready()
        if cleanup_stale_same_endpoint:
            await self.remove_stale_advertisements(spec)
        lease = BeaconAdvertisementLease(self, spec)
        handle = await lease._publish_initial()
        logger.log(
            _beacon_lifecycle_log_level(handle.feature_id),
            "%s Beacon advertisement announced feature=%s endpoint=%s "
            "session=%s advertisement=%s refresh=%s revision=%s",
            spec.log_label,
            handle.feature_id,
            handle.endpoint,
            handle.session_id,
            handle.advertisement_id,
            handle.refresh_seq,
            handle.revision,
        )
        async with self._lock:
            self._leases.add(lease)
        if self._task_group is not None:
            lease.start(self._task_group)
        return lease

    def candidates(
        self,
        feature_id: str,
        *,
        selector: AdvertisementFilter | None = None,
    ) -> tuple[Candidate, ...]:
        feature_id = _require_text(feature_id, field_name="Beacon feature id")
        keys = tuple(self._keys_by_feature.get(feature_id, ()))
        candidates = [
            candidate
            for key in keys
            if (candidate := self._entries_by_key.get(key)) is not None
            and (
                selector is None
                or _selector_accepts(selector, candidate.advertisement)
            )
        ]
        return tuple(sorted(candidates, key=_candidate_newest_sort_key))

    def get(
        self,
        *,
        feature_id: str,
        advertisement_id: str,
    ) -> Candidate | None:
        key = beacon_advertisement_key(
            feature_id=_require_text(feature_id, field_name="Beacon feature id"),
            advertisement_id=_require_text(
                advertisement_id,
                field_name="Beacon advertisement id",
            ),
        )
        return self._entries_by_key.get(key)

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus:
        if self._started and not self._ready.is_set():
            return CandidateStatus.UNAVAILABLE
        current = self._entries_by_key.get(candidate.key)
        if current is None:
            if candidate.key in self._invalid_by_key:
                return CandidateStatus.SCHEMA_INVALID
            return CandidateStatus.MISSING
        advertisement = current.advertisement
        if advertisement.feature_id != candidate.advertisement.feature_id:
            return CandidateStatus.FEATURE_MISMATCH
        if current.revision < candidate.revision:
            return CandidateStatus.MISSING
        if current_sessions is not None:
            current_session = current_sessions.get(str(advertisement.advertiser))
            if current_session is not None and advertisement.session_id != current_session:
                return CandidateStatus.SESSION_MISMATCH
        return CandidateStatus.CANDIDATE

    @asynccontextmanager
    async def watch(
        self,
        feature_id: str | None = None,
        *,
        selector: AdvertisementFilter | None = None,
        replay_current: bool = True,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[BeaconFeatureEvent]]:
        if feature_id is not None:
            feature_id = _require_text(feature_id, field_name="Beacon feature id")
        if self._started:
            await self.wait_ready()
        send, receive = anyio.create_memory_object_stream[BeaconFeatureEvent](
            max_buffer_size=self._buffer_size
        )
        subscriber = _BeaconSubscriber(send, feature_id, selector)
        initial: tuple[BeaconFeatureEvent, ...] = ()
        if replay_current:
            initial_candidates = self._matching_candidates(feature_id, selector)
            subscriber.known_keys.update(candidate.key for candidate in initial_candidates)
            initial = tuple(
                BeaconFeatureEvent(
                    BeaconFeatureEventType.ADVERTISED,
                    candidate.advertisement.feature_id,
                    candidate.key,
                    candidate=candidate,
                )
                for candidate in initial_candidates
            )
        async with self._lock:
            self._subscribers.add(subscriber)
        try:
            async with send, receive:
                for event in initial:
                    await send.send(event)
                yield receive
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)

    async def remove_stale_advertisements(
        self,
        spec: BeaconAdvertisementSpec,
    ) -> int:
        if self._started:
            await self.wait_ready()
        keys = tuple(
            self._keys_by_feature_endpoint.get(
                (spec.feature_id, str(spec.advertiser), str(spec.endpoint)),
                (),
            )
        )
        removed = 0
        for key in keys:
            candidate = self._entries_by_key.get(key)
            if candidate is None:
                continue
            if _advertisement_matches_spec_config(candidate.advertisement, spec):
                continue
            try:
                await self._delete_candidate(candidate)
                removed += 1
            except (KvConflict, KvUnavailable):
                logger.debug(
                    "Could not remove stale Beacon advertisement key=%s",
                    candidate.key,
                    exc_info=True,
                )
        return removed

    async def _event_loop(self) -> None:
        async with self._bucket.subscribe() as changes:
            await self._bucket.wait_ready()
            await self._rebuild_from_bucket()
            self._ready.set()
            async for change in changes:
                await self._apply_kv_change(change)

    async def _rebuild_from_bucket(self) -> None:
        entries_by_key: dict[str, Candidate] = {}
        revision_by_key: dict[str, int] = {}
        invalid_by_key: dict[str, tuple[int, str]] = {}
        keys_by_feature: dict[str, set[str]] = {}
        keys_by_feature_endpoint: dict[tuple[str, str, str], set[str]] = {}
        for entry in self._bucket.items_cached():
            revision_by_key[entry.key] = entry.revision
            candidate, reason = _candidate_from_entry(entry)
            if candidate is None:
                invalid_by_key[entry.key] = (entry.revision, reason)
                continue
            entries_by_key[entry.key] = candidate
            _index_candidate(
                candidate,
                keys_by_feature=keys_by_feature,
                keys_by_feature_endpoint=keys_by_feature_endpoint,
            )
        async with self._lock:
            self._entries_by_key = entries_by_key
            self._revision_by_key = revision_by_key
            self._invalid_by_key = invalid_by_key
            self._keys_by_feature = keys_by_feature
            self._keys_by_feature_endpoint = keys_by_feature_endpoint

    async def _create_advertisement(
        self,
        spec: BeaconAdvertisementSpec,
        *,
        advertisement_id: str,
    ) -> AdvertisementHandle:
        record = _record_from_spec(
            spec,
            advertisement_id=advertisement_id,
            default_ttl_seconds=self._default_ttl_seconds,
        )
        key = beacon_advertisement_key(
            feature_id=record.feature_id,
            advertisement_id=record.advertisement_id,
        )
        entry = await self._bucket.create(key, record, ttl=record.ttl_seconds)
        await self._apply_kv_change(KvChange(self.bucket, key, entry.revision, "put", entry))
        return _advertisement_handle(key, record, entry.revision)

    async def _refresh_advertisement(
        self,
        handle: AdvertisementHandle,
        *,
        protocol: Mapping[str, str] | BeaconProtocol | None,
        operations: Sequence[str],
        labels: Mapping[str, str],
        hints: Mapping[str, Any],
        payload: Mapping[str, Any] | None,
        force_refresh: bool,
    ) -> AdvertisementHandle:
        current = self._entries_by_key.get(handle.key)
        if current is None:
            exact = await self._bucket.get_exact(handle.key)
            if exact is None:
                raise KvConflict(f"Beacon advertisement {handle.key!r} is missing")
            current, _reason = _candidate_from_entry(exact)
            if current is None:
                raise KvConflict(f"Beacon advertisement {handle.key!r} is invalid")
        record = current.advertisement
        if not _advertisement_matches_handle(record, handle):
            raise KvConflict(f"Beacon advertisement {handle.key!r} changed owner")
        refreshed = _updated_record(
            record,
            protocol=protocol,
            operations=operations,
            labels=labels,
            hints=hints,
            payload=payload,
            force_refresh=force_refresh,
        )
        if refreshed is record:
            return handle
        try:
            entry = await self._bucket.update(
                handle.key,
                refreshed,
                revision=current.revision,
                ttl=refreshed.ttl_seconds,
            )
        except KvConflict:
            exact = await self._bucket.get_exact(handle.key)
            if exact is None:
                raise
            exact_candidate, _reason = _candidate_from_entry(exact)
            if exact_candidate is None:
                raise
            exact_record = exact_candidate.advertisement
            if not _advertisement_matches_handle(exact_record, handle):
                raise
            refreshed = _updated_record(
                exact_record,
                protocol=protocol,
                operations=operations,
                labels=labels,
                hints=hints,
                payload=payload,
                force_refresh=True,
            )
            entry = await self._bucket.update(
                handle.key,
                refreshed,
                revision=exact_candidate.revision,
                ttl=refreshed.ttl_seconds,
            )
        await self._apply_kv_change(
            KvChange(self.bucket, handle.key, entry.revision, "put", entry)
        )
        return _advertisement_handle(handle.key, refreshed, entry.revision)

    async def _withdraw_advertisement(self, handle: AdvertisementHandle) -> bool:
        current = await self._bucket.get_exact(handle.key)
        if current is None:
            return False
        candidate, _reason = _candidate_from_entry(current)
        if candidate is None:
            raise KvConflict(f"Beacon advertisement {handle.key!r} is invalid")
        if not _advertisement_matches_handle(candidate.advertisement, handle):
            raise KvConflict(f"Beacon advertisement {handle.key!r} changed owner")
        await self._bucket.delete(handle.key, revision=current.revision)
        marker_revision = _bucket_revision_cached(self._bucket, handle.key) or (
            current.revision + 1
        )
        await self._apply_kv_change(
            KvChange(self.bucket, handle.key, marker_revision, "delete")
        )
        return True

    async def _delete_candidate(self, candidate: Candidate) -> None:
        await self._bucket.delete(candidate.key, revision=candidate.revision)
        marker_revision = _bucket_revision_cached(self._bucket, candidate.key) or (
            candidate.revision + 1
        )
        await self._apply_kv_change(
            KvChange(self.bucket, candidate.key, marker_revision, "delete")
        )

    async def _forget_lease(self, lease: BeaconAdvertisementLease) -> None:
        async with self._lock:
            self._leases.discard(lease)

    async def _apply_kv_change(self, change: KvChange) -> None:
        async with self._lock:
            events = self._apply_kv_change_locked(change)
        for subscriber, event in events:
            try:
                subscriber.send.send_nowait(event)
            except anyio.WouldBlock:
                logger.warning(
                    "Beacon watcher buffer full feature=%s key=%s",
                    subscriber.feature_id,
                    event.key,
                )
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.discard(subscriber)

    def _apply_kv_change_locked(
        self,
        change: KvChange,
    ) -> tuple[tuple[_BeaconSubscriber, BeaconFeatureEvent], ...]:
        current_revision = self._revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            return ()
        previous = self._entries_by_key.get(change.key)
        feature_id = (
            previous.advertisement.feature_id if previous is not None else ""
        )
        candidate: Candidate | None = None
        event_type: BeaconFeatureEventType | None = None
        reason: str | None = None

        self._revision_by_key[change.key] = change.revision
        if change.operation == "put" and change.entry is not None:
            candidate, reason = _candidate_from_entry(change.entry)
            if candidate is None:
                parsed = parse_beacon_advertisement_key(change.key)
                feature_id = parsed[0] if parsed is not None else feature_id
                self._invalid_by_key[change.key] = (change.revision, reason)
                if previous is not None:
                    self._remove_candidate(previous)
                event_type = BeaconFeatureEventType.INVALID
            else:
                feature_id = candidate.advertisement.feature_id
                self._invalid_by_key.pop(change.key, None)
                if previous is not None:
                    self._remove_candidate(previous)
                self._entries_by_key[change.key] = candidate
                _index_candidate(
                    candidate,
                    keys_by_feature=self._keys_by_feature,
                    keys_by_feature_endpoint=self._keys_by_feature_endpoint,
                )
                event_type = (
                    BeaconFeatureEventType.ADVERTISED
                    if previous is None
                    else BeaconFeatureEventType.UPDATED
                )
        elif change.operation in {"delete", "expire"}:
            self._invalid_by_key.pop(change.key, None)
            if previous is not None:
                self._remove_candidate(previous)
                feature_id = previous.advertisement.feature_id
            event_type = (
                BeaconFeatureEventType.EXPIRED
                if change.operation == "expire"
                else BeaconFeatureEventType.WITHDRAWN
            )
            reason = change.operation
        if event_type is None:
            return ()

        base_event = BeaconFeatureEvent(
            event_type,
            feature_id,
            change.key,
            candidate=candidate,
            previous=previous,
            reason=reason,
            change=change,
        )
        _log_beacon_feature_event(base_event)
        return tuple(
            (subscriber, item)
            for subscriber in tuple(self._subscribers)
            if (
                item := _event_for_subscriber(
                    subscriber,
                    base_event,
                    candidate=candidate,
                    previous=previous,
                )
            )
            is not None
        )

    def _remove_candidate(self, candidate: Candidate) -> None:
        key = candidate.key
        self._entries_by_key.pop(key, None)
        feature_keys = self._keys_by_feature.get(candidate.advertisement.feature_id)
        if feature_keys is not None:
            feature_keys.discard(key)
            if not feature_keys:
                self._keys_by_feature.pop(candidate.advertisement.feature_id, None)
        endpoint_key = _feature_endpoint_key(candidate.advertisement)
        endpoint_keys = self._keys_by_feature_endpoint.get(endpoint_key)
        if endpoint_keys is not None:
            endpoint_keys.discard(key)
            if not endpoint_keys:
                self._keys_by_feature_endpoint.pop(endpoint_key, None)

    def _matching_candidates(
        self,
        feature_id: str | None,
        selector: AdvertisementFilter | None,
    ) -> tuple[Candidate, ...]:
        if feature_id is None:
            candidates = tuple(self._entries_by_key.values())
        else:
            candidates = tuple(
                candidate
                for key in self._keys_by_feature.get(feature_id, ())
                if (candidate := self._entries_by_key.get(key)) is not None
            )
        if selector is not None:
            candidates = tuple(
                candidate
                for candidate in candidates
                if _selector_accepts(selector, candidate.advertisement)
            )
        return tuple(sorted(candidates, key=_candidate_newest_sort_key))


class BeaconAdvertisementLease:
    """Core-owned Beacon advertisement lease with heartbeat refreshes."""

    def __init__(self, beacon: Beacon, spec: BeaconAdvertisementSpec) -> None:
        self._beacon = beacon
        self.spec = spec
        self.feature_id = spec.feature_id
        self.endpoint = spec.endpoint
        self.session_id = spec.session_id
        self.advertiser = spec.advertiser
        self._advertisement_id = spec.advertisement_id or str(uuid.uuid4())
        self._protocol = spec.protocol
        self._operations = tuple(spec.operations)
        self._labels = dict(spec.labels or {})
        self._hints = dict(spec.hints or {})
        self._payload = dict(spec.payload) if spec.payload is not None else None
        self._refresh_interval = _beacon_refresh_interval(
            requested=spec.refresh_interval,
            ttl_seconds=spec.ttl_seconds or beacon._default_ttl_seconds,  # noqa: SLF001
        )
        self._log_label = spec.log_label
        self._handle: AdvertisementHandle | None = None
        self._last_refresh_at: float | None = None
        self._lock = anyio.Lock()
        self._started = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def handle(self) -> AdvertisementHandle:
        if self._handle is None:
            raise RuntimeError("Beacon advertisement has not been published")
        return self._handle

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        if self._closed or self._started:
            return
        self._started = True
        task_group.start_soon(self._heartbeat_loop)

    def start_soon(self, start_soon: Callable[..., object] | None = None) -> None:
        if start_soon is None or self._closed or self._started:
            return
        self._started = True
        start_soon(self._heartbeat_loop)

    async def update(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        operations: Sequence[str] | None = None,
    ) -> AdvertisementHandle:
        async with self._lock:
            if payload is not None:
                self._payload = dict(payload)
            if labels is not None:
                self._labels = dict(labels)
            if hints is not None:
                self._hints = dict(hints)
            if operations is not None:
                self._operations = tuple(
                    _require_text(item, field_name="Beacon operation")
                    for item in operations
                )
            return await self._publish_locked(force_refresh=False)

    async def withdraw(self) -> bool:
        async with self._lock:
            handle = self._handle
            self._handle = None
            self._closed = True
        try:
            return (
                False
                if handle is None
                else await self._beacon._withdraw_advertisement(handle)
            )
        finally:
            await self._beacon._forget_lease(self)

    async def aclose(self) -> None:
        try:
            await self.withdraw()
        except (KvConflict, KvUnavailable):
            logger.debug(
                "Could not withdraw Beacon advertisement feature=%s "
                "endpoint=%s session=%s advertisement=%s",
                self.feature_id,
                self.endpoint,
                self.session_id,
                self._advertisement_id,
                exc_info=True,
            )

    async def _publish_initial(self) -> AdvertisementHandle:
        async with self._lock:
            return await self._publish_locked(force_refresh=True)

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await anyio.sleep(self._refresh_interval)
            if self._closed:
                return
            try:
                async with self._lock:
                    await self._publish_locked(force_refresh=True)
            except (KvConflict, KvUnavailable):
                logger.warning(
                    "%s Beacon advertisement heartbeat failed feature=%s "
                    "endpoint=%s session=%s advertisement=%s",
                    self._log_label,
                    self.feature_id,
                    self.endpoint,
                    self.session_id,
                    self._advertisement_id,
                    exc_info=True,
                )

    async def _publish_locked(self, *, force_refresh: bool) -> AdvertisementHandle:
        if self._closed:
            raise KvConflict("Beacon advertisement is closed")
        if self._handle is None:
            self._handle = await self._beacon._create_advertisement(
                self.spec,
                advertisement_id=self._advertisement_id,
            )
            self._last_refresh_at = monotonic()
            return self._handle
        if force_refresh and not self._refresh_due():
            return self._handle
        old_revision = self._handle.revision
        refreshed = await self._beacon._refresh_advertisement(
            self._handle,
            protocol=self._protocol,
            operations=self._operations,
            labels=self._labels,
            hints=self._hints,
            payload=self._payload,
            force_refresh=force_refresh,
        )
        if refreshed.revision != old_revision:
            logger.debug(
                "%s Beacon advertisement heartbeat feature=%s endpoint=%s "
                "session=%s advertisement=%s refresh=%s revision=%s",
                self._log_label,
                refreshed.feature_id,
                refreshed.endpoint,
                refreshed.session_id,
                refreshed.advertisement_id,
                refreshed.refresh_seq,
                refreshed.revision,
            )
        self._handle = refreshed
        if refreshed.revision != old_revision:
            self._last_refresh_at = monotonic()
        return refreshed

    def _refresh_due(self) -> bool:
        return (
            self._last_refresh_at is None
            or monotonic() - self._last_refresh_at >= self._refresh_interval
        )


def _is_materialized_bucket(value: Any) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            "start",
            "wait_ready",
            "get_exact",
            "items_cached",
            "subscribe",
            "create",
            "update",
            "delete",
        )
    )


def _bucket_revision_cached(bucket: Any, key: str) -> int | None:
    revision_cached = getattr(bucket, "revision_cached", None)
    if revision_cached is None:
        return None
    return revision_cached(key)


def _record_from_spec(
    spec: BeaconAdvertisementSpec,
    *,
    advertisement_id: str,
    default_ttl_seconds: int,
) -> AdvertisementRecord:
    now = _now_utc()
    return AdvertisementRecord(
        advertisementId=advertisement_id,
        featureId=spec.feature_id,
        advertiser=spec.advertiser,
        endpoint=spec.endpoint,
        sessionId=spec.session_id,
        refreshSeq=1,
        ttlSeconds=spec.ttl_seconds or default_ttl_seconds,
        protocol=spec.protocol,
        operations=tuple(spec.operations),
        labels=spec.labels or {},
        hints=spec.hints or {},
        payload=spec.payload,
        createdAt=now,
        updatedAt=now,
    )


def _updated_record(
    record: AdvertisementRecord,
    *,
    protocol: Mapping[str, str] | BeaconProtocol | None,
    operations: Sequence[str],
    labels: Mapping[str, str],
    hints: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    force_refresh: bool,
) -> AdvertisementRecord:
    update = {
        "protocol": protocol,
        "operations": tuple(operations),
        "labels": freeze_json(labels),
        "hints": freeze_json(hints),
        "payload": freeze_json(payload) if payload is not None else None,
    }
    changed = (
        record.protocol != update["protocol"]
        or record.operations != update["operations"]
        or record.labels != update["labels"]
        or record.hints != update["hints"]
        or record.payload != update["payload"]
    )
    if not changed and not force_refresh:
        return record
    return record.model_copy(
        update={
            **update,
            "refresh_seq": record.refresh_seq + 1,
            "updated_at": _now_utc(),
        }
    )


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


def _advertisement_matches_spec_config(
    record: AdvertisementRecord,
    spec: BeaconAdvertisementSpec,
) -> bool:
    return (
        record.feature_id == spec.feature_id
        and record.advertiser == spec.advertiser
        and record.endpoint == spec.endpoint
        and record.session_id == spec.session_id
        and record.protocol == spec.protocol
        and record.operations == tuple(spec.operations)
        and record.labels == freeze_json(spec.labels or {})
        and record.hints == freeze_json(spec.hints or {})
        and record.payload
        == (freeze_json(spec.payload) if spec.payload is not None else None)
    )


def _candidate_from_entry(entry: KvEntry) -> tuple[Candidate | None, str]:
    try:
        advertisement = AdvertisementRecord.model_validate(entry.value)
    except ValueError:
        return None, "invalid_schema"
    parsed = parse_beacon_advertisement_key(entry.key)
    if parsed is None:
        return None, "invalid_key"
    feature_id, advertisement_id = parsed
    if (
        feature_id != advertisement.feature_id
        or advertisement_id != advertisement.advertisement_id
    ):
        return None, "identity_mismatch"
    return (
        Candidate(
            key=entry.key,
            advertisement=advertisement,
            revision=entry.revision,
            observed_at=_now_utc(),
        ),
        "",
    )


def _candidate_newest_sort_key(candidate: Candidate) -> tuple[int, float, int, str]:
    updated_at = candidate.advertisement.updated_at
    updated_timestamp = (
        updated_at.astimezone(UTC).timestamp()
        if updated_at is not None
        else float("-inf")
    )
    return (
        -candidate.revision,
        -updated_timestamp,
        -candidate.advertisement.refresh_seq,
        candidate.advertisement.advertisement_id,
    )


def _selector_accepts(
    selector: AdvertisementFilter,
    advertisement: AdvertisementRecord,
) -> bool:
    accepts = getattr(selector, "accepts", None)
    if accepts is not None:
        return bool(accepts(advertisement))
    return bool(selector(advertisement))


def _feature_endpoint_key(record: AdvertisementRecord) -> tuple[str, str, str]:
    return (record.feature_id, str(record.advertiser), str(record.endpoint))


def _index_candidate(
    candidate: Candidate,
    *,
    keys_by_feature: dict[str, set[str]],
    keys_by_feature_endpoint: dict[tuple[str, str, str], set[str]],
) -> None:
    keys_by_feature.setdefault(candidate.advertisement.feature_id, set()).add(
        candidate.key
    )
    keys_by_feature_endpoint.setdefault(
        _feature_endpoint_key(candidate.advertisement),
        set(),
    ).add(candidate.key)


def _event_for_subscriber(
    subscriber: _BeaconSubscriber,
    event: BeaconFeatureEvent,
    *,
    candidate: Candidate | None,
    previous: Candidate | None,
) -> BeaconFeatureEvent | None:
    key = event.key
    if event.event_type == BeaconFeatureEventType.INVALID:
        if not _subscriber_accepts_feature(subscriber, event.feature_id):
            return None
        subscriber.known_keys.discard(key)
        return event
    if candidate is not None:
        matches = _subscriber_accepts_candidate(subscriber, candidate)
        known = key in subscriber.known_keys
        if matches:
            subscriber.known_keys.add(key)
            return BeaconFeatureEvent(
                BeaconFeatureEventType.UPDATED
                if known
                else BeaconFeatureEventType.ADVERTISED,
                candidate.advertisement.feature_id,
                key,
                candidate=candidate,
                previous=previous,
                change=event.change,
            )
        if known:
            subscriber.known_keys.discard(key)
            return BeaconFeatureEvent(
                BeaconFeatureEventType.WITHDRAWN,
                event.feature_id,
                key,
                previous=previous,
                reason="selector_mismatch",
                change=event.change,
            )
        return None
    if key not in subscriber.known_keys:
        return None
    subscriber.known_keys.discard(key)
    return event


def _subscriber_accepts_feature(
    subscriber: _BeaconSubscriber,
    feature_id: str,
) -> bool:
    return subscriber.feature_id is None or subscriber.feature_id == feature_id


def _subscriber_accepts_candidate(
    subscriber: _BeaconSubscriber,
    candidate: Candidate,
) -> bool:
    if not _subscriber_accepts_feature(subscriber, candidate.advertisement.feature_id):
        return False
    return (
        subscriber.selector is None
        or _selector_accepts(subscriber.selector, candidate.advertisement)
    )


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
    "DEFAULT_BEACON_REFRESH_SECONDS",
    "DEFAULT_BEACON_TTL_SECONDS",
    "AdvertisementHandle",
    "AdvertisementRecord",
    "Beacon",
    "BeaconAdvertisementLease",
    "BeaconAdvertisementSpec",
    "BeaconEvent",
    "BeaconFeatureEvent",
    "BeaconFeatureEventType",
    "BeaconProtocol",
    "Candidate",
    "CandidateStatus",
    "beacon_advertisement_key",
    "beacon_feature_prefix",
    "parse_beacon_advertisement_key",
]
