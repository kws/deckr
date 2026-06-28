from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal

import anyio

from deckr.contracts.models import DeckrModel, freeze_json, thaw_json

logger = logging.getLogger(__name__)

KV_OPERATION_HEADER = "KV-Operation"
KV_DELETE_OPERATION = "DEL"
KV_PURGE_OPERATION = "PURGE"
NATS_MARKER_REASON_HEADER = "Nats-Marker-Reason"
NATS_MARKER_MAX_AGE = "MaxAge"
NATS_NANOSECONDS_PER_SECOND = 1_000_000_000
NATS_SUBJECT_DELETE_MARKER_TTL_FIELD = "subject_delete_marker_ttl"


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
    view_generation: int | None = None


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
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        kv = await self._available_kv()
        send, receive = anyio.create_memory_object_stream[KvChange | None](
            max_buffer_size=self._buffer_size
        )
        watcher = None

        async def run() -> None:
            try:
                async for entry in watcher:
                    if entry is None:
                        await send.send(None)
                        continue
                    change = kv_change_from_raw(self.bucket, entry)
                    if change is None:
                        continue
                    if not change.key.startswith(prefix):
                        continue
                    await send.send(change)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                return
            except Exception as exc:
                logger.warning(
                    "NATS KV watch failed bucket=%s prefix=%s",
                    self.bucket,
                    prefix,
                    exc_info=True,
                )
                raise KvUnavailable(
                    f"Could not watch KV prefix {prefix!r}"
                ) from exc
            finally:
                await send.aclose()

        try:
            watcher = await kv.watch(kv_watch_pattern(prefix), inactive_threshold=5 * 60)
        except Exception as exc:
            raise KvUnavailable(f"Could not watch KV prefix {prefix!r}") from exc
        try:
            async with send, receive, anyio.create_task_group() as task_group:
                task_group.start_soon(run)
                yield receive
                task_group.cancel_scope.cancel()
        finally:
            try:
                await watcher.stop()
            finally:
                await delete_ephemeral_consumer(
                    getattr(watcher, "_sub", None),
                    reason=f"KV watch prefix {prefix!r}",
                )

    async def _ensure_kv(self):
        if self._kv is not None:
            return self._kv
        try:
            self._kv = await self._js.key_value(self.bucket)
        except Exception:
            try:
                self._kv = await self._create_kv()
            except TypeError:
                self._kv = await self._create_kv_with_params()
            except Exception:
                self._kv = await self._js.key_value(self.bucket)
        await self._ensure_kv_stream_config(self._kv)
        return self._kv

    async def _available_kv(self):
        try:
            return await self._ensure_kv()
        except Exception as exc:
            raise KvUnavailable(f"NATS KV bucket {self.bucket!r} is unavailable") from exc

    async def _create_kv(self):
        from nats.js.api import KeyValueConfig

        return await self._js.create_key_value(
            config=KeyValueConfig(
                bucket=self.bucket,
                history=1,
                ttl=self.policy.ttl_seconds,
            )
        )

    async def _create_kv_with_params(self):
        return await self._js.create_key_value(
            bucket=self.bucket,
            history=1,
            ttl=self.policy.ttl_seconds,
        )

    async def _ensure_kv_stream_config(self, kv) -> None:
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
        needs_update = (
            not _duration_seconds_equal(
                getattr(config, "max_age", None),
                self.policy.ttl_seconds,
            )
            or getattr(config, "max_msgs_per_subject", None) != 1
        )
        if self.policy.allow_write_ttl:
            needs_update = (
                needs_update or getattr(config, "allow_msg_ttl", None) is not True
            )
        if expected_marker_ttl_ns is not None:
            needs_update = (
                needs_update
                or _raw_duration_nanoseconds(
                    raw_config,
                    NATS_SUBJECT_DELETE_MARKER_TTL_FIELD,
                )
                != expected_marker_ttl_ns
            )
        if not needs_update:
            self._resolved_ttl_seconds = _resolved_stream_ttl_seconds(
                config,
                raw_config=raw_config,
            )
            return
        try:
            if raw_config is None:
                config.max_age = self.policy.ttl_seconds
                config.max_msgs_per_subject = 1
                if self.policy.allow_write_ttl:
                    config.allow_msg_ttl = True
                await self._js.update_stream(config)
            else:
                updated_config = dict(raw_config)
                updated_config["max_age"] = expected_marker_ttl_ns
                updated_config["max_msgs_per_subject"] = 1
                updated_config[NATS_SUBJECT_DELETE_MARKER_TTL_FIELD] = (
                    expected_marker_ttl_ns
                )
                if self.policy.allow_write_ttl:
                    updated_config["allow_msg_ttl"] = True
                await _update_nats_stream_raw_config(
                    self._js,
                    stream_name,
                    updated_config,
                )
                raw_config = updated_config
        except Exception as exc:
            raise RuntimeError(
                f"Existing NATS KV bucket {self.bucket!r} is not configured for "
                "Deckr's current KV policy. "
                f"Delete the development bucket/stream KV_{self.bucket} and restart."
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


class NatsKvMaterializedBucket:
    """Internal materialized view over one JSON KV bucket.

    This is intentionally a protocol-runtime helper, not a public generic state
    abstraction. It keeps one long-lived bucket watch and serves reads from an
    in-memory revision-indexed cache.
    """

    def __init__(
        self,
        *,
        js: Any | None = None,
        bucket: str | NatsJsonKvBucket | Any | None = None,
        policy: KvBucketPolicy | None = None,
        key_prefix: str = "",
        buffer_size: int = 100,
    ) -> None:
        if isinstance(bucket, str):
            if js is None:
                raise ValueError("js is required when bucket is a name")
            resolved_policy = policy or KvBucketPolicy(
                bucket=bucket,
                ttl_seconds=None,
            )
            self._bucket = NatsJsonKvBucket(
                js=js,
                policy=resolved_policy,
                buffer_size=buffer_size,
            )
        elif bucket is not None:
            self._bucket = bucket
        else:
            if js is None or policy is None:
                raise ValueError("bucket or js+policy is required")
            self._bucket = NatsJsonKvBucket(
                js=js,
                policy=policy,
                buffer_size=buffer_size,
            )
        self.key_prefix = key_prefix
        self._buffer_size = buffer_size
        self._ready = anyio.Event()
        self._status = KvViewStatus.STARTING
        self._status_condition = anyio.Condition()
        self._started = False
        self._entries: dict[str, KvEntry] = {}
        self._revision_by_key: dict[str, int] = {}
        self._generation = 0
        self._subscribers: set[anyio.abc.ObjectSendStream[KvChange]] = set()
        self._lock = anyio.Lock()

    @property
    def bucket(self) -> str:
        return str(self._bucket.bucket)

    @property
    def status(self) -> KvViewStatus:
        return self._status

    @property
    def generation(self) -> int:
        return self._generation

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
        self._started = True
        task_group.start_soon(self._watch_loop)

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def is_current(self) -> bool:
        return self._status == KvViewStatus.READY

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def wait_current(self) -> None:
        async with self._status_condition:
            while self._status != KvViewStatus.READY:
                if self._status == KvViewStatus.CLOSED:
                    raise KvUnavailable(
                        f"NATS KV materialized view is closed bucket={self.bucket!r}"
                    )
                await self._status_condition.wait()

    def get_cached(self, key: str) -> KvEntry | None:
        return self._entries.get(key)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return tuple(
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(prefix)
        )

    def revision_cached(self, key: str) -> int | None:
        return self._revision_by_key.get(key)

    async def get_exact(self, key: str) -> KvEntry | None:
        return await self._bucket.get(key)

    async def items_exact(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return await self._bucket.items(prefix)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        entry = await self._bucket.put(key, value, ttl=ttl)
        await self._apply_change(KvChange(self.bucket, key, entry.revision, "put", entry))
        return entry

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        entry = await self._bucket.create(key, value, ttl=ttl)
        await self._apply_change(KvChange(self.bucket, key, entry.revision, "put", entry))
        return entry

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        entry = await self._bucket.update(key, value, revision=revision, ttl=ttl)
        await self._apply_change(KvChange(self.bucket, key, entry.revision, "put", entry))
        return entry

    async def delete(self, key: str, *, revision: int | None = None) -> int | None:
        previous_revision = self._revision_by_key.get(key, 0)
        marker_revision = await self._bucket.delete(key, revision=revision)
        if previous_revision == 0 and revision is None:
            return None
        if marker_revision is None:
            return None
        await self._apply_change(KvChange(self.bucket, key, marker_revision, "delete"))
        return marker_revision

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange]]:
        send, receive = anyio.create_memory_object_stream[KvChange](
            max_buffer_size=self._buffer_size
        )
        async with self._lock:
            self._subscribers.add(send)
        try:
            async with send, receive:
                yield receive
        finally:
            async with self._lock:
                self._subscribers.discard(send)

    async def _watch_loop(self) -> None:
        retry_seconds = 1.0
        while True:
            try:
                snapshot_revisions = self._snapshot_revisions()
                async with self._bucket.watch(self.key_prefix) as changes:
                    snapshot_keys: set[str] = set()
                    snapshot_open = True
                    async for change in changes:
                        if change is None:
                            if snapshot_open:
                                await self._reconcile_snapshot(
                                    snapshot_keys,
                                    snapshot_revisions=snapshot_revisions,
                                )
                                snapshot_open = False
                            await self._set_status(KvViewStatus.READY)
                            continue
                        if snapshot_open:
                            snapshot_keys.add(change.key)
                        await self._apply_change(change)
                await self._set_status(KvViewStatus.STALE)
            except anyio.get_cancelled_exc_class():
                with anyio.CancelScope(shield=True):
                    await self._set_status(KvViewStatus.CLOSED)
                raise
            except Exception:
                await self._set_status(KvViewStatus.STALE)
                logger.warning(
                    "NATS KV materialized watch failed bucket=%s prefix=%s",
                    self.bucket,
                    self.key_prefix,
                    exc_info=True,
                )
                await anyio.sleep(retry_seconds)

    async def _set_status(self, status: KvViewStatus) -> None:
        async with self._status_condition:
            if self._status == KvViewStatus.CLOSED and status != KvViewStatus.CLOSED:
                return
            if status == KvViewStatus.READY:
                self._ready.set()
            if self._status == status:
                return
            self._status = status
            self._status_condition.notify_all()

    def _snapshot_revisions(self) -> dict[str, int]:
        return {
            key: revision
            for key, revision in self._revision_by_key.items()
            if key.startswith(self.key_prefix)
        }

    async def _reconcile_snapshot(
        self,
        snapshot_keys: set[str],
        *,
        snapshot_revisions: Mapping[str, int],
    ) -> None:
        stale_changes: list[KvChange] = []
        async with self._lock:
            for key, baseline_revision in snapshot_revisions.items():
                if key in snapshot_keys:
                    continue
                if key not in self._entries:
                    continue
                if self._revision_by_key.get(key, 0) != baseline_revision:
                    continue
                stale_changes.append(
                    KvChange(
                        self.bucket,
                        key,
                        baseline_revision + 1,
                        "delete",
                        marker_reason="watch_snapshot_absent",
                    )
                )
        for change in stale_changes:
            await self._apply_change(change)

    async def _apply_change(self, change: KvChange) -> None:
        if self.key_prefix and not change.key.startswith(self.key_prefix):
            return
        async with self._lock:
            current_revision = self._revision_by_key.get(change.key, 0)
            if change.revision <= current_revision:
                return
            self._revision_by_key[change.key] = change.revision
            if change.operation == "put" and change.entry is not None:
                self._entries[change.key] = change.entry
            else:
                self._entries.pop(change.key, None)
            self._generation += 1
            view_generation = self._generation
            delivered = replace(change, view_generation=view_generation)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                await subscriber.send(delivered)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.discard(subscriber)


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
    "KvUnavailable",
    "KvViewStatus",
    "NatsKvMaterializedBucket",
    "NatsJsonKvBucket",
]
