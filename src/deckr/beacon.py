from __future__ import annotations

import logging
import random
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Any, Generic, Literal, Protocol, TypeVar

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr._authority_buckets import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    DEFAULT_BEACON_TTL_SECONDS,
)
from deckr._beacon._view import BeaconView
from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.substrates.nats_kv import (
    KvConflict,
    KvEntry,
    KvUnavailable,
    NatsKvMaterializedBucket,
)

BEACON_ADVERTISEMENT_SCHEMA_ID = "dev.deckr.beacon.advertisement.v1"

logger = logging.getLogger(__name__)


def _beacon_lifecycle_log_level(feature_id: str) -> int:
    del feature_id
    return logging.INFO


class _BeaconAdvertisementMissing(KvConflict):
    pass


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

T = TypeVar("T")


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


def _beacon_refresh_interval(
    *,
    requested: float | None,
    ttl_seconds: int | float,
) -> float:
    ttl = float(ttl_seconds)
    upper = ttl * 0.75
    lower = ttl * 0.5
    if requested is not None:
        lower = min(max(float(requested), lower), upper)
    return random.uniform(lower, upper)


def _beacon_ttl_seconds(value: float | int | None, *, bucket: str) -> int:
    if value is None:
        raise KvUnavailable(f"Beacon advertisement bucket {bucket!r} must be TTL-bound")
    ttl = float(value)
    rounded = int(ttl)
    if ttl <= 0 or abs(ttl - rounded) > 0.001:
        raise KvUnavailable(
            f"Beacon advertisement bucket {bucket!r} TTL must be positive "
            "whole seconds"
        )
    return rounded


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
class BeaconWatchSnapshot:
    version: int
    current: bool
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class BeaconWatchChange:
    version: int
    current: bool
    candidates: tuple[Candidate, ...]
    changed_keys: frozenset[str]
    resnapshot_required: bool


BeaconDirectoryParser = Callable[[Candidate], T | list[T] | tuple[T, ...] | None]
BeaconDirectoryPredicate = Callable[[T], bool]
BeaconDirectorySelector = Callable[[Collection[T]], T | None]


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
    refresh_interval: float | None = None
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
        if self.refresh_interval is not None and self.refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
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


class Beacon:
    """Direct KV-backed Beacon runtime with a materialized advertisement view."""

    def __init__(
        self,
        bucket: NatsKvMaterializedBucket | Any,
    ) -> None:
        self._bucket = (
            bucket if _is_materialized_bucket(bucket) else NatsKvMaterializedBucket(bucket=bucket)
        )
        self._exact = self._bucket.exact_bucket
        self._view = BeaconView[Candidate](
            self._bucket,
            parse_entry=_candidate_from_entry,
            candidate_key=lambda candidate: candidate.key,
            candidate_feature=lambda candidate: candidate.advertisement.feature_id,
            candidate_endpoint_key=lambda candidate: _feature_endpoint_key(
                candidate.advertisement
            ),
            sort_key=_candidate_newest_sort_key,
        )
        self._started = False
        self._closed = False
        self._task_group: anyio.abc.TaskGroup | None = None
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
            task_group.start_soon(self._view.run)
        for lease in tuple(self._leases):
            lease.start(task_group)

    async def wait_ready(self) -> None:
        await self._view.wait_ready()

    def is_current(self) -> bool:
        return self._bucket.is_current() and self._view.is_current()

    async def wait_current(self) -> None:
        await self._view.wait_current()

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            leases = tuple(self._leases)
            self._leases.clear()
        for lease in leases:
            await lease.aclose()
        await self._view.aclose()

    async def advertise(
        self,
        spec: BeaconAdvertisementSpec,
        *,
        cleanup_stale_same_endpoint: bool = True,
    ) -> BeaconAdvertisementLease:
        if self._closed:
            raise KvUnavailable("Beacon is closed")
        if self._started:
            await self.wait_current()
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
        self._raise_if_cache_unavailable()
        feature_id = _require_text(feature_id, field_name="Beacon feature id")
        candidates = self._view.candidates(feature_id)
        if selector is None:
            return candidates
        return tuple(
            candidate
            for candidate in candidates
            if _selector_accepts(selector, candidate.advertisement)
        )

    def get(
        self,
        *,
        feature_id: str,
        advertisement_id: str,
    ) -> Candidate | None:
        self._raise_if_cache_unavailable()
        key = beacon_advertisement_key(
            feature_id=_require_text(feature_id, field_name="Beacon feature id"),
            advertisement_id=_require_text(
                advertisement_id,
                field_name="Beacon advertisement id",
            ),
        )
        return self._view.get(key)

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus:
        if self._started:
            if not self.is_current():
                return CandidateStatus.UNAVAILABLE
        current = self._view.get(candidate.key)
        if current is None:
            if self._view.is_invalid(candidate.key):
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
    ) -> AsyncIterator[AsyncIterator[BeaconWatchSnapshot | BeaconWatchChange]]:
        if feature_id is not None:
            feature_id = _require_text(feature_id, field_name="Beacon feature id")
        if self._started:
            await self.wait_current()
        async with self._view.subscribe(feature_id) as subscription:

            async def stream() -> AsyncIterator[BeaconWatchSnapshot | BeaconWatchChange]:
                initial_candidates = _filter_candidates(
                    subscription.initial.candidates,
                    selector,
                )
                known_keys = {candidate.key for candidate in initial_candidates}
                last_current = subscription.initial.current
                yield BeaconWatchSnapshot(
                    version=subscription.initial.version,
                    current=last_current,
                    candidates=initial_candidates,
                )
                async for wakeup in subscription:
                    try:
                        snapshot = await self._view.snapshot(feature_id)
                    except anyio.ClosedResourceError:
                        return
                    candidates = _filter_candidates(snapshot.candidates, selector)
                    current_keys = {candidate.key for candidate in candidates}
                    relevant = wakeup.changed & (known_keys | current_keys)
                    if (
                        not wakeup.resnapshot_required
                        and not relevant
                        and snapshot.current == last_current
                    ):
                        known_keys = current_keys
                        continue
                    known_keys = current_keys
                    last_current = snapshot.current
                    yield BeaconWatchChange(
                        version=snapshot.version,
                        current=snapshot.current,
                        candidates=candidates,
                        changed_keys=frozenset(relevant),
                        resnapshot_required=wakeup.resnapshot_required,
                    )

            yield stream()

    async def remove_stale_advertisements(
        self,
        spec: BeaconAdvertisementSpec,
    ) -> int:
        if self._started:
            await self.wait_current()
        removed = 0
        for candidate in self._view.candidates_for_endpoint(
            (spec.feature_id, str(spec.advertiser), str(spec.endpoint))
        ):
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
        await self._view.run()

    def _raise_if_cache_unavailable(self) -> None:
        if self._started and not self.is_current():
            raise KvUnavailable("Beacon materialized view is not current")

    async def _create_advertisement(
        self,
        spec: BeaconAdvertisementSpec,
        *,
        advertisement_id: str,
    ) -> AdvertisementHandle:
        ttl_seconds = await self._ttl_seconds()
        record = _record_from_spec(
            spec,
            advertisement_id=advertisement_id,
            ttl_seconds=ttl_seconds,
        )
        key = beacon_advertisement_key(
            feature_id=record.feature_id,
            advertisement_id=record.advertisement_id,
        )
        entry = await self._exact.create(key, record, ttl=ttl_seconds)
        await self._observe_write(key, entry.revision)
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
        ttl_seconds = await self._ttl_seconds()
        current = self._view.get(handle.key)
        if current is None:
            exact = await _exact_get(self._exact, handle.key)
            if exact is None:
                raise _BeaconAdvertisementMissing(
                    f"Beacon advertisement {handle.key!r} is missing"
                )
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
            ttl_seconds=ttl_seconds,
            force_refresh=force_refresh,
        )
        if refreshed is record:
            return handle
        try:
            entry = await self._exact.update(
                handle.key,
                refreshed,
                revision=current.revision,
                ttl=ttl_seconds,
            )
        except KvConflict as exc:
            exact = await _exact_get(self._exact, handle.key)
            if exact is None:
                raise _BeaconAdvertisementMissing(
                    f"Beacon advertisement {handle.key!r} is missing"
                ) from exc
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
                ttl_seconds=ttl_seconds,
                force_refresh=True,
            )
            entry = await self._exact.update(
                handle.key,
                refreshed,
                revision=exact_candidate.revision,
                ttl=ttl_seconds,
            )
        await self._observe_write(handle.key, entry.revision)
        return _advertisement_handle(handle.key, refreshed, entry.revision)

    async def _ttl_seconds(self) -> int:
        ttl_seconds = getattr(self._exact, "ttl_seconds", None)
        value = None
        if ttl_seconds is not None:
            value = ttl_seconds()
            if hasattr(value, "__await__"):
                value = await value
        return _beacon_ttl_seconds(value, bucket=self.bucket)

    async def _withdraw_advertisement(self, handle: AdvertisementHandle) -> bool:
        current = await _exact_get(self._exact, handle.key)
        if current is None:
            return False
        candidate, _reason = _candidate_from_entry(current)
        if candidate is None:
            raise KvConflict(f"Beacon advertisement {handle.key!r} is invalid")
        if not _advertisement_matches_handle(candidate.advertisement, handle):
            raise KvConflict(f"Beacon advertisement {handle.key!r} changed owner")
        marker_revision = await self._exact.delete(
            handle.key,
            revision=current.revision,
        )
        if marker_revision is not None:
            await self._observe_write(handle.key, marker_revision)
        return True

    async def _delete_candidate(self, candidate: Candidate) -> None:
        marker_revision = await self._exact.delete(
            candidate.key,
            revision=candidate.revision,
        )
        if marker_revision is not None:
            await self._observe_write(candidate.key, marker_revision)

    async def _observe_write(self, key: str, revision: int) -> None:
        await self._bucket.wait_for_revision(key, revision)
        if self._started:
            await self._view.wait_for_revision(revision)

    async def _forget_lease(self, lease: BeaconAdvertisementLease) -> None:
        async with self._lock:
            self._leases.discard(lease)


class BeaconDirectory(Generic[T]):
    """Generic local parsed-record view over one Beacon feature."""

    def __init__(
        self,
        beacon: Beacon,
        feature_id: str,
        parser: BeaconDirectoryParser[T],
        *,
        log_label: str = "BeaconDirectory",
    ) -> None:
        self._beacon = beacon
        self.feature_id = _require_text(feature_id, field_name="Beacon feature id")
        self._parser = parser
        self._log_label = _require_text(log_label, field_name="Beacon directory log label")
        self._ready = anyio.Event()
        self._changed = anyio.Event()
        self._closed = False
        self._started = False
        self._current = False
        self._cancel_scope: anyio.CancelScope | None = None
        self._records_by_key: dict[str, tuple[T, ...]] = {}
        self._version = 0
        self._lock = RLock()

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object] | None) -> None:
        if start_soon is None or self._closed or self._started:
            return
        self._started = True
        start_soon(self._event_loop)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def watch_records(self) -> AsyncIterator[tuple[T, ...]]:
        """Yield current parsed records and subsequent current snapshots."""

        last_version = -1
        while True:
            with self._lock:
                if self._closed:
                    return
                current = (
                    self._ready.is_set()
                    and self._current
                    and self._beacon.is_current()
                )
                version = self._version
                if current and version != last_version:
                    records = self._records_locked()
                    last_version = version
                    changed: anyio.Event | None = None
                else:
                    records = None
                    changed = self._changed

            if records is not None:
                yield records
                continue

            assert changed is not None
            await changed.wait()

    def is_current(self) -> bool:
        with self._lock:
            return self._ready.is_set() and self._current and self._beacon.is_current()

    async def wait_current(self, *, timeout: float | None = None) -> None:
        async def wait_loop() -> None:
            await self.wait_ready()
            while True:
                with self._lock:
                    if self._closed:
                        raise KvUnavailable(
                            f"Beacon directory for feature {self.feature_id!r} is closed"
                        )
                    current = (
                        self._ready.is_set()
                        and self._current
                        and self._beacon.is_current()
                    )
                    changed = self._changed
                if current:
                    return
                if self._beacon.is_current():
                    await changed.wait()
                else:
                    await self._beacon.wait_current()

        if timeout is None:
            await wait_loop()
            return
        with anyio.fail_after(timeout):
            await wait_loop()

    def records(self) -> tuple[T, ...]:
        with self._lock:
            self._raise_if_stale_locked()
            return self._records_locked()

    def resolve(
        self,
        predicate: BeaconDirectoryPredicate[T] | None = None,
        *,
        select: BeaconDirectorySelector[T] | None = None,
    ) -> T | None:
        with self._lock:
            self._raise_if_stale_locked()
            return self._resolve_locked(predicate, select=select)

    async def wait_for(
        self,
        predicate: BeaconDirectoryPredicate[T] | None = None,
        *,
        select: BeaconDirectorySelector[T] | None = None,
        timeout: float | None = None,
    ) -> T:
        async def wait_loop() -> T:
            await self.wait_ready()
            while True:
                with self._lock:
                    current = (
                        self._ready.is_set()
                        and self._current
                        and self._beacon.is_current()
                    )
                    selected = (
                        self._resolve_locked(predicate, select=select)
                        if current
                        else None
                    )
                    changed = self._changed
                if selected is not None:
                    return selected
                if self._beacon.is_current():
                    await changed.wait()
                else:
                    await self._beacon.wait_current()

        if timeout is None:
            return await wait_loop()
        with anyio.fail_after(timeout):
            return await wait_loop()

    async def aclose(self) -> None:
        self._closed = True
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        self._notify_changed()

    async def _event_loop(self) -> None:
        with anyio.CancelScope() as cancel_scope:
            self._cancel_scope = cancel_scope
            try:
                async with self._beacon.watch(self.feature_id) as snapshots:
                    async for snapshot in snapshots:
                        self._apply_snapshot(
                            snapshot.candidates,
                            current=snapshot.current,
                        )
            except anyio.get_cancelled_exc_class():
                raise
            except KvUnavailable:
                pass
            except Exception:
                logger.warning(
                    "%s Beacon directory watch failed feature=%s",
                    self._log_label,
                    self.feature_id,
                    exc_info=True,
                )
            finally:
                self._mark_stale()

    def _apply_snapshot(
        self,
        candidates: tuple[Candidate, ...],
        *,
        current: bool,
    ) -> None:
        # Parser code is intentionally outside the directory state lock so one
        # slow or faulty feature parser cannot block readers or view ingestion.
        records_by_key = {
            candidate.key: records
            for candidate in candidates
            if (records := self._parse_candidate(candidate))
        }
        with self._lock:
            self._records_by_key = records_by_key
            self._current = current
            if current:
                self._ready.set()
            self._notify_changed_locked()

    def _parse_candidate(self, candidate: Candidate | None) -> tuple[T, ...]:
        if candidate is None:
            return ()
        try:
            parsed = self._parser(candidate)
        except Exception:
            logger.warning(
                "%s Beacon directory parser rejected feature=%s key=%s",
                self._log_label,
                self.feature_id,
                candidate.key,
                exc_info=True,
            )
            return ()
        if parsed is None:
            return ()
        if isinstance(parsed, list | tuple):
            return tuple(parsed)
        return (parsed,)

    def _mark_stale(self) -> None:
        with self._lock:
            self._current = False
            self._notify_changed_locked()

    def _raise_if_stale_locked(self) -> None:
        if not (self._ready.is_set() and self._current and self._beacon.is_current()):
            raise KvUnavailable(
                f"Beacon directory for feature {self.feature_id!r} is not current"
            )

    def _records_locked(self) -> tuple[T, ...]:
        return tuple(
            record
            for key in sorted(self._records_by_key)
            for record in self._records_by_key[key]
        )

    def _resolve_locked(
        self,
        predicate: BeaconDirectoryPredicate[T] | None,
        *,
        select: BeaconDirectorySelector[T] | None,
    ) -> T | None:
        records = self._records_locked()
        if predicate is not None:
            records = tuple(record for record in records if predicate(record))
        if select is not None:
            return select(records)
        return records[0] if records else None

    def _notify_changed(self) -> None:
        with self._lock:
            self._notify_changed_locked()

    def _notify_changed_locked(self) -> None:
        self._version += 1
        changed = self._changed
        self._changed = anyio.Event()
        changed.set()


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
        self._requested_refresh_interval = spec.refresh_interval
        self._refresh_interval: float | None = None
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
            await anyio.sleep(await self._next_heartbeat_delay())
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
            self._handle = await self._create_advertisement_from_current_state()
            self._last_refresh_at = monotonic()
            self._refresh_interval = await self._next_refresh_interval()
            return self._handle
        if force_refresh and not self._refresh_due():
            return self._handle
        old_revision = self._handle.revision
        old_handle = self._handle
        try:
            refreshed = await self._beacon._refresh_advertisement(
                old_handle,
                protocol=self._protocol,
                operations=self._operations,
                labels=self._labels,
                hints=self._hints,
                payload=self._payload,
                force_refresh=force_refresh,
            )
        except _BeaconAdvertisementMissing:
            logger.warning(
                "%s Beacon advertisement missing; recreating feature=%s "
                "endpoint=%s session=%s advertisement=%s key=%s",
                self._log_label,
                self.feature_id,
                self.endpoint,
                self.session_id,
                self._advertisement_id,
                old_handle.key,
                exc_info=True,
            )
            self._handle = await self._create_advertisement_from_current_state()
            self._last_refresh_at = monotonic()
            self._refresh_interval = await self._next_refresh_interval()
            return self._handle
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
            self._refresh_interval = await self._next_refresh_interval()
        return refreshed

    def _refresh_due(self) -> bool:
        return (
            self._last_refresh_at is None
            or self._refresh_interval is None
            or monotonic() - self._last_refresh_at >= self._refresh_interval
        )

    async def _next_heartbeat_delay(self) -> float:
        if self._refresh_interval is None:
            self._refresh_interval = await self._next_refresh_interval()
        if self._last_refresh_at is None:
            return self._refresh_interval
        elapsed = monotonic() - self._last_refresh_at
        return max(0.0, self._refresh_interval - elapsed)

    async def _create_advertisement_from_current_state(self) -> AdvertisementHandle:
        return await self._beacon._create_advertisement(
            BeaconAdvertisementSpec(
                feature_id=self.feature_id,
                endpoint=self.endpoint,
                session_id=self.session_id,
                advertiser=self.advertiser,
                protocol=self._protocol,
                operations=self._operations,
                labels=self._labels,
                hints=self._hints,
                payload=self._payload,
                refresh_interval=self._requested_refresh_interval,
                log_label=self._log_label,
            ),
            advertisement_id=self._advertisement_id,
        )

    async def _next_refresh_interval(self) -> float:
        return _beacon_refresh_interval(
            requested=self._requested_refresh_interval,
            ttl_seconds=await self._beacon._ttl_seconds(),  # noqa: SLF001
        )


def _is_materialized_bucket(value: Any) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            "start",
            "is_current",
            "wait_current",
            "exact_bucket",
            "get_cached",
            "items_cached",
            "revision_cached",
            "snapshot",
            "subscribe",
            "wait_for_revision",
        )
    )


async def _exact_get(bucket: Any, key: str) -> KvEntry | None:
    get_exact = getattr(bucket, "get_exact", None)
    return await (get_exact if get_exact is not None else bucket.get)(key)


def _filter_candidates(
    candidates: tuple[Candidate, ...],
    selector: AdvertisementFilter | None,
) -> tuple[Candidate, ...]:
    if selector is None:
        return candidates
    return tuple(
        candidate
        for candidate in candidates
        if _selector_accepts(selector, candidate.advertisement)
    )


def _record_from_spec(
    spec: BeaconAdvertisementSpec,
    *,
    advertisement_id: str,
    ttl_seconds: int,
) -> AdvertisementRecord:
    now = _now_utc()
    return AdvertisementRecord(
        advertisementId=advertisement_id,
        featureId=spec.feature_id,
        advertiser=spec.advertiser,
        endpoint=spec.endpoint,
        sessionId=spec.session_id,
        refreshSeq=1,
        ttlSeconds=ttl_seconds,
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
    ttl_seconds: int,
    force_refresh: bool,
) -> AdvertisementRecord:
    update = {
        "protocol": protocol,
        "operations": tuple(operations),
        "labels": freeze_json(labels),
        "hints": freeze_json(hints),
        "payload": freeze_json(payload) if payload is not None else None,
        "ttl_seconds": ttl_seconds,
    }
    changed = (
        record.protocol != update["protocol"]
        or record.operations != update["operations"]
        or record.labels != update["labels"]
        or record.hints != update["hints"]
        or record.payload != update["payload"]
        or record.ttl_seconds != ttl_seconds
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


__all__ = [
    "BEACON_ADVERTISEMENT_SCHEMA_ID",
    "BEACON_ADVERTISEMENT_STORE_POLICY",
    "DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME",
    "DEFAULT_BEACON_TTL_SECONDS",
    "AdvertisementHandle",
    "AdvertisementRecord",
    "Beacon",
    "BeaconAdvertisementLease",
    "BeaconAdvertisementSpec",
    "BeaconDirectory",
    "BeaconProtocol",
    "BeaconWatchChange",
    "BeaconWatchSnapshot",
    "Candidate",
    "CandidateStatus",
    "beacon_advertisement_key",
    "beacon_feature_prefix",
    "parse_beacon_advertisement_key",
]
