from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deckr._authority_buckets import (
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
)
from deckr._concord._keys import (
    concord_contract_key,
    concord_participant_token_key,
)
from deckr._concord._models import (
    ContractHandle,
    ContractRecord,
    ParticipantHandle,
    ParticipantTokenRecord,
    contract_handle,
    participant_handle,
)
from deckr.concord import Concord
from deckr.concord_maintenance import ConcordMaintenance
from deckr.contracts.authority import ContractPointer
from deckr.substrates.nats_kv import (
    KvChange,
    KvEntry,
    KvViewStatus,
    NatsKvMaterializedBucket,
)
from deckr.testing.kv import MemoryJsonKvBucket


def _runtime_concord(
    contract_store: Any,
    token_store: Any,
    *,
    buffer_size: int,
) -> Concord:
    return Concord(
        contract_store,
        token_store,
        buffer_size=buffer_size,
    )


def _materialized(store: Any) -> Any:
    if all(
        hasattr(store, name)
        for name in (
            "get_exact",
            "get_cached",
            "items_exact",
            "items_cached",
            "subscribe",
            "start",
        )
    ):
        return store
    return NatsKvMaterializedBucket(bucket=store)


class ConcordRuntimeHarness:
    def __init__(
        self,
        *,
        contract_store: Any | None = None,
        token_store: Any | None = None,
        token_ttl_seconds: float = 120,
        buffer_size: int = 100,
    ) -> None:
        self.contract_store = contract_store or MemoryJsonKvBucket(
            bucket=DEFAULT_CONCORD_CONTRACT_BUCKET_NAME
        )
        self.token_store = token_store or MemoryJsonKvBucket(
            bucket=DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
            ttl_seconds=token_ttl_seconds,
        )
        self._contract_view = _materialized(self.contract_store)
        self._token_view = _materialized(self.token_store)
        self.concord = _runtime_concord(
            self._contract_view,
            self._token_view,
            buffer_size=buffer_size,
        )

    async def seed_contract(self, record: ContractRecord) -> ContractHandle:
        key = concord_contract_key(
            contract_id=record.contract_id,
            generation=record.generation,
        )
        entry = await self.seed_raw_contract(key, record.to_dict())
        return contract_handle(key, record, entry.revision)

    async def seed_token(self, record: ParticipantTokenRecord) -> ParticipantHandle:
        key = concord_participant_token_key(
            contract_id=record.contract_id,
            generation=record.generation,
            participant=record.participant,
        )
        entry = await self.seed_raw_token(key, record.to_dict())
        return participant_handle(key, record, entry.revision)

    async def seed_raw_contract(
        self,
        key: str,
        value: Mapping[str, Any],
    ) -> KvEntry:
        return await self.contract_store.put(key, value)

    async def seed_raw_token(
        self,
        key: str,
        value: Mapping[str, Any],
    ) -> KvEntry:
        return await self.token_store.put(key, value)

    async def materialize(self) -> None:
        await _materialize_store(self._contract_view, self.contract_store)
        await _materialize_store(self._token_view, self.token_store)
        await self.concord._rebuild_from_buckets()  # noqa: SLF001

    async def contract_entry(
        self,
        key_or_pointer: str | ContractPointer | Mapping[str, Any],
    ) -> KvEntry | None:
        key = (
            key_or_pointer
            if isinstance(key_or_pointer, str)
            else concord_contract_key(
                contract_id=(
                    key_or_pointer.contract_id
                    if isinstance(key_or_pointer, ContractPointer)
                    else ContractPointer.model_validate(key_or_pointer).contract_id
                ),
                generation=(
                    key_or_pointer.generation
                    if isinstance(key_or_pointer, ContractPointer)
                    else ContractPointer.model_validate(key_or_pointer).generation
                ),
            )
        )
        return await self.contract_store.get(key)

    async def token_entry(
        self,
        key_or_handle: str | ParticipantHandle,
    ) -> KvEntry | None:
        key = key_or_handle if isinstance(key_or_handle, str) else key_or_handle.key
        return await self.token_store.get(key)

    async def expire_token(self, key_or_handle: str | ParticipantHandle) -> bool:
        key = key_or_handle if isinstance(key_or_handle, str) else key_or_handle.key
        expire = getattr(self.token_store, "expire", None)
        if expire is None:
            raise TypeError("token store does not support deterministic expiry")
        return bool(await expire(key))


class ConcordMaintenanceHarness(ConcordRuntimeHarness):
    def __init__(
        self,
        *,
        contract_store: Any | None = None,
        token_store: Any | None = None,
        maintenance_store: Any | None = None,
        token_ttl_seconds: float = 120,
        buffer_size: int = 100,
    ) -> None:
        super().__init__(
            contract_store=contract_store,
            token_store=token_store,
            token_ttl_seconds=token_ttl_seconds,
            buffer_size=buffer_size,
        )
        self.maintenance_store = maintenance_store or MemoryJsonKvBucket(
            bucket=DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME
        )
        self.maintenance = ConcordMaintenance(
            self.contract_store,
            self.token_store,
            self.maintenance_store,
        )


async def _materialize_store(view: Any, store: Any | None) -> None:
    if store is None or view is store:
        return
    apply_change = getattr(view, "_apply_change", None)
    if apply_change is None:
        return
    entries = await store.items()
    present = {entry.key for entry in entries}
    for entry in entries:
        await apply_change(
            KvChange(view.bucket, entry.key, entry.revision, "put", entry)
        )
    for cached in view.items_cached():
        if cached.key in present:
            continue
        await apply_change(
            KvChange(view.bucket, cached.key, cached.revision + 1, "delete")
        )
    set_status = getattr(view, "_set_status", None)
    if set_status is not None:
        await set_status(KvViewStatus.READY)
