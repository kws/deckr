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
    ServiceUseLease,
    ServiceViewRef,
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
    session_id: str
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
    session_id: str
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
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> ServiceViewEntry | None:
        self._assert_authorized(lease, view)
        await lease.refresh()
        await self.wait_current()
        storage_key = _storage_key_for_lease(view, lease)
        async with self._lock:
            entry = self._entries.get(storage_key)
        if entry is None:
            return None
        if not _entry_matches_lease(entry, lease):
            return None
        return entry

    async def put(
        self,
        *,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        service_id: str,
        service_namespace: str,
        session_id: str,
        contract: ContractPointer | Mapping[str, Any] | Any,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        if view.store_name != self.bucket:
            raise ValueError(
                f"Service view store {view.store_name!r} does not match bucket "
                f"{self.bucket!r}"
            )
        if self._started:
            await self.wait_current()
        pointer = _coerce_contract_pointer(contract)
        storage_key = _storage_key(view.key, pointer)
        value = _fenced_payload(
            payload,
            view_key=view.key,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=session_id,
            contract=pointer,
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
            service_id,
            service_namespace,
            session_id,
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
        service_id: str,
        service_namespace: str,
        session_id: str,
        contract: ContractPointer | Mapping[str, Any] | Any,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        if view.store_name != self.bucket:
            raise ValueError(
                f"Service view store {view.store_name!r} does not match bucket "
                f"{self.bucket!r}"
            )
        if self._started:
            await self.wait_current()
        pointer = _coerce_contract_pointer(contract)
        storage_key = _storage_key(view.key, pointer)
        value = _fenced_payload(
            payload,
            view_key=view.key,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=session_id,
            contract=pointer,
        )
        entry = await self._bucket.create(storage_key, value, ttl=ttl)
        service_entry = _service_view_entry_from_kv(entry)
        logger.debug(
            "Service view write bucket=%s key=%s revision=%s service=%s "
            "namespace=%s session=%s operation=create payload_hash=%s",
            self.bucket,
            storage_key,
            entry.revision,
            service_id,
            service_namespace,
            session_id,
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
        service_id: str,
        service_namespace: str,
        session_id: str,
        contract: ContractPointer | Mapping[str, Any] | Any,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        return await self.put(
            view=view,
            payload=payload,
            service_id=service_id,
            service_namespace=service_namespace,
            session_id=session_id,
            contract=contract,
            revision=revision,
            ttl=ttl,
        )

    async def delete(
        self,
        *,
        view: ServiceViewRef,
        contract: ContractPointer | Mapping[str, Any] | Any,
        revision: int | None = None,
    ) -> None:
        if view.store_name != self.bucket:
            raise ValueError(
                f"Service view store {view.store_name!r} does not match bucket "
                f"{self.bucket!r}"
            )
        if self._started:
            await self.wait_current()
        pointer = _coerce_contract_pointer(contract)
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
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ServiceViewChange]]:
        self._assert_authorized(lease, view)
        await lease.refresh()
        await self.wait_current()
        send, receive = anyio.create_memory_object_stream[ServiceViewChange](
            max_buffer_size=self._buffer_size
        )
        fence = _lease_fence(lease)
        storage_key = _storage_key_for_lease(view, lease)
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
            entry.session_id if entry is not None else None,
            entry.contract.contract_id if entry is not None else None,
            entry.contract.generation if entry is not None else None,
            _payload_hash(entry.value) if entry is not None else None,
            delivered_count,
        )

    def _advance_bucket_generation_locked(self, view_generation: int | None) -> None:
        if view_generation is None:
            view_generation = _bucket_generation_cached(self._bucket)
        self._bucket_generation = max(self._bucket_generation, view_generation)

    def _assert_authorized(self, lease: ServiceUseLease, view: ServiceViewRef) -> None:
        if view.store_name != self.bucket:
            raise UnsupportedServiceScope(
                f"Service view {view.key!r} is not in bucket {self.bucket!r}"
            )
        for family, prefixes in lease.terms.allowed_views.items():
            view_family = lease.descriptor.views.get(family)
            if view_family is None:
                continue
            if view.store_name != view_family.store_name:
                continue
            if any(view.key.startswith(prefix) for prefix in prefixes):
                return
        raise UnsupportedServiceScope(
            f"Service-use lease does not authorize view {view.key!r}"
        )


def _fenced_payload(
    payload: Mapping[str, Any],
    *,
    view_key: str,
    service_id: str,
    service_namespace: str,
    session_id: str,
    contract: ContractPointer,
) -> Mapping[str, Any]:
    return freeze_json(
        {
            **thaw_json(payload),
            "viewKey": view_key,
            "serviceId": service_id,
            "serviceNamespace": service_namespace,
            "sessionId": session_id,
            "contractId": contract.contract_id,
            "generation": contract.generation,
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
    session_id = _required_value(value, "sessionId")
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
        session_id=session_id,
        contract=contract,
    )


def _required_value(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"service view value requires {key}")
    return item


def _entry_matches_lease(entry: ServiceViewEntry, lease: ServiceUseLease) -> bool:
    return _entry_matches_fence(entry, _lease_fence(lease))


def _lease_fence(lease: ServiceUseLease) -> _ServiceViewLeaseFence:
    descriptor = lease.descriptor
    return _ServiceViewLeaseFence(
        service_id=descriptor.service_id,
        service_namespace=descriptor.namespace,
        session_id=descriptor.session_id,
        contract=_contract_pointer_from_handle(lease.contract),
    )


def _entry_matches_fence(
    entry: ServiceViewEntry,
    fence: _ServiceViewLeaseFence,
) -> bool:
    return (
        entry.service_id == fence.service_id
        and entry.service_namespace == fence.service_namespace
        and entry.session_id == fence.session_id
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


def _storage_key_for_lease(view: ServiceViewRef, lease: ServiceUseLease) -> str:
    return _storage_key(view.key, _contract_pointer_from_handle(lease.contract))


def _storage_key(logical_key: str, contract: ContractPointer) -> str:
    return (
        f"{logical_key}.contract.{encode_key_token(contract.contract_id)}."
        f"{contract.generation}"
    )


def _logical_key_from_storage_key(storage_key: str) -> str:
    return storage_key.rsplit(".contract.", 1)[0]


def _change_storage_key(change: ServiceViewChange) -> str:
    return change.storage_key or change.key


def _coerce_contract_pointer(
    value: ContractPointer | Mapping[str, Any] | Any,
) -> ContractPointer:
    if isinstance(value, ContractPointer):
        return value
    if isinstance(value, Mapping):
        return ContractPointer.model_validate(value)
    contract_id = getattr(value, "contract_id", None)
    generation = getattr(value, "generation", None)
    if contract_id is not None and generation is not None:
        return ContractPointer(contractId=contract_id, generation=generation)
    return ContractPointer.model_validate(value)


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
    "ServiceViewChange",
    "ServiceViewEntry",
    "ServiceViewStore",
]
