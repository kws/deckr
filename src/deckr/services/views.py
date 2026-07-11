from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio

from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import encode_key_token
from deckr.contracts.models import freeze_json, thaw_json
from deckr.core.util.anyio import CoalescedStateBroadcaster
from deckr.services.runtime import (
    ServiceViewReadContext,
    ServiceViewRef,
    ServiceViewWriteContext,
    ServiceViewWriter,
    UnsupportedServiceScope,
)
from deckr.substrates.nats_kv import (
    KvEntry,
    KvMaterializedChange,
    KvMaterializedSnapshot,
    KvUnavailable,
    NatsJsonKvBucket,
    NatsKvMaterializedBucket,
)

logger = logging.getLogger(__name__)
_HASH_SIZE = 12


@dataclass(frozen=True, slots=True)
class ServiceViewEntry:
    bucket: str
    storage_key: str
    key: str
    value: Mapping[str, Any]
    revision: int
    service_id: str
    service_namespace: str
    service_endpoint: str
    service_session_id: str
    consumer_endpoint: str
    consumer_session_id: str
    writer: ServiceViewWriter
    contract: ContractPointer


@dataclass(frozen=True, slots=True)
class ServiceViewWatchSnapshot:
    version: int
    current: bool
    entry: ServiceViewEntry | None


@dataclass(frozen=True, slots=True)
class ServiceViewWatchChange:
    version: int
    current: bool
    entry: ServiceViewEntry | None
    resnapshot_required: bool


@dataclass(frozen=True, slots=True)
class _ServiceViewLeaseFence:
    service_id: str
    service_namespace: str
    service_endpoint: str
    service_session_id: str
    consumer_endpoint: str
    consumer_session_id: str
    contract: ContractPointer


class ServiceViewStore:
    """Exact fenced service-view store with a coalesced materialized read model."""

    def __init__(
        self,
        *,
        bucket: NatsJsonKvBucket | NatsKvMaterializedBucket | Any,
    ) -> None:
        self._bucket = (
            bucket
            if _is_materialized_bucket(bucket)
            else NatsKvMaterializedBucket(bucket=bucket)
        )
        self._exact = self._bucket.exact_bucket
        self._entries: dict[str, ServiceViewEntry] = {}
        self._observed_revision: dict[str, int] = {}
        self._revision_condition = anyio.Condition()
        self._ready = anyio.Event()
        self._started = False
        self._state = CoalescedStateBroadcaster[str](current=False)
        self._closed = False

    @property
    def bucket(self) -> str:
        return self._bucket.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._bucket.start(task_group)
        if self._started:
            return
        self._started = True
        task_group.start_soon(self._event_loop)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._state.aclose()
        await self._bucket.aclose()
        await self._notify_revisions()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_current(self) -> bool:
        return (
            self._bucket.is_current()
            and self._ready.is_set()
            and self._state.current
        )

    async def wait_current(self) -> None:
        while not self.is_current():
            await self._bucket.wait_current()
            if self.is_current():
                return
            await anyio.sleep(0)

    async def get(
        self,
        context: ServiceViewReadContext,
        view: ServiceViewRef,
    ) -> ServiceViewEntry | None:
        self._assert_read_authorized(context, view)
        await self.wait_current()
        entry = self._entries.get(_storage_key_for_context(view, context))
        if entry is None or not _entry_matches_read_context(entry, context):
            return None
        return entry

    async def put(
        self,
        *,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        context: ServiceViewWriteContext,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        self._assert_write_authorized(context, view)
        if self._started:
            await self.wait_current()
        storage_key = _storage_key(view.key, context.contract)
        value = _fenced_payload(payload, view_key=view.key, context=context)
        entry = (
            await self._exact.put(storage_key, value, ttl=ttl)
            if revision is None
            else await self._exact.update(
                storage_key,
                value,
                revision=revision,
                ttl=ttl,
            )
        )
        await self._observe_write(storage_key, entry.revision)
        service_entry = _service_view_entry_from_kv(entry)
        logger.debug(
            "Service view write bucket=%s key=%s revision=%s service=%s "
            "namespace=%s session=%s operation=%s payload_hash=%s",
            self.bucket,
            storage_key,
            entry.revision,
            context.service_id,
            context.service_namespace,
            context.service_session_id,
            "put" if revision is None else "update",
            _payload_hash(payload),
        )
        return service_entry

    async def create(
        self,
        *,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        context: ServiceViewWriteContext,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        self._assert_write_authorized(context, view)
        if self._started:
            await self.wait_current()
        storage_key = _storage_key(view.key, context.contract)
        value = _fenced_payload(payload, view_key=view.key, context=context)
        entry = await self._exact.create(storage_key, value, ttl=ttl)
        await self._observe_write(storage_key, entry.revision)
        service_entry = _service_view_entry_from_kv(entry)
        logger.debug(
            "Service view write bucket=%s key=%s revision=%s service=%s "
            "namespace=%s session=%s operation=create payload_hash=%s",
            self.bucket,
            storage_key,
            entry.revision,
            context.service_id,
            context.service_namespace,
            context.service_session_id,
            _payload_hash(payload),
        )
        return service_entry

    async def update(
        self,
        *,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        context: ServiceViewWriteContext,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        return await self.put(
            view=view,
            payload=payload,
            context=context,
            revision=revision,
            ttl=ttl,
        )

    async def delete(
        self,
        *,
        view: ServiceViewRef,
        context: ServiceViewWriteContext,
        revision: int | None = None,
    ) -> None:
        self._assert_write_authorized(context, view)
        if self._started:
            await self.wait_current()
        storage_key = _storage_key(view.key, context.contract)
        marker_revision = await self._exact.delete(storage_key, revision=revision)
        if marker_revision is None:
            return
        await self._observe_write(storage_key, marker_revision)
        logger.debug(
            "Service view write bucket=%s key=%s revision=%s operation=delete",
            self.bucket,
            storage_key,
            marker_revision,
        )

    async def _observe_write(self, key: str, revision: int) -> None:
        if not self._started:
            return
        await self._bucket.wait_for_revision(key, revision)
        while self._observed_revision.get(key, 0) < revision:
            if self._closed:
                raise KvUnavailable(
                    "Service view closed before observing "
                    f"revision {revision} for {key!r}"
                )
            async with self._revision_condition:
                if self._observed_revision.get(key, 0) >= revision:
                    return
                await self._revision_condition.wait()

    @asynccontextmanager
    async def watch(
        self,
        context: ServiceViewReadContext,
        view: ServiceViewRef,
    ) -> AsyncIterator[
        AsyncIterator[ServiceViewWatchSnapshot | ServiceViewWatchChange]
    ]:
        self._assert_read_authorized(context, view)
        await self.wait_current()
        fence = _read_fence(context)
        storage_key = _storage_key_for_context(view, context)
        async with self._state.subscribe(
            lambda version, current: self._watch_snapshot_locked(
                storage_key,
                fence,
                version,
                current,
            )
        ) as subscription:

            async def stream() -> AsyncIterator[
                ServiceViewWatchSnapshot | ServiceViewWatchChange
            ]:
                initial = subscription.initial
                last_entry = initial.entry
                last_current = initial.current
                yield initial
                async for wakeup in subscription:
                    if (
                        not wakeup.resnapshot_required
                        and storage_key not in wakeup.changed
                        and wakeup.current == last_current
                    ):
                        continue
                    try:
                        snapshot = await self._state.capture(
                            lambda version, current: self._watch_snapshot_locked(
                                storage_key,
                                fence,
                                version,
                                current,
                            )
                        )
                    except anyio.ClosedResourceError:
                        return
                    if (
                        not wakeup.resnapshot_required
                        and snapshot.entry == last_entry
                        and snapshot.current == last_current
                    ):
                        continue
                    last_entry = snapshot.entry
                    last_current = snapshot.current
                    yield ServiceViewWatchChange(
                        version=snapshot.version,
                        current=snapshot.current,
                        entry=snapshot.entry,
                        resnapshot_required=wakeup.resnapshot_required,
                    )

            yield stream()

    def _watch_snapshot_locked(
        self,
        storage_key: str,
        fence: _ServiceViewLeaseFence,
        version: int,
        current: bool,
    ) -> ServiceViewWatchSnapshot:
        entry = self._entries.get(storage_key)
        if entry is not None and not _entry_matches_fence(entry, fence):
            entry = None
        return ServiceViewWatchSnapshot(
            version=version,
            current=current,
            entry=entry,
        )

    async def _event_loop(self) -> None:
        try:
            async with self._bucket.subscribe() as changes:
                async for item in changes:
                    if isinstance(item, KvMaterializedSnapshot):
                        await self._install_snapshot(item)
                    else:
                        await self._consume_change(item)
        except anyio.ClosedResourceError:
            return

    async def _consume_change(self, change: KvMaterializedChange) -> None:
        if change.resnapshot_required:
            await self._install_snapshot(await self._bucket.snapshot())
            return
        parsed = {
            key: _parse_service_view_entry(self._bucket.get_cached(key))
            for key in change.changed_keys
        }
        async with self._state.lock:
            for key, entry in parsed.items():
                if entry is None:
                    self._entries.pop(key, None)
                else:
                    self._entries[key] = entry
                revision = self._bucket.revision_cached(key)
                if revision is not None:
                    self._observed_revision[key] = revision
            current_changed = self._state.current != change.current
            if change.changed_keys or current_changed:
                self._state.publish_locked(
                    change.changed_keys,
                    current=change.current,
                )
            if change.current:
                self._ready.set()
        await self._notify_revisions()

    async def _install_snapshot(self, snapshot: KvMaterializedSnapshot) -> None:
        entries = {
            entry.storage_key: entry
            for raw in snapshot.entries
            if (entry := _parse_service_view_entry(raw)) is not None
        }
        async with self._state.lock:
            previous_keys = set(self._entries)
            changed = frozenset(
                key
                for key in previous_keys | entries.keys()
                if self._entries.get(key) != entries.get(key)
            )
            self._entries = entries
            self._observed_revision = {
                raw.key: raw.revision for raw in snapshot.entries
            }
            for key in previous_keys - self._observed_revision.keys():
                revision = self._bucket.revision_cached(key)
                if revision is not None:
                    self._observed_revision[key] = revision
            current_changed = self._state.current != snapshot.current
            if changed or current_changed or not self._ready.is_set():
                self._state.publish_locked(
                    changed,
                    current=snapshot.current,
                    resnapshot_required=len(changed) > 256,
                )
            if snapshot.current:
                self._ready.set()
        await self._notify_revisions()

    async def _notify_revisions(self) -> None:
        async with self._revision_condition:
            self._revision_condition.notify_all()

    def _assert_read_authorized(
        self,
        context: ServiceViewReadContext,
        view: ServiceViewRef,
    ) -> None:
        if view.store_name != self.bucket:
            raise UnsupportedServiceScope(
                f"Service view {view.key!r} is not in bucket {self.bucket!r}"
            )
        for view_family in context.views.values():
            if view.store_name != view_family.store_name:
                continue
            if view.key.startswith(view_family.key_prefix):
                if view_family.writer == context.reader:
                    raise UnsupportedServiceScope(
                        f"Service view {view.key!r} is written by "
                        f"{context.reader.value}"
                    )
                return
        raise UnsupportedServiceScope(
            f"Service read context does not authorize view {view.key!r}"
        )

    def _assert_write_authorized(
        self,
        context: ServiceViewWriteContext,
        view: ServiceViewRef,
    ) -> None:
        if view.store_name != self.bucket:
            raise UnsupportedServiceScope(
                f"Service view {view.key!r} is not in bucket {self.bucket!r}"
            )
        for view_family in context.views.values():
            if view.store_name != view_family.store_name:
                continue
            if not view.key.startswith(view_family.key_prefix):
                continue
            if view_family.writer != context.writer:
                raise UnsupportedServiceScope(
                    f"Service view {view.key!r} writer is "
                    f"{view_family.writer.value}, not {context.writer.value}"
                )
            return
        raise UnsupportedServiceScope(
            f"Service write context does not authorize view {view.key!r}"
        )



class ManagedServiceViewAccess:
    """Lease-bound retained-view access for one service-use contract."""

    def __init__(
        self,
        store: ServiceViewStore,
        *,
        read_context: ServiceViewReadContext | None = None,
        write_context: ServiceViewWriteContext | None = None,
    ) -> None:
        self._store = store
        self._read_context = read_context
        self._write_context = write_context

    async def read(self, view: ServiceViewRef) -> ServiceViewEntry | None:
        return await self._store.get(self._require_read_context(), view)

    @asynccontextmanager
    async def watch(
        self,
        view: ServiceViewRef,
    ) -> AsyncIterator[
        AsyncIterator[ServiceViewWatchSnapshot | ServiceViewWatchChange]
    ]:
        async with self._store.watch(self._require_read_context(), view) as changes:
            yield changes

    async def create(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        return await self._store.create(
            view=view,
            payload=payload,
            context=self._require_write_context(),
            ttl=ttl,
        )

    async def put(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        return await self._store.put(
            view=view,
            payload=payload,
            context=self._require_write_context(),
            revision=revision,
            ttl=ttl,
        )

    async def update(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        return await self._store.update(
            view=view,
            payload=payload,
            context=self._require_write_context(),
            revision=revision,
            ttl=ttl,
        )

    async def delete(
        self,
        view: ServiceViewRef,
        *,
        revision: int | None = None,
    ) -> None:
        await self._store.delete(
            view=view,
            context=self._require_write_context(),
            revision=revision,
        )

    def _require_read_context(self) -> ServiceViewReadContext:
        if self._read_context is None:
            raise UnsupportedServiceScope("managed service view access is write-only")
        return self._read_context

    def _require_write_context(self) -> ServiceViewWriteContext:
        if self._write_context is None:
            raise UnsupportedServiceScope("managed service view access is read-only")
        return self._write_context


def _fenced_payload(
    payload: Mapping[str, Any],
    *,
    view_key: str,
    context: ServiceViewWriteContext,
) -> Mapping[str, Any]:
    return freeze_json(
        {
            **thaw_json(payload),
            "viewKey": view_key,
            "serviceId": context.service_id,
            "serviceNamespace": context.service_namespace,
            "serviceEndpoint": str(context.service_endpoint),
            "serviceSessionId": context.service_session_id,
            "consumerEndpoint": str(context.consumer_endpoint),
            "consumerSessionId": context.consumer_session_id,
            "writer": context.writer.value,
            "contractId": context.contract.contract_id,
            "generation": context.contract.generation,
        }
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    value = json.dumps(
        thaw_json(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_SIZE]


def _service_view_entry_from_kv(entry: KvEntry) -> ServiceViewEntry:
    value = freeze_json(dict(entry.value))
    key = _required_value(value, "viewKey")
    service_id = _required_value(value, "serviceId")
    service_namespace = _required_value(value, "serviceNamespace")
    service_endpoint = _required_value(value, "serviceEndpoint")
    service_session_id = _required_value(value, "serviceSessionId")
    consumer_endpoint = _required_value(value, "consumerEndpoint")
    consumer_session_id = _required_value(value, "consumerSessionId")
    writer = ServiceViewWriter(_required_value(value, "writer"))
    contract_id = _required_value(value, "contractId")
    generation = value.get("generation")
    if not isinstance(generation, int):
        raise ValueError("service view value requires generation")
    contract = ContractPointer(contractId=contract_id, generation=generation)
    return ServiceViewEntry(
        bucket=entry.bucket,
        storage_key=entry.key,
        key=key,
        value=value,
        revision=entry.revision,
        service_id=service_id,
        service_namespace=service_namespace,
        service_endpoint=service_endpoint,
        service_session_id=service_session_id,
        consumer_endpoint=consumer_endpoint,
        consumer_session_id=consumer_session_id,
        writer=writer,
        contract=contract,
    )


def _parse_service_view_entry(entry: KvEntry | None) -> ServiceViewEntry | None:
    if entry is None:
        return None
    try:
        return _service_view_entry_from_kv(entry)
    except (TypeError, ValueError):
        return None


def _required_value(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"service view value requires {key}")
    return item


def _entry_matches_read_context(
    entry: ServiceViewEntry,
    context: ServiceViewReadContext,
) -> bool:
    return _entry_matches_fence(entry, _read_fence(context))


def _read_fence(context: ServiceViewReadContext) -> _ServiceViewLeaseFence:
    return _ServiceViewLeaseFence(
        service_id=context.service_id,
        service_namespace=context.service_namespace,
        service_endpoint=str(context.service_endpoint),
        service_session_id=context.service_session_id,
        consumer_endpoint=str(context.consumer_endpoint),
        consumer_session_id=context.consumer_session_id,
        contract=context.contract,
    )


def _entry_matches_fence(
    entry: ServiceViewEntry,
    fence: _ServiceViewLeaseFence,
) -> bool:
    return (
        entry.service_id == fence.service_id
        and entry.service_namespace == fence.service_namespace
        and entry.service_endpoint == fence.service_endpoint
        and entry.service_session_id == fence.service_session_id
        and entry.consumer_endpoint == fence.consumer_endpoint
        and entry.consumer_session_id == fence.consumer_session_id
        and entry.contract == fence.contract
    )


def _storage_key_for_context(
    view: ServiceViewRef,
    context: ServiceViewReadContext | ServiceViewWriteContext,
) -> str:
    return _storage_key(view.key, context.contract)


def _storage_key(logical_key: str, contract: ContractPointer) -> str:
    return (
        f"{logical_key}.contract.{encode_key_token(contract.contract_id)}."
        f"{contract.generation}"
    )


def _contract_pointer_from_handle(value: Any) -> ContractPointer:
    return ContractPointer(contractId=value.contract_id, generation=value.generation)


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


__all__ = [
    "ManagedServiceViewAccess",
    "ServiceViewEntry",
    "ServiceViewStore",
    "ServiceViewWatchChange",
    "ServiceViewWatchSnapshot",
]
