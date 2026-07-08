from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import anyio

from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import encode_key_token
from deckr.contracts.models import freeze_json, thaw_json
from deckr.services.runtime import (
    ServiceViewReadContext,
    ServiceViewRef,
    ServiceViewWriteContext,
    ServiceViewWriter,
    UnsupportedServiceScope,
)
from deckr.substrates.nats_kv import (
    KvChange,
    KvEntry,
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
class ServiceViewChange:
    operation: Literal["put", "delete", "expire"]
    bucket: str
    key: str
    revision: int
    entry: ServiceViewEntry | None = None
    storage_key: str | None = None


@dataclass(frozen=True, slots=True)
class _ServiceViewLeaseFence:
    service_id: str
    service_namespace: str
    service_endpoint: str
    service_session_id: str
    consumer_endpoint: str
    consumer_session_id: str
    contract: ContractPointer


@dataclass(slots=True)
class _ServiceViewSubscriber:
    key: str
    storage_key: str
    fence: _ServiceViewLeaseFence
    visible: bool


class ServiceViewStore:
    """Direct KV-backed materialized service-view store."""

    def __init__(
        self,
        *,
        bucket: NatsJsonKvBucket | NatsKvMaterializedBucket | Any,
        buffer_size: int = 100,
    ) -> None:
        self._bucket = (
            bucket
            if _is_materialized_bucket(bucket)
            else NatsKvMaterializedBucket(bucket=bucket, buffer_size=buffer_size)
        )
        self._buffer_size = buffer_size
        self._ready = anyio.Event()
        self._started = False
        self._entries: dict[str, ServiceViewEntry] = {}
        self._revision_by_key: dict[str, int] = {}
        self._bucket_generation = 0
        self._subscribers: dict[
            anyio.abc.ObjectSendStream[ServiceViewChange],
            _ServiceViewSubscriber,
        ] = {}
        self._lock = anyio.Lock()

    @property
    def bucket(self) -> str:
        return self._bucket.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._bucket.start(task_group)
        if self._started:
            return
        self._started = True
        task_group.start_soon(self._event_loop)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_current(self) -> bool:
        return (
            self._ready.is_set()
            and self._bucket.is_current()
            and self._bucket_generation == _bucket_generation_cached(self._bucket)
        )

    async def wait_current(self) -> None:
        await self.wait_ready()
        while True:
            await self._bucket.wait_current()
            if self._bucket_generation == _bucket_generation_cached(self._bucket):
                return
            await self._rebuild_from_bucket()
            await anyio.sleep(0)

    async def get(
        self,
        context: ServiceViewReadContext,
        view: ServiceViewRef,
    ) -> ServiceViewEntry | None:
        self._assert_read_authorized(context, view)
        await self.wait_current()
        storage_key = _storage_key_for_context(view, context)
        async with self._lock:
            entry = self._entries.get(storage_key)
        if entry is None:
            return None
        if not _entry_matches_read_context(entry, context):
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
        pointer = context.contract
        storage_key = _storage_key(view.key, pointer)
        value = _fenced_payload(
            payload,
            view_key=view.key,
            context=context,
        )
        entry = (
            await self._bucket.put(storage_key, value, ttl=ttl)
            if revision is None
            else await self._bucket.update(
                storage_key,
                value,
                revision=revision,
                ttl=ttl,
            )
        )
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
        await self._apply_service_change(
            ServiceViewChange(
                "put",
                self.bucket,
                view.key,
                entry.revision,
                service_entry,
                storage_key,
            ),
            view_generation=_bucket_generation_cached(self._bucket),
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
        pointer = context.contract
        storage_key = _storage_key(view.key, pointer)
        value = _fenced_payload(
            payload,
            view_key=view.key,
            context=context,
        )
        entry = await self._bucket.create(storage_key, value, ttl=ttl)
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
        await self._apply_service_change(
            ServiceViewChange(
                "put",
                self.bucket,
                view.key,
                entry.revision,
                service_entry,
                storage_key,
            ),
            view_generation=_bucket_generation_cached(self._bucket),
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
        pointer = context.contract
        storage_key = _storage_key(view.key, pointer)
        marker_revision = await self._bucket.delete(storage_key, revision=revision)
        if marker_revision is None:
            return
        logger.debug(
            "Service view write bucket=%s key=%s revision=%s operation=delete",
            self.bucket,
            storage_key,
            marker_revision,
        )
        await self._apply_service_change(
            ServiceViewChange(
                "delete",
                self.bucket,
                view.key,
                marker_revision,
                storage_key=storage_key,
            ),
            view_generation=_bucket_generation_cached(self._bucket),
        )

    @asynccontextmanager
    async def watch(
        self,
        context: ServiceViewReadContext,
        view: ServiceViewRef,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ServiceViewChange]]:
        self._assert_read_authorized(context, view)
        await self.wait_current()
        send, receive = anyio.create_memory_object_stream[ServiceViewChange](
            max_buffer_size=self._buffer_size
        )
        fence = _read_fence(context)
        storage_key = _storage_key_for_context(view, context)
        async with self._lock:
            current = self._entries.get(storage_key)
            self._subscribers[send] = _ServiceViewSubscriber(
                key=view.key,
                storage_key=storage_key,
                fence=fence,
                visible=current is not None and _entry_matches_fence(current, fence),
            )
        try:
            async with send, receive:
                yield receive
        finally:
            async with self._lock:
                self._subscribers.pop(send, None)

    async def _event_loop(self) -> None:
        await self._bucket.wait_current()
        async with self._bucket.subscribe() as changes:
            await self._rebuild_from_bucket()
            self._ready.set()
            async for change in changes:
                await self._apply_kv_change(change)

    async def _rebuild_from_bucket(self) -> None:
        entries: dict[str, ServiceViewEntry] = {}
        revisions: dict[str, int] = {}
        bucket_generation = _bucket_generation_cached(self._bucket)
        for entry in self._bucket.items_cached():
            revisions[entry.key] = entry.revision
            try:
                service_entry = _service_view_entry_from_kv(entry)
            except ValueError:
                continue
            entries[entry.key] = service_entry
        async with self._lock:
            self._entries = entries
            self._revision_by_key = revisions
            self._bucket_generation = bucket_generation

    async def _apply_kv_change(self, change: KvChange) -> None:
        if await self._rebuild_if_generation_gap(change):
            return
        if change.operation == "put" and change.entry is not None:
            try:
                entry = _service_view_entry_from_kv(change.entry)
            except ValueError:
                entry = None
            await self._apply_service_change(
                ServiceViewChange(
                    "put",
                    self.bucket,
                    entry.key
                    if entry is not None
                    else _logical_key_from_storage_key(change.key),
                    change.revision,
                    entry,
                    change.key,
                ),
                view_generation=change.view_generation,
            )
            return
        await self._apply_service_change(
            ServiceViewChange(
                change.operation,
                self.bucket,
                _logical_key_from_storage_key(change.key),
                change.revision,
                storage_key=change.key,
            ),
            view_generation=change.view_generation,
        )

    async def _rebuild_if_generation_gap(self, change: KvChange) -> bool:
        if change.view_generation is None:
            return False
        async with self._lock:
            gap = change.view_generation > self._bucket_generation + 1
        if not gap:
            return False
        await self._rebuild_from_bucket()
        return True

    async def _apply_service_change(
        self,
        change: ServiceViewChange,
        *,
        view_generation: int | None = None,
    ) -> None:
        async with self._lock:
            if not _change_generation_is_next(
                view_generation,
                current_generation=self._bucket_generation,
            ):
                logger.debug(
                    "Service view stale change ignored bucket=%s key=%s "
                    "operation=%s revision=%s view_generation=%s "
                    "current_generation=%s reason=generation",
                    change.bucket,
                    change.key,
                    change.operation,
                    change.revision,
                    view_generation,
                    self._bucket_generation,
                )
                return
            storage_key = _change_storage_key(change)
            current_revision = self._revision_by_key.get(storage_key, 0)
            if change.revision <= current_revision:
                self._advance_bucket_generation_locked(view_generation)
                logger.debug(
                    "Service view stale change ignored bucket=%s key=%s "
                    "operation=%s revision=%s current_revision=%s "
                    "view_generation=%s reason=revision",
                    change.bucket,
                    change.key,
                    change.operation,
                    change.revision,
                    current_revision,
                    view_generation,
                )
                return
            self._revision_by_key[storage_key] = change.revision
            if change.operation == "put" and change.entry is not None:
                self._entries[storage_key] = change.entry
            else:
                self._entries.pop(storage_key, None)
            self._advance_bucket_generation_locked(view_generation)
            deliveries: list[
                tuple[
                    anyio.abc.ObjectSendStream[ServiceViewChange],
                    ServiceViewChange,
                ]
            ] = []
            for subscriber, state in self._subscribers.items():
                if state.storage_key != storage_key:
                    continue
                delivery = _subscriber_delivery(change, state)
                if delivery is not None:
                    deliveries.append((subscriber, delivery))
        delivered_count = 0
        for subscriber, delivery in deliveries:
            try:
                subscriber.send_nowait(delivery)
                delivered_count += 1
            except anyio.WouldBlock:
                continue
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.pop(subscriber, None)
        entry = change.entry
        logger.debug(
            "Service view change applied bucket=%s key=%s operation=%s "
            "revision=%s storage_key=%s service=%s namespace=%s session=%s "
            "contract=%s generation=%s payload_hash=%s delivery_count=%s",
            change.bucket,
            change.key,
            change.operation,
            change.revision,
            _change_storage_key(change),
            entry.service_id if entry is not None else None,
            entry.service_namespace if entry is not None else None,
            entry.service_session_id if entry is not None else None,
            entry.contract.contract_id if entry is not None else None,
            entry.contract.generation if entry is not None else None,
            _payload_hash(entry.value) if entry is not None else None,
            delivered_count,
        )

    def _advance_bucket_generation_locked(self, view_generation: int | None) -> None:
        if view_generation is None:
            view_generation = _bucket_generation_cached(self._bucket)
        self._bucket_generation = max(self._bucket_generation, view_generation)

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
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ServiceViewChange]]:
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


def _subscriber_delivery(
    change: ServiceViewChange,
    state: _ServiceViewSubscriber,
) -> ServiceViewChange | None:
    if change.operation == "put":
        if change.entry is not None and _entry_matches_fence(change.entry, state.fence):
            state.visible = True
            return change
        if state.visible:
            state.visible = False
            return ServiceViewChange(
                "delete",
                change.bucket,
                change.key,
                change.revision,
                storage_key=change.storage_key,
            )
        return None

    if state.visible:
        state.visible = False
        return change
    return None


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


def _logical_key_from_storage_key(storage_key: str) -> str:
    return storage_key.rsplit(".contract.", 1)[0]


def _change_storage_key(change: ServiceViewChange) -> str:
    return change.storage_key or change.key


def _contract_pointer_from_handle(value: Any) -> ContractPointer:
    return ContractPointer(contractId=value.contract_id, generation=value.generation)


def _change_generation_is_next(
    view_generation: int | None,
    *,
    current_generation: int,
) -> bool:
    if view_generation is None:
        return True
    if view_generation <= current_generation:
        return False
    return view_generation == current_generation + 1


def _bucket_generation_cached(bucket: Any) -> int:
    return int(getattr(bucket, "generation", 0))


def _is_materialized_bucket(value: Any) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            "start",
            "wait_ready",
            "is_current",
            "wait_current",
            "get_exact",
            "get_cached",
            "items_cached",
            "revision_cached",
            "subscribe",
            "put",
            "create",
            "update",
            "delete",
        )
    )


__all__ = [
    "ManagedServiceViewAccess",
    "ServiceViewChange",
    "ServiceViewEntry",
    "ServiceViewStore",
]
