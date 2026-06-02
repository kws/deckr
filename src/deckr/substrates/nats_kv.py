from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import anyio

from deckr.contracts.models import DeckrModel, freeze_json, thaw_json

logger = logging.getLogger(__name__)

KV_OPERATION_HEADER = "KV-Operation"
KV_DELETE_OPERATION = "DEL"
KV_PURGE_OPERATION = "PURGE"
NATS_MARKER_REASON_HEADER = "Nats-Marker-Reason"
NATS_MARKER_MAX_AGE = "MaxAge"


class KvConflict(RuntimeError):
    """Raised when a KV create or revision-checked write fails."""


class KvUnavailable(RuntimeError):
    """Raised when a NATS KV bucket cannot answer safely."""


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

    @property
    def bucket(self) -> str:
        return self.policy.bucket

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
        try:
            revision = await kv.create(key, kv_payload(normalized))
        except Exception as exc:
            if is_revision_conflict(exc):
                raise KvConflict(f"KV key {key!r} already exists") from exc
            raise KvUnavailable(f"Could not create KV key {key!r}") from exc
        return KvEntry(self.bucket, key, normalized, int(revision))

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

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        kv = await self._available_kv()
        try:
            if revision is None:
                await kv.delete(key)
            else:
                await kv.delete(key, last=revision)
        except Exception as exc:
            if is_key_missing(exc):
                return
            if is_revision_conflict(exc):
                raise KvConflict(f"KV key {key!r} revision changed") from exc
            raise KvUnavailable(f"Could not delete KV key {key!r}") from exc

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
        needs_update = (
            getattr(config, "max_age", None) != self.policy.ttl_seconds
            or getattr(config, "max_msgs_per_subject", None) != 1
        )
        if self.policy.allow_write_ttl:
            needs_update = (
                needs_update or getattr(config, "allow_msg_ttl", None) is not True
            )
        if not needs_update:
            return
        config.max_age = self.policy.ttl_seconds
        config.max_msgs_per_subject = 1
        if self.policy.allow_write_ttl:
            config.allow_msg_ttl = True
        try:
            await self._js.update_stream(config)
        except Exception as exc:
            raise RuntimeError(
                f"Existing NATS KV bucket {self.bucket!r} is not configured for "
                "Deckr's current KV policy. "
                f"Delete the development bucket/stream KV_{self.bucket} and restart."
            ) from exc

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


def kv_value(value: Mapping[str, Any] | DeckrModel) -> Mapping[str, Any]:
    if isinstance(value, DeckrModel):
        return freeze_json(
            value.model_dump(by_alias=True, exclude_none=True, mode="json")
        )
    return freeze_json(dict(value))


def kv_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), separators=(",", ":")).encode("utf-8")


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
    if operation in {KV_DELETE_OPERATION, KV_PURGE_OPERATION}:
        return KvChange(
            bucket=bucket,
            key=key,
            revision=revision,
            operation="delete",
            marker_reason=operation,
        )
    if kv_entry_is_absent_marker(entry):
        return KvChange(
            bucket=bucket,
            key=key,
            revision=revision,
            operation="delete",
            marker_reason=operation or "absent",
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


def kv_watch_pattern(prefix: str) -> str:
    if not prefix:
        return ">"
    if prefix.endswith("."):
        return f"{prefix}>"
    return prefix


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
    "NatsJsonKvBucket",
]
