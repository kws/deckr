from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import anyio

from deckr.contracts.models import DeckrModel, freeze_json, thaw_json
from deckr.core.util.anyio import (
    CoalescedStateBroadcaster,
    CoalescedStateSubscription,
)

logger = logging.getLogger(__name__)

KV_OPERATION_HEADER = "KV-Operation"
KV_DELETE_OPERATION = "DEL"
KV_PURGE_OPERATION = "PURGE"
NATS_MARKER_REASON_HEADER = "Nats-Marker-Reason"
NATS_MARKER_MAX_AGE = "MaxAge"
NATS_NANOSECONDS_PER_SECOND = 1_000_000_000
NATS_SUBJECT_DELETE_MARKER_TTL_FIELD = "subject_delete_marker_ttl"
NATS_WATCH_CLEANUP_TIMEOUT_SECONDS = 0.5
NATS_WATCH_HEALTH_INTERVAL_SECONDS = 0.1
NATS_WATCH_INACTIVE_THRESHOLD_SECONDS = 1.0


class KvConflict(RuntimeError):
    """Raised when a KV create or revision-checked write fails."""


class KvUnavailable(RuntimeError):
    """Raised when a NATS KV bucket cannot answer safely."""


class KvViewStatus(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STALE = "stale"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class KvBucketPolicy:
    bucket: str
    ttl_seconds: float | None
    allow_write_ttl: bool = False
    description: str = "NATS KV bucket"

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("bucket must not be empty")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if self.allow_write_ttl and self.ttl_seconds is None:
            raise ValueError("allow_write_ttl requires ttl_seconds")


@dataclass(frozen=True, slots=True)
class KvEntry:
    bucket: str
    key: str
    value: Mapping[str, Any]
    revision: int


@dataclass(frozen=True, slots=True)
class KvChange:
    bucket: str
    key: str
    revision: int
    operation: Literal["put", "delete", "expire"]
    entry: KvEntry | None = None
    marker_reason: str | None = None


@dataclass(frozen=True, slots=True)
class KvWatchBarrier:
    """Broker stream high-water captured before a raw KV watch was opened."""

    revision: int


@dataclass(frozen=True, slots=True)
class KvMaterializedSnapshot:
    version: int
    current: bool
    entries: tuple[KvEntry, ...]


@dataclass(frozen=True, slots=True)
class KvMaterializedChange:
    version: int
    current: bool
    changed_keys: frozenset[str]
    resnapshot_required: bool


class _NatsKvWatchReceiveStream(
    anyio.abc.ObjectReceiveStream[KvChange | KvWatchBarrier]
):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        watcher: Any,
        barrier: KvWatchBarrier,
        connection: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._watcher = watcher
        self._barrier = barrier
        self._connection = connection
        self._iterator = watcher.__aiter__()
        self._closed = False

    async def _next_entry(self) -> Any:
        if self._connection is None:
            return await self._iterator.__anext__()
        while True:
            if not bool(getattr(self._connection, "is_connected", True)):
                raise KvUnavailable("NATS connection is not current")
            with anyio.move_on_after(NATS_WATCH_HEALTH_INTERVAL_SECONDS) as scope:
                entry = await self._iterator.__anext__()
            if not scope.cancel_called:
                return entry

    async def receive(self) -> KvChange | KvWatchBarrier:
        if self._closed:
            raise anyio.ClosedResourceError
        try:
            while True:
                try:
                    entry = await self._next_entry()
                except StopAsyncIteration:
                    await self.aclose()
                    raise anyio.EndOfStream from None
                if entry is None:
                    return self._barrier
                change = kv_change_from_raw(self._bucket, entry)
                if change is None:
                    continue
                if not change.key.startswith(self._prefix):
                    continue
                return change
        except anyio.ClosedResourceError:
            raise
        except anyio.EndOfStream:
            raise
        except Exception as exc:
            logger.warning(
                "NATS KV watch failed bucket=%s prefix=%s",
                self._bucket,
                self._prefix,
                exc_info=True,
            )
            try:
                await self.aclose()
            finally:
                raise KvUnavailable(
                    f"Could not watch KV prefix {self._prefix!r}"
                ) from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        connection_current = self._connection is None or bool(
            getattr(self._connection, "is_connected", True)
        )
        try:
            with anyio.move_on_after(
                NATS_WATCH_CLEANUP_TIMEOUT_SECONDS,
                shield=True,
            ):
                await self._watcher.stop()
        finally:
            if connection_current:
                with anyio.move_on_after(
                    NATS_WATCH_CLEANUP_TIMEOUT_SECONDS,
                    shield=True,
                ):
                    await delete_ephemeral_consumer(
                        getattr(self._watcher, "_sub", None),
                        reason=f"KV watch prefix {self._prefix!r}",
                    )


class NatsJsonKvBucket:
    """Thin JSON adapter for one NATS KV bucket."""

    def __init__(
        self,
        *,
        js: Any,
        policy: KvBucketPolicy,
        buffer_size: int = 100,
    ) -> None:
        self._js = js
        self.policy = policy
        self._buffer_size = buffer_size
        self._kv = None
        self._ensure_lock = anyio.Lock()
        self._resolved_ttl_seconds: float | None = None

    @property
    def bucket(self) -> str:
        return self.policy.bucket

    async def ttl_seconds(self) -> float | None:
        await self._available_kv()
        return self._resolved_ttl_seconds

    async def get(self, key: str) -> KvEntry | None:
        kv = await self._available_kv()
        try:
            entry = await kv.get(key)
        except Exception as exc:
            if is_key_missing(exc):
                return None
            raise KvUnavailable(f"Could not get KV key {key!r}") from exc
        if kv_entry_is_absent_marker(entry):
            return None
        return kv_entry_from_raw(self.bucket, entry)

    async def items(self, prefix: str = "") -> tuple[KvEntry, ...]:
        kv = await self._available_kv()
        try:
            keys = await kv_keys(kv, prefix)
        except Exception as exc:
            if is_key_missing(exc):
                return ()
            raise KvUnavailable(f"Could not list KV prefix {prefix!r}") from exc
        entries: list[KvEntry] = []
        for key in sorted(str(item) for item in keys if str(item).startswith(prefix)):
            entry = await self.get(key)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = kv_value(value)
        try:
            revision = await kv.put(key, kv_payload(normalized))
        except Exception as exc:
            raise KvUnavailable(f"Could not put KV key {key!r}") from exc
        return KvEntry(self.bucket, key, normalized, int(revision))

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = kv_value(value)
        payload = kv_payload(normalized)
        try:
            revision = await kv.create(key, payload)
        except Exception as exc:
            if is_revision_conflict(exc):
                revision = await self._create_over_absent_marker(
                    kv,
                    key,
                    payload,
                    conflict=exc,
                )
            else:
                raise KvUnavailable(f"Could not create KV key {key!r}") from exc
        return KvEntry(self.bucket, key, normalized, int(revision))

    async def _create_over_absent_marker(
        self,
        kv,
        key: str,
        payload: bytes,
        *,
        conflict: BaseException,
    ) -> int:
        try:
            marker_revision = await kv_absent_marker_revision(kv, key)
        except Exception as exc:
            if is_key_missing(exc):
                raise KvConflict(f"KV key {key!r} already exists") from conflict
            raise KvUnavailable(f"Could not create KV key {key!r}") from exc
        if marker_revision is None:
            raise KvConflict(f"KV key {key!r} already exists") from conflict
        try:
            return int(await kv.update(key, payload, last=marker_revision))
        except Exception as exc:
            if is_revision_conflict(exc) or is_key_missing(exc):
                raise KvConflict(f"KV key {key!r} already exists") from exc
            raise KvUnavailable(f"Could not create KV key {key!r}") from exc

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = kv_value(value)
        try:
            new_revision = await kv.update(
                key,
                kv_payload(normalized),
                last=revision,
            )
        except Exception as exc:
            if is_revision_conflict(exc) or is_key_missing(exc):
                raise KvConflict(f"KV key {key!r} revision changed") from exc
            raise KvUnavailable(f"Could not update KV key {key!r}") from exc
        return KvEntry(self.bucket, key, normalized, int(new_revision))

    async def delete(self, key: str, *, revision: int | None = None) -> int | None:
        kv = await self._available_kv()
        try:
            if revision is None:
                await kv.delete(key)
            else:
                await kv.delete(key, last=revision)
        except Exception as exc:
            if is_key_missing(exc):
                return None
            if is_revision_conflict(exc):
                raise KvConflict(f"KV key {key!r} revision changed") from exc
            raise KvUnavailable(f"Could not delete KV key {key!r}") from exc
        try:
            return await kv_absent_marker_revision(kv, key)
        except Exception as exc:
            if is_key_missing(exc):
                return None
            logger.debug(
                "Could not confirm KV delete marker bucket=%s key=%s",
                self.bucket,
                key,
                exc_info=True,
            )
            return None

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[
        anyio.abc.ObjectReceiveStream[KvChange | KvWatchBarrier]
    ]:
        kv = await self._available_kv()
        try:
            high_water = await self._stream_high_water_revision(kv)
            watcher = await kv.watch(
                kv_watch_pattern(prefix),
                inactive_threshold=NATS_WATCH_INACTIVE_THRESHOLD_SECONDS,
            )
        except Exception as exc:
            raise KvUnavailable(f"Could not watch KV prefix {prefix!r}") from exc
        stream = _NatsKvWatchReceiveStream(
            bucket=self.bucket,
            prefix=prefix,
            watcher=watcher,
            barrier=KvWatchBarrier(high_water),
            connection=getattr(self._js, "_nc", None),
        )
        try:
            yield stream
        finally:
            await stream.aclose()

    async def _stream_high_water_revision(self, kv: Any) -> int:
        stream_name = getattr(kv, "_stream", f"KV_{self.bucket}")
        info = await self._js.stream_info(stream_name)
        state = getattr(info, "state", None)
        value = getattr(state, "last_seq", None)
        if value is None:
            # Deterministic test stores expose their revision directly. Real
            # JetStream stream info always supplies ``state.last_seq``.
            value = getattr(kv, "_revision", 0)
        return int(value)

    async def _ensure_kv(self):
        if self._kv is not None:
            return self._kv
        async with self._ensure_lock:
            if self._kv is not None:
                return self._kv
            return await self._ensure_kv_locked()

    async def _ensure_kv_locked(self):
        newly_created = False
        try:
            kv = await self._js.key_value(self.bucket)
        except Exception:
            try:
                kv = await self._create_kv()
                newly_created = True
            except TypeError:
                try:
                    kv = await self._create_kv_with_params()
                    newly_created = True
                except Exception:
                    # Another process may have created the bucket between our
                    # lookup and create attempt. The winner owns its policy;
                    # treat the bucket as existing and validate it read-only.
                    kv = await self._js.key_value(self.bucket)
            except Exception:
                # Another process may have created the bucket between our
                # lookup and create attempt. The winner owns its policy;
                # treat the bucket as existing and validate it read-only.
                kv = await self._js.key_value(self.bucket)
        await self._ensure_kv_stream_config(kv, newly_created=newly_created)
        # Do not cache a handle until its policy has been validated. Otherwise
        # a failed first call would bypass validation on the next operation.
        self._kv = kv
        return kv

    async def _available_kv(self):
        try:
            return await self._ensure_kv()
        except KvUnavailable:
            raise
        except Exception as exc:
            raise KvUnavailable(f"NATS KV bucket {self.bucket!r} is unavailable") from exc

    async def _create_kv(self):
        from nats.js.api import DiscardPolicy, KeyValueConfig, StreamConfig

        if not self.policy.allow_write_ttl:
            # nats.py 2.14 creates every KeyValueConfig stream with
            # allow_msg_ttl=True. NATS does not allow that capability to be
            # disabled after creation, so persistent Deckr authority buckets
            # must be created with their final policy atomically. This mirrors
            # nats.py's KV stream shape except for the deliberate TTL flag.
            await self._js.add_stream(
                StreamConfig(
                    name=f"KV_{self.bucket}",
                    subjects=[f"$KV.{self.bucket}.>"],
                    allow_direct=None,
                    allow_rollup_hdrs=True,
                    allow_msg_ttl=False,
                    deny_delete=True,
                    discard=DiscardPolicy.NEW,
                    duplicate_window=120.0,
                    max_age=None,
                    max_bytes=None,
                    max_consumers=-1,
                    max_msg_size=None,
                    max_msgs=-1,
                    max_msgs_per_subject=1,
                    num_replicas=1,
                    storage=None,
                )
            )
            return await self._js.key_value(self.bucket)

        return await self._js.create_key_value(
            config=KeyValueConfig(
                bucket=self.bucket,
                history=1,
                ttl=self.policy.ttl_seconds,
            )
        )

    async def _create_kv_with_params(self):
        if not self.policy.allow_write_ttl:
            # The exact StreamConfig path above does not depend on the
            # KeyValueConfig call signature. Reaching this fallback means the
            # stream creation itself raised TypeError, which is not safe to
            # reinterpret as permission to create a non-canonical bucket.
            raise KvUnavailable(
                f"Could not create persistent NATS KV bucket {self.bucket!r} "
                "with its required policy"
            )
        return await self._js.create_key_value(
            bucket=self.bucket,
            history=1,
            ttl=self.policy.ttl_seconds,
        )

    async def _ensure_kv_stream_config(self, kv, *, newly_created: bool) -> None:
        stream_name = getattr(kv, "_stream", f"KV_{self.bucket}")
        try:
            info = await self._js.stream_info(stream_name)
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect NATS KV bucket {self.bucket!r}; delete and "
                "recreate the development bucket if it was created by an older "
                "Deckr build."
            ) from exc
        config = info.config
        expected_marker_ttl_ns = _duration_nanoseconds(self.policy.ttl_seconds)
        raw_config = None
        if expected_marker_ttl_ns is not None:
            try:
                raw_config = await _nats_stream_raw_config(self._js, stream_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not inspect raw NATS KV stream config for {self.bucket!r}; "
                    "Deckr requires subject delete markers on TTL-bound buckets."
                ) from exc

        max_age_matches = _duration_seconds_equal(
            getattr(config, "max_age", None),
            self.policy.ttl_seconds,
        )
        max_messages_matches = getattr(config, "max_msgs_per_subject", None) == 1
        observed_allow_write_ttl = (
            raw_config.get("allow_msg_ttl")
            if raw_config is not None
            else getattr(config, "allow_msg_ttl", None)
        )
        allow_write_ttl_matches = (
            observed_allow_write_ttl is True
        ) == self.policy.allow_write_ttl
        marker_ttl_matches = (
            expected_marker_ttl_ns is None
            or _raw_duration_nanoseconds(
                raw_config,
                NATS_SUBJECT_DELETE_MARKER_TTL_FIELD,
            )
            == expected_marker_ttl_ns
        )
        policy_matches = (
            max_age_matches
            and max_messages_matches
            and allow_write_ttl_matches
            and marker_ttl_matches
        )
        if policy_matches:
            self._resolved_ttl_seconds = _resolved_stream_ttl_seconds(
                config,
                raw_config=raw_config,
            )
            return

        if not newly_created:
            mismatches: list[str] = []
            if not max_age_matches:
                mismatches.append(
                    "max_age "
                    f"(expected {self.policy.ttl_seconds!r}s, "
                    f"found {getattr(config, 'max_age', None)!r})"
                )
            if not max_messages_matches:
                mismatches.append(
                    "max_msgs_per_subject "
                    f"(expected 1, "
                    f"found {getattr(config, 'max_msgs_per_subject', None)!r})"
                )
            if not allow_write_ttl_matches:
                mismatches.append(
                    "allow_msg_ttl "
                    f"(expected {self.policy.allow_write_ttl!r}, "
                    f"found {observed_allow_write_ttl!r})"
                )
            if not marker_ttl_matches:
                mismatches.append(
                    f"{NATS_SUBJECT_DELETE_MARKER_TTL_FIELD} "
                    f"(expected {expected_marker_ttl_ns!r}ns, "
                    "found "
                    f"{_raw_duration_nanoseconds(raw_config, NATS_SUBJECT_DELETE_MARKER_TTL_FIELD)!r})"
                )
            raise KvUnavailable(
                f"Existing NATS KV bucket {self.bucket!r} has an incompatible "
                f"Deckr KV policy: {', '.join(mismatches)}. Deckr will not "
                "rewrite shared bucket policy; delete and recreate the "
                f"development bucket/stream {stream_name} and restart."
            )

        # The KV creation API expresses max_age and history. If the broker did
        # not honor those fields, do not disguise the mismatch with a rewrite.
        # A proven-new bucket may only receive the fields that API cannot set.
        if not max_age_matches or not max_messages_matches:
            raise KvUnavailable(
                f"New NATS KV bucket {self.bucket!r} was created with an "
                "incompatible max_age or max_msgs_per_subject policy."
            )
        try:
            if raw_config is None:
                config.allow_msg_ttl = self.policy.allow_write_ttl
                await self._js.update_stream(config)
            else:
                updated_config = dict(raw_config)
                if expected_marker_ttl_ns is not None:
                    updated_config[NATS_SUBJECT_DELETE_MARKER_TTL_FIELD] = (
                        expected_marker_ttl_ns
                    )
                updated_config["allow_msg_ttl"] = self.policy.allow_write_ttl
                await _update_nats_stream_raw_config(
                    self._js,
                    stream_name,
                    updated_config,
                )
                raw_config = updated_config
        except Exception as exc:
            raise KvUnavailable(
                f"New NATS KV bucket {self.bucket!r} could not be configured for "
                "Deckr's current KV policy. "
                f"Delete the development bucket/stream {stream_name} and restart."
            ) from exc
        self._resolved_ttl_seconds = _resolved_stream_ttl_seconds(
            config,
            raw_config=raw_config,
        )

    def _validate_ttl(self, ttl: float | None) -> None:
        if not self.policy.allow_write_ttl:
            if ttl is None:
                return
            raise ValueError(
                f"NATS {self.policy.description} does not use write TTL; "
                f"per-key TTL {ttl!r} is not supported."
            )
        if ttl is None:
            return
        bucket_ttl = self.policy.ttl_seconds
        if bucket_ttl is not None and abs(float(ttl) - bucket_ttl) <= 0.001:
            return
        raise ValueError(
            "NATS KV uses the broker-owned bucket TTL "
            f"({bucket_ttl:g}s); per-key TTL {ttl!r} is not supported."
        )


MATERIALIZED_TOMBSTONE_LIMIT = 2_000


class _KvMaterializedWatch:
    def __init__(
        self,
        subscription: CoalescedStateSubscription[
            str,
            KvMaterializedSnapshot,
        ],
    ) -> None:
        self._subscription = subscription
        self._initial: KvMaterializedSnapshot | None = subscription.initial

    def __aiter__(self) -> _KvMaterializedWatch:
        return self

    async def __anext__(self) -> KvMaterializedSnapshot | KvMaterializedChange:
        try:
            return await self.receive()
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            raise StopAsyncIteration from None

    async def receive(self) -> KvMaterializedSnapshot | KvMaterializedChange:
        if self._initial is not None:
            initial = self._initial
            self._initial = None
            return initial
        change = await self._subscription.receive()
        return KvMaterializedChange(
            version=change.version,
            current=change.current,
            changed_keys=change.changed,
            resnapshot_required=change.resnapshot_required,
        )


class NatsKvMaterializedBucket:
    """Read-only, current-state materialization of one JSON KV bucket.

    The raw bucket remains the sole exact/CAS writer. This source installs each
    broker recovery snapshot atomically at its typed high-water barrier and
    emits only bounded coalesced wakeups afterward.
    """

    def __init__(
        self,
        *,
        js: Any | None = None,
        bucket: str | NatsJsonKvBucket | Any | None = None,
        policy: KvBucketPolicy | None = None,
        key_prefix: str = "",
    ) -> None:
        if isinstance(bucket, str):
            if js is None:
                raise ValueError("js is required when bucket is a name")
            resolved_policy = policy or KvBucketPolicy(
                bucket=bucket,
                ttl_seconds=None,
            )
            self._bucket = NatsJsonKvBucket(js=js, policy=resolved_policy)
        elif bucket is not None:
            self._bucket = bucket
        else:
            if js is None or policy is None:
                raise ValueError("bucket or js+policy is required")
            self._bucket = NatsJsonKvBucket(js=js, policy=policy)
        self.key_prefix = key_prefix
        self._ready = anyio.Event()
        self._current_event = anyio.Event()
        self._run_started = anyio.Event()
        self._closed_event = anyio.Event()
        self._revision_condition = anyio.Condition()
        self._status = KvViewStatus.STARTING
        self._started = False
        self._closed = False
        self._cancel_scope: anyio.CancelScope | None = None
        self._entries: dict[str, KvEntry] = {}
        self._tombstone_revision_by_key: dict[str, int] = {}
        self._high_water_revision = 0
        self._broadcaster = CoalescedStateBroadcaster[str](current=False)

    @property
    def bucket(self) -> str:
        return str(self._bucket.bucket)

    @property
    def exact_bucket(self) -> Any:
        """The separate exact/CAS store used by an owning runtime facade."""

        return self._bucket

    @property
    def status(self) -> KvViewStatus:
        return self._status

    @property
    def version(self) -> int:
        return self._broadcaster.version

    @property
    def high_water_revision(self) -> int:
        return self._high_water_revision

    @property
    def tombstone_count(self) -> int:
        return len(self._tombstone_revision_by_key)

    async def ttl_seconds(self) -> float | None:
        ttl_seconds = getattr(self._bucket, "ttl_seconds", None)
        if ttl_seconds is None:
            return None
        value = ttl_seconds()
        if hasattr(value, "__await__"):
            value = await value
        return value

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("materialized KV source is closed")
        self._started = True
        task_group.start_soon(self._run)

    async def _run(self) -> None:
        try:
            with anyio.CancelScope() as cancel_scope:
                self._cancel_scope = cancel_scope
                self._run_started.set()
                await self._watch_loop()
        finally:
            self._cancel_scope = None
            await self._set_status(KvViewStatus.CLOSED)
            await self._broadcaster.aclose()
            self._closed_event.set()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def is_current(self) -> bool:
        return self._status == KvViewStatus.READY

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def wait_current(self) -> None:
        while self._status != KvViewStatus.READY:
            if self._status == KvViewStatus.CLOSED:
                raise KvUnavailable(
                    f"NATS KV materialized view is closed bucket={self.bucket!r}"
                )
            event = self._current_event
            await event.wait()

    async def wait_for_revision(self, key: str, revision: int) -> None:
        """Wait until the broker watch has observed a committed exact revision."""

        if not self._started:
            return
        while not self._revision_was_observed(key, revision):
            if self._status == KvViewStatus.CLOSED:
                raise KvUnavailable(
                    f"NATS KV materialized view closed before observing {key!r} "
                    f"revision {revision}"
                )
            async with self._revision_condition:
                if self._revision_was_observed(key, revision):
                    return
                await self._revision_condition.wait()

    def _revision_was_observed(self, key: str, revision: int) -> bool:
        cached = self.revision_cached(key)
        return (cached is not None and cached >= revision) or (
            self._high_water_revision >= revision
        )

    def get_cached(self, key: str) -> KvEntry | None:
        return self._entries.get(key)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return tuple(
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(prefix)
        )

    def revision_cached(self, key: str) -> int | None:
        entry = self._entries.get(key)
        if entry is not None:
            return entry.revision
        return self._tombstone_revision_by_key.get(key)

    async def aclose(self) -> None:
        if self._closed:
            if self._started:
                await self._closed_event.wait()
            return
        self._closed = True
        if not self._started:
            await self._set_status(KvViewStatus.CLOSED)
            await self._broadcaster.aclose()
            self._closed_event.set()
            return
        await self._run_started.wait()
        cancel_scope = self._cancel_scope
        if cancel_scope is not None:
            cancel_scope.cancel()
        await self._closed_event.wait()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[_KvMaterializedWatch]:
        async with self._broadcaster.subscribe(self._snapshot_locked) as subscription:
            yield _KvMaterializedWatch(subscription)

    def _snapshot_locked(self, version: int, current: bool) -> KvMaterializedSnapshot:
        return KvMaterializedSnapshot(
            version=version,
            current=current,
            entries=tuple(entry for _, entry in sorted(self._entries.items())),
        )

    async def snapshot(self) -> KvMaterializedSnapshot:
        return await self._broadcaster.capture(self._snapshot_locked)

    async def _watch_loop(self) -> None:
        retry_seconds = 1.0
        while True:
            if self._ready.is_set():
                await self._set_status(KvViewStatus.STALE)
            try:
                await self._consume_one_watch()
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                logger.warning(
                    "NATS KV materialized watch failed bucket=%s prefix=%s",
                    self.bucket,
                    self.key_prefix,
                    exc_info=True,
                )
                await anyio.sleep(retry_seconds)

    async def _consume_one_watch(self) -> bool:
        recovered: dict[str, KvChange] = {}
        post_barrier: dict[str, KvChange] = {}
        barrier_seen = False
        async with self._bucket.watch(self.key_prefix) as changes:
            async for item in changes:
                if isinstance(item, KvWatchBarrier):
                    if barrier_seen:
                        continue
                    barrier_seen = True
                    compact = await self._install_recovered_snapshot(
                        recovered,
                        post_barrier,
                        barrier=item,
                    )
                    if compact:
                        return True
                    continue
                change = item
                if self.key_prefix and not change.key.startswith(self.key_prefix):
                    continue
                if not barrier_seen:
                    # The raw watch's typed high-water determines which changes
                    # form the recovered snapshot. A change newer than that
                    # barrier is classified when the barrier item arrives.
                    recovered[change.key] = _newer_change(recovered.get(change.key), change)
                    continue
                if await self._apply_change(change):
                    return True
        return False

    async def _install_recovered_snapshot(
        self,
        observed: Mapping[str, KvChange],
        post_barrier: Mapping[str, KvChange],
        *,
        barrier: KvWatchBarrier,
    ) -> bool:
        # Raw NATS watches can include a change newer than the captured stream
        # high-water in their initial replay. Split those changes before the
        # atomic install, then apply them in stream-revision order.
        snapshot_changes = {
            key: change
            for key, change in observed.items()
            if change.revision <= barrier.revision
        }
        newer = {
            key: change
            for key, change in observed.items()
            if change.revision > barrier.revision
        }
        for key, change in post_barrier.items():
            newer[key] = _newer_change(newer.get(key), change)

        entries: dict[str, KvEntry] = {}
        for change in snapshot_changes.values():
            if change.operation == "put" and change.entry is not None:
                entries[change.key] = change.entry

        tombstones: dict[str, int] = {}
        for change in sorted(newer.values(), key=lambda item: item.revision):
            current_entry = entries.get(change.key)
            current_revision = current_entry.revision if current_entry is not None else 0
            current_revision = max(current_revision, tombstones.get(change.key, 0))
            if change.revision <= current_revision:
                continue
            if change.operation == "put" and change.entry is not None:
                entries[change.key] = change.entry
                tombstones.pop(change.key, None)
            else:
                entries.pop(change.key, None)
                tombstones[change.key] = change.revision

        for key in self._entries.keys() - entries.keys():
            tombstones.setdefault(key, barrier.revision)

        compact = len(tombstones) >= MATERIALIZED_TOMBSTONE_LIMIT
        if len(tombstones) > MATERIALIZED_TOMBSTONE_LIMIT:
            newest_tombstones = sorted(
                tombstones.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:MATERIALIZED_TOMBSTONE_LIMIT]
            tombstones = dict(newest_tombstones)

        installed_high_water = max(
            (barrier.revision, *(change.revision for change in newer.values()))
        )

        async with self._broadcaster.lock:
            changed = _changed_entry_keys(self._entries, entries)
            self._entries = entries
            self._tombstone_revision_by_key = tombstones
            self._high_water_revision = installed_high_water
            self._status = KvViewStatus.READY
            self._current_event.set()
            self._ready.set()
            self._broadcaster.publish_locked(
                changed,
                current=True,
                resnapshot_required=len(changed) > 256,
            )
        await self._notify_revision_waiters()
        return compact

    async def _apply_change(self, change: KvChange) -> bool:
        """Apply one ordered live broker observation.

        Returns true when the bounded tombstone metadata reached its compaction
        threshold and the watch should be reopened immediately.
        """

        if self.key_prefix and not change.key.startswith(self.key_prefix):
            return False
        async with self._broadcaster.lock:
            current_revision = self.revision_cached(change.key) or 0
            if change.revision <= current_revision:
                return False
            self._high_water_revision = max(self._high_water_revision, change.revision)
            if change.operation == "put" and change.entry is not None:
                self._entries[change.key] = change.entry
                self._tombstone_revision_by_key.pop(change.key, None)
            else:
                self._entries.pop(change.key, None)
                self._tombstone_revision_by_key[change.key] = change.revision
            self._broadcaster.publish_locked((change.key,), current=self.is_current())
            compact = (
                len(self._tombstone_revision_by_key) >= MATERIALIZED_TOMBSTONE_LIMIT
            )
        await self._notify_revision_waiters()
        return compact

    async def _set_status(self, status: KvViewStatus) -> None:
        async with self._broadcaster.lock:
            if self._status == KvViewStatus.CLOSED and status != KvViewStatus.CLOSED:
                return
            if self._status == status:
                return
            self._status = status
            current = status == KvViewStatus.READY
            if current:
                self._current_event.set()
            else:
                self._current_event = anyio.Event()
            if status == KvViewStatus.READY:
                self._ready.set()
            if status != KvViewStatus.CLOSED:
                self._broadcaster.publish_locked((), current=current)
        await self._notify_revision_waiters()

    async def _notify_revision_waiters(self) -> None:
        async with self._revision_condition:
            self._revision_condition.notify_all()


def _newer_change(previous: KvChange | None, change: KvChange) -> KvChange:
    if previous is None or change.revision > previous.revision:
        return change
    return previous


def _changed_entry_keys(
    previous: Mapping[str, KvEntry],
    current: Mapping[str, KvEntry],
) -> frozenset[str]:
    return frozenset(
        key
        for key in previous.keys() | current.keys()
        if previous.get(key) != current.get(key)
    )


def kv_value(value: Mapping[str, Any] | DeckrModel) -> Mapping[str, Any]:
    if isinstance(value, DeckrModel):
        return freeze_json(
            value.model_dump(by_alias=True, exclude_none=True, mode="json")
        )
    return freeze_json(dict(value))


def kv_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), separators=(",", ":")).encode("utf-8")


def _duration_nanoseconds(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return int(float(seconds) * NATS_NANOSECONDS_PER_SECOND)


def _duration_seconds_equal(actual: float | None, expected: float | None) -> bool:
    if expected is None:
        return actual is None or actual == 0
    if actual is None:
        return False
    return abs(float(actual) - float(expected)) <= 0.001


def _raw_duration_nanoseconds(
    config: Mapping[str, Any] | None,
    field: str,
) -> int | None:
    if config is None:
        return None
    value = config.get(field)
    if value is None:
        return None
    return int(value)


def _resolved_stream_ttl_seconds(
    config: Any,
    *,
    raw_config: Mapping[str, Any] | None,
) -> float | None:
    raw_max_age = _raw_duration_nanoseconds(raw_config, "max_age")
    if raw_max_age is not None:
        if raw_max_age == 0:
            return None
        return raw_max_age / NATS_NANOSECONDS_PER_SECOND
    max_age = getattr(config, "max_age", None)
    if max_age is None or max_age == 0:
        return None
    return float(max_age)


async def _nats_stream_raw_config(js: Any, stream_name: str) -> Mapping[str, Any]:
    api_request = getattr(js, "_api_request", None)
    if api_request is None:
        raise RuntimeError("NATS JetStream context does not expose _api_request")
    prefix = getattr(js, "_prefix", "$JS.API")
    timeout = getattr(js, "_timeout", 5)
    response = await api_request(
        f"{prefix}.STREAM.INFO.{stream_name}",
        b"",
        timeout=timeout,
    )
    config = response.get("config") if isinstance(response, Mapping) else None
    if not isinstance(config, Mapping):
        raise RuntimeError(f"NATS stream {stream_name!r} response did not include config")
    return config


async def _update_nats_stream_raw_config(
    js: Any,
    stream_name: str,
    config: Mapping[str, Any],
) -> None:
    api_request = getattr(js, "_api_request", None)
    if api_request is None:
        raise RuntimeError("NATS JetStream context does not expose _api_request")
    prefix = getattr(js, "_prefix", "$JS.API")
    timeout = getattr(js, "_timeout", 5)
    await api_request(
        f"{prefix}.STREAM.UPDATE.{stream_name}",
        json.dumps(config).encode("utf-8"),
        timeout=timeout,
    )


def kv_entry_from_raw(bucket: str, entry) -> KvEntry:
    value = json.loads(entry.value.decode("utf-8")) if entry.value else {}
    return KvEntry(
        bucket=bucket,
        key=str(entry.key),
        value=kv_value(value),
        revision=int(entry.revision),
    )


def kv_change_from_raw(bucket: str, entry) -> KvChange | None:
    key = str(entry.key)
    revision = int(getattr(entry, "revision", 0))
    operation = str(getattr(entry, "operation", "") or "").upper()
    marker_reason = _raw_header(entry, NATS_MARKER_REASON_HEADER)
    if operation in {KV_DELETE_OPERATION, KV_PURGE_OPERATION}:
        return KvChange(
            bucket=bucket,
            key=key,
            revision=revision,
            operation=(
                "expire" if marker_reason == NATS_MARKER_MAX_AGE else "delete"
            ),
            marker_reason=marker_reason or operation,
        )
    if kv_entry_is_absent_marker(entry):
        return KvChange(
            bucket=bucket,
            key=key,
            revision=revision,
            operation=(
                "expire" if marker_reason == NATS_MARKER_MAX_AGE else "delete"
            ),
            marker_reason=marker_reason or operation or "absent",
        )
    return KvChange(
        bucket=bucket,
        key=key,
        revision=revision,
        operation="put",
        entry=kv_entry_from_raw(bucket, entry),
    )


def kv_entry_is_absent_marker(entry) -> bool:
    if getattr(entry, "operation", None) in {
        KV_DELETE_OPERATION,
        KV_PURGE_OPERATION,
    }:
        return True
    return getattr(entry, "value", None) in {None, b""}


async def kv_absent_marker_revision(kv, key: str) -> int | None:
    get_raw = getattr(kv, "_get", None)
    if get_raw is None:
        get_raw = kv.get
    try:
        entry = await get_raw(key)
    except Exception as exc:
        entry = getattr(exc, "entry", None)
        if entry is not None and kv_entry_is_absent_marker(entry):
            return int(entry.revision)
        raise
    if kv_entry_is_absent_marker(entry):
        return int(entry.revision)
    return None


def kv_watch_pattern(prefix: str) -> str:
    if not prefix:
        return ">"
    if prefix.endswith("."):
        return f"{prefix}>"
    return prefix


async def kv_keys(kv, prefix: str) -> tuple[str, ...]:
    if not prefix:
        return await kv_keys_unfiltered(kv, prefix)

    try:
        return await kv_keys_by_watch(kv, prefix)
    except (AttributeError, TypeError):
        return await kv_keys_unfiltered(kv, prefix)
    except Exception as exc:
        if is_key_missing(exc):
            return ()
        raise


async def kv_keys_by_watch(kv, prefix: str) -> tuple[str, ...]:
    watcher = await kv.watch(
        kv_watch_pattern(prefix),
        ignore_deletes=True,
        meta_only=True,
        inactive_threshold=5 * 60,
    )
    keys: list[str] = []
    try:
        async for entry in watcher:
            if entry is None:
                break
            key = str(entry.key)
            if key.startswith(prefix):
                keys.append(key)
    finally:
        try:
            await watcher.stop()
        finally:
            await delete_ephemeral_consumer(
                getattr(watcher, "_sub", None),
                reason=f"KV keys prefix {prefix!r}",
            )
    return tuple(keys)


async def kv_keys_unfiltered(kv, prefix: str) -> tuple[str, ...]:
    try:
        keys = await kv.keys()
    except Exception as exc:
        if is_key_missing(exc):
            return ()
        raise
    if keys is None:
        return ()
    return tuple(str(key) for key in keys if str(key).startswith(prefix))


def _raw_header(entry, key: str) -> str | None:
    headers = getattr(entry, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(key)
    except AttributeError:
        value = None
    if value is None and isinstance(headers, Mapping):
        value = headers.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value is not None else None


async def delete_ephemeral_consumer(subscription, *, reason: str) -> None:
    stream = getattr(subscription, "_stream", None)
    consumer = getattr(subscription, "_consumer", None)
    js = getattr(subscription, "_js", None)
    jsm = getattr(js, "_jsm", None)
    if not stream or not consumer or jsm is None:
        return
    try:
        await jsm.delete_consumer(stream, consumer)
    except Exception:
        logger.debug(
            "Could not delete NATS ephemeral consumer stream=%s consumer=%s after %s",
            stream,
            consumer,
            reason,
            exc_info=True,
        )


def exception_names(exc: BaseException) -> set[str]:
    names: set[str] = set()
    current: BaseException | None = exc
    while current is not None:
        names.add(type(current).__name__)
        current = current.__cause__
    return names


def is_key_missing(exc: BaseException) -> bool:
    names = exception_names(exc)
    if names & {"KeyNotFoundError", "KeyDeletedError", "NoKeysError", "NotFoundError"}:
        return True
    message = str(exc).lower()
    return message in {"missing", "not found", "key not found"}


def is_revision_conflict(exc: BaseException) -> bool:
    names = exception_names(exc)
    if "KeyWrongLastSequenceError" in names:
        return True
    if getattr(exc, "err_code", None) == 10071:
        return True
    message = str(exc).lower()
    return (
        message in {"exists", "revision changed"}
        or "wrong last" in message
        or "wrong sequence" in message
        or "revision changed" in message
    )


__all__ = [
    "KvBucketPolicy",
    "KvChange",
    "KvConflict",
    "KvEntry",
    "KvMaterializedChange",
    "KvMaterializedSnapshot",
    "KvUnavailable",
    "KvViewStatus",
    "KvWatchBarrier",
    "MATERIALIZED_TOMBSTONE_LIMIT",
    "NatsKvMaterializedBucket",
    "NatsJsonKvBucket",
]
