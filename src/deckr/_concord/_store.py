from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio

from deckr._concord._keys import (
    canonical_json_hash,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr._concord._models import (
    ConcordConflict,
    ConcordConflictCode,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ParticipantHandle,
    ParticipantTokenRecord,
    contract_handle,
    contract_handle_has_canonical_identity,
    participant_handle,
    participant_handle_has_canonical_identity,
    token_matches_handle,
)
from deckr._concord._ports import (
    ConcordMaintenanceScanPort,
    ConcordMaterializedSourcePort,
    ConcordRawMaintenanceStorePort,
    ExactConcordKvPort,
)
from deckr._concord._validation import (
    ConcordObservationState,
    ConcordSessionAssertions,
    ContractObservation,
    ParticipantTokenObservation,
    contract_observation_from_entry,
    evaluate_contract_validity,
    token_observation_from_entry,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import (
    KvConflict,
    KvEntry,
    KvUnavailable,
    NatsKvMaterializedBucket,
)

MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS = 8


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _normalize_materialized_bucket(value: NatsKvMaterializedBucket | Any) -> Any:
    if _is_materialized_bucket(value):
        return value
    return NatsKvMaterializedBucket(bucket=value)


def _is_materialized_bucket(value: Any) -> bool:
    return all(
        hasattr(value, name)
        for name in (
            "start",
            "wait_ready",
            "is_ready",
            "is_current",
            "wait_current",
            "get_exact",
            "get_cached",
            "items_cached",
            "items_exact",
            "revision_cached",
            "subscribe",
            "create",
            "update",
            "delete",
        )
    )


class _ExactKvAdapter:
    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return str(self._bucket.bucket)

    async def get_exact(self, key: str) -> KvEntry | None:
        get_exact = getattr(self._bucket, "get_exact", None)
        get_value = get_exact if get_exact is not None else self._bucket.get
        try:
            return await get_value(key)
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        try:
            return await self._bucket.create(key, value, ttl=ttl)
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.KEY_ALREADY_EXISTS,
                str(exc),
                key=key,
            ) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        try:
            return await self._bucket.update(
                key,
                value,
                revision=revision,
                ttl=ttl,
            )
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                str(exc),
                key=key,
            ) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def delete(self, key: str, *, revision: int) -> int | None:
        try:
            return await self._bucket.delete(key, revision=revision)
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                str(exc),
                key=key,
            ) from exc
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def ttl_seconds(self) -> int:
        ttl_seconds = getattr(self._bucket, "ttl_seconds", None)
        if ttl_seconds is None:
            raise ConcordUnavailable(
                ConcordUnavailableCode.TTL_METADATA_MISSING,
                f"Concord KV bucket {self.bucket!r} does not expose TTL metadata",
                bucket=self.bucket,
            )
        try:
            value = ttl_seconds()
            if hasattr(value, "__await__"):
                value = await value
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.TTL_INVALID,
                f"Concord participant token bucket {self.bucket!r} TTL is invalid",
                bucket=self.bucket,
            ) from exc
        if value is None:
            raise ConcordUnavailable(
                ConcordUnavailableCode.TTL_METADATA_MISSING,
                f"Concord participant token bucket {self.bucket!r} must be TTL-bound",
                bucket=self.bucket,
            )
        try:
            ttl = float(value)
            rounded = int(ttl)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.TTL_INVALID,
                f"Concord participant token bucket {self.bucket!r} TTL is invalid",
                bucket=self.bucket,
            ) from exc
        if ttl <= 0 or abs(ttl - rounded) > 0.001:
            raise ConcordUnavailable(
                ConcordUnavailableCode.TTL_INVALID,
                f"Concord participant token bucket {self.bucket!r} TTL must be positive whole seconds",
                bucket=self.bucket,
            )
        return rounded


class _MaterializedSourceAdapter:
    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return str(self._bucket.bucket)

    @property
    def generation(self) -> int:
        return int(getattr(self._bucket, "generation", 0))

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._bucket.start(task_group)

    def is_ready(self) -> bool:
        return bool(self._bucket.is_ready())

    def is_current(self) -> bool:
        return bool(self._bucket.is_current())

    async def wait_ready(self) -> None:
        try:
            await self._bucket.wait_ready()
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.SOURCE_STALE,
                str(exc),
                bucket=self.bucket,
            ) from exc

    async def wait_current(self) -> None:
        try:
            await self._bucket.wait_current()
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.SOURCE_STALE,
                str(exc),
                bucket=self.bucket,
            ) from exc

    def get_cached(self, key: str) -> KvEntry | None:
        return self._bucket.get_cached(key)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return self._bucket.items_cached(prefix)

    def revision_cached(self, key: str) -> int | None:
        revision_cached = getattr(self._bucket, "revision_cached", None)
        return None if revision_cached is None else revision_cached(key)

    @asynccontextmanager
    async def subscribe(self):
        async with self._bucket.subscribe() as changes:
            yield changes


class _MaintenanceScanAdapter:
    def __init__(self, bucket: Any) -> None:
        self._exact = _ExactKvAdapter(bucket)
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._exact.bucket

    async def items_exact(self, prefix: str = "") -> tuple[KvEntry, ...]:
        items_exact = getattr(self._bucket, "items_exact", None)
        items = items_exact if items_exact is not None else self._bucket.items
        try:
            return await items(prefix)
        except KvUnavailable as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
            ) from exc

    async def get_exact(self, key: str) -> KvEntry | None:
        return await self._exact.get_exact(key)

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry:
        return await self._exact.create(key, value, ttl=ttl)

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry:
        return await self._exact.update(
            key,
            value,
            revision=revision,
            ttl=ttl,
        )

    async def delete(self, key: str, *, revision: int) -> int | None:
        return await self._exact.delete(key, revision=revision)


@dataclass(frozen=True, slots=True)
class ConcordBucketAdapters:
    exact: ExactConcordKvPort
    source: ConcordMaterializedSourcePort


def concord_bucket_adapters(
    bucket: NatsKvMaterializedBucket | Any,
) -> ConcordBucketAdapters:
    materialized = _normalize_materialized_bucket(bucket)
    return ConcordBucketAdapters(
        exact=_ExactKvAdapter(materialized),
        source=_MaterializedSourceAdapter(materialized),
    )


def concord_maintenance_scan_store(
    bucket: ConcordRawMaintenanceStorePort | ConcordMaintenanceScanPort,
) -> ConcordMaintenanceScanPort:
    """Adapt one raw KV bucket to the exact maintenance scan boundary.

    Unlike :func:`concord_bucket_adapters`, this constructor never composes a
    materialized view. Prefix scans therefore use the raw store's temporary
    list consumer and maintenance construction starts no watch or background
    task.
    """

    required_methods = {
        "exact read": ("get_exact", "get"),
        "exact prefix scan": ("items_exact", "items"),
        "create": ("create",),
        "revision update": ("update",),
        "revision delete": ("delete",),
    }
    missing = [
        operation
        for operation, names in required_methods.items()
        if not any(callable(getattr(bucket, name, None)) for name in names)
    ]
    if missing or not hasattr(bucket, "bucket"):
        operations = ", ".join(missing or ["bucket identity"])
        raise TypeError(f"Concord maintenance store is missing {operations}")
    return _MaintenanceScanAdapter(bucket)


class ConcordKvStore:
    def __init__(
        self,
        contract_bucket: NatsKvMaterializedBucket | Any,
        token_bucket: NatsKvMaterializedBucket | Any,
    ) -> None:
        contract = concord_bucket_adapters(contract_bucket)
        token = concord_bucket_adapters(token_bucket)
        self.contract_exact = contract.exact
        self.contract_source = contract.source
        self.token_exact = token.exact
        self.token_source = token.source

    async def create_contract(
        self,
        participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress],
        *,
        contract_id: str | None = None,
        generation: int = 1,
        profile: str | None = None,
        terms: Mapping[str, Any] | DeckrModel | None = None,
        created_by: str | EndpointAddress | None = None,
        supersedes: ContractPointer | Mapping[str, Any] | None = None,
    ) -> ContractHandle:
        parsed_participants = tuple(
            sorted((parse_endpoint_address(item) for item in participants), key=str)
        )
        dumped_terms = (
            terms.model_dump(by_alias=True, exclude_none=True, mode="json")
            if isinstance(terms, DeckrModel)
            else terms
        )
        terms_hash = canonical_json_hash(dumped_terms) if dumped_terms is not None else None
        record = ContractRecord(
            contractId=contract_id or str(uuid.uuid4()),
            generation=generation,
            participants=parsed_participants,
            attachedParticipants=(),
            state=ContractState.OPEN,
            profile=profile,
            termsHash=terms_hash,
            terms=dumped_terms,
            createdBy=parse_endpoint_address(created_by) if created_by is not None else None,
            createdAt=_now_utc(),
            supersedes=supersedes,
        )
        key = concord_contract_key(
            contract_id=record.contract_id,
            generation=record.generation,
        )
        entry = await self.contract_exact.create(key, record)
        _assert_entry_key(entry, key, token=False)
        return contract_handle(key, record, entry.revision)

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        pointer = _assert_contract_handle(contract)
        entry = await self.contract_exact.get_exact(contract.key)
        if entry is None:
            return None
        return _parse_contract_entry(entry, contract.key, pointer)

    async def attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
    ) -> ParticipantHandle:
        pointer = _assert_contract_handle(contract)
        current, record = await self._read_contract(pointer, contract.key)
        if record.state == ContractState.CANCELLED:
            raise ConcordConflict(
                ConcordConflictCode.CONTRACT_CANCELLED,
                f"Concord contract {contract.key!r} is cancelled",
                key=contract.key,
                expected_pointer=pointer,
            )
        parsed_participant = parse_endpoint_address(participant)
        if parsed_participant not in record.participants:
            raise ConcordConflict(
                ConcordConflictCode.PARTICIPANT_NOT_NAMED,
                "participant is not named by the Concord contract",
                key=contract.key,
                expected_pointer=pointer,
            )
        key = concord_participant_token_key(
            contract_id=record.contract_id,
            generation=record.generation,
            participant=parsed_participant,
        )
        if parsed_participant in record.attached_participants:
            token_entry = await self.token_exact.get_exact(key)
            if token_entry is not None:
                existing_token = _parse_token_entry(
                    token_entry,
                    key=key,
                    contract_pointer=pointer,
                    participant=parsed_participant,
                )
                if _token_matches_attach_request(
                    existing_token,
                    record=record,
                    participant=parsed_participant,
                    session_id=session_id,
                    token_id=token_id,
                ):
                    return participant_handle(key, existing_token, token_entry.revision)
            raise ConcordConflict(
                ConcordConflictCode.PARTICIPANT_ALREADY_ATTACHED,
                "Concord participant is already attached",
                key=key,
                expected_pointer=pointer,
            )
        ttl = await self.token_exact.ttl_seconds()
        token = ParticipantTokenRecord(
            contractId=record.contract_id,
            generation=record.generation,
            participant=parsed_participant,
            sessionId=session_id,
            tokenId=token_id or str(uuid.uuid4()),
            refreshSeq=1,
            ttlSeconds=ttl,
            termsHash=record.terms_hash,
        )
        try:
            entry = await self.token_exact.create(key, token, ttl=token.ttl_seconds)
        except ConcordConflict as exc:
            if exc.code != ConcordConflictCode.KEY_ALREADY_EXISTS:
                raise
            token_entry = await self.token_exact.get_exact(key)
            if token_entry is None:
                raise ConcordConflict(
                    ConcordConflictCode.TOKEN_ALREADY_EXISTS,
                    "Concord participant token changed during attach",
                    key=key,
                    expected_pointer=pointer,
                ) from exc
            observed = _parse_token_entry(
                token_entry,
                key=key,
                contract_pointer=pointer,
                participant=parsed_participant,
            )
            if not _token_matches_attach_request(
                observed,
                record=record,
                participant=parsed_participant,
                session_id=session_id,
                token_id=token_id,
            ):
                raise ConcordConflict(
                    ConcordConflictCode.TOKEN_ALREADY_EXISTS,
                    "Concord participant token already exists",
                    key=key,
                    expected_pointer=pointer,
                ) from exc
            token = observed
            entry = token_entry
            await self._mark_participant_attached(
                pointer=pointer,
                participant=parsed_participant,
                allow_already_attached=False,
            )
        else:
            _assert_entry_key(entry, key, token=True, pointer=pointer)
            await self._mark_participant_attached(
                pointer=pointer,
                participant=parsed_participant,
                allow_already_attached=True,
            )
        return participant_handle(key, token, entry.revision)

    async def _mark_participant_attached(
        self,
        *,
        pointer: ContractPointer,
        participant: EndpointAddress,
        allow_already_attached: bool,
    ) -> None:
        contract_key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        last_conflict: ConcordConflict | None = None
        for _attempt in range(MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS):
            current, record = await self._read_contract(pointer, contract_key)
            if record.state == ContractState.CANCELLED:
                raise ConcordConflict(
                    ConcordConflictCode.CONTRACT_CANCELLED,
                    f"Concord contract {contract_key!r} is cancelled",
                    key=contract_key,
                    expected_pointer=pointer,
                )
            if participant not in record.participants:
                raise ConcordConflict(
                    ConcordConflictCode.PARTICIPANT_NOT_NAMED,
                    "participant is not named by the Concord contract",
                    key=contract_key,
                    expected_pointer=pointer,
                )
            if participant in record.attached_participants:
                if allow_already_attached:
                    return
                raise ConcordConflict(
                    ConcordConflictCode.PARTICIPANT_ALREADY_ATTACHED,
                    "Concord participant is already attached",
                    key=contract_key,
                    expected_pointer=pointer,
                )
            attached = tuple(sorted((*record.attached_participants, participant), key=str))
            updated = record.model_copy(update={"attached_participants": attached})
            _assert_contract_replacement_identity(record, updated, pointer, contract_key)
            try:
                await self.contract_exact.update(
                    contract_key,
                    updated,
                    revision=current.revision,
                )
            except ConcordConflict as exc:
                if exc.code != ConcordConflictCode.REVISION_CHANGED:
                    raise
                last_conflict = exc
                continue
            return
        message = (
            "Concord contract attached-participant revision changed during all "
            f"{MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS} CAS attempts; final conflict: "
            f"{last_conflict}"
        )
        raise ConcordConflict(
            ConcordConflictCode.REVISION_CHANGED,
            message,
            key=contract_key,
            expected_pointer=pointer,
        ) from last_conflict

    async def refresh(self, handle: ParticipantHandle) -> ParticipantHandle:
        pointer = _assert_participant_handle(handle)
        contract_key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        _, contract = await self._read_contract(pointer, contract_key)
        if contract.state == ContractState.CANCELLED:
            raise ConcordConflict(
                ConcordConflictCode.CONTRACT_CANCELLED,
                "Concord contract is cancelled",
                key=contract_key,
                expected_pointer=pointer,
            )
        token_entry = await self.token_exact.get_exact(handle.key)
        if token_entry is None:
            raise ConcordConflict(
                ConcordConflictCode.TOKEN_MISSING,
                "Concord participant token is missing",
                key=handle.key,
                expected_pointer=pointer,
            )
        token = _parse_token_entry_for_handle(token_entry, handle, pointer)
        if token.terms_hash != contract.terms_hash:
            raise _token_identity_conflict(handle, pointer)
        ttl = await self.token_exact.ttl_seconds()
        refreshed = token.model_copy(
            update={"refresh_seq": token.refresh_seq + 1, "ttl_seconds": ttl}
        )
        _assert_token_replacement_identity(token, refreshed, handle, pointer)
        try:
            entry = await self.token_exact.update(
                handle.key,
                refreshed,
                revision=token_entry.revision,
                ttl=refreshed.ttl_seconds,
            )
        except ConcordConflict as exc:
            if exc.code != ConcordConflictCode.REVISION_CHANGED:
                raise
            latest_entry = await self.token_exact.get_exact(handle.key)
            if latest_entry is None:
                raise ConcordConflict(
                    ConcordConflictCode.TOKEN_MISSING,
                    "Concord participant token is missing",
                    key=handle.key,
                    expected_pointer=pointer,
                ) from exc
            latest = _parse_token_entry_for_handle(latest_entry, handle, pointer)
            if latest.terms_hash != contract.terms_hash:
                raise _token_identity_conflict(handle, pointer) from exc
            return participant_handle(handle.key, latest, latest_entry.revision)
        _assert_entry_key(entry, handle.key, token=True, pointer=pointer)
        return participant_handle(handle.key, refreshed, entry.revision)

    async def validate_participant_handle(
        self,
        handle: ParticipantHandle,
    ) -> ParticipantHandle:
        pointer = _assert_participant_handle(handle)
        contract_key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        _, contract = await self._read_contract(pointer, contract_key)
        if contract.state == ContractState.CANCELLED:
            raise ConcordConflict(
                ConcordConflictCode.CONTRACT_CANCELLED,
                "Concord contract is cancelled",
                key=contract_key,
                expected_pointer=pointer,
            )
        token_entry = await self.token_exact.get_exact(handle.key)
        if token_entry is None:
            raise ConcordConflict(
                ConcordConflictCode.TOKEN_MISSING,
                "Concord participant token is missing",
                key=handle.key,
                expected_pointer=pointer,
            )
        token = _parse_token_entry_for_handle(token_entry, handle, pointer)
        if token.terms_hash != contract.terms_hash:
            raise _token_identity_conflict(handle, pointer)
        return participant_handle(handle.key, token, token_entry.revision)

    async def withdraw(self, handle: ParticipantHandle) -> bool:
        pointer = _assert_participant_handle(handle)
        token_entry = await self.token_exact.get_exact(handle.key)
        if token_entry is None:
            return False
        _parse_token_entry_for_handle(token_entry, handle, pointer)
        await self.token_exact.delete(handle.key, revision=token_entry.revision)
        return True

    async def cancel(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
    ) -> bool:
        pointer = _assert_contract_handle(contract)
        current = await self.contract_exact.get_exact(contract.key)
        if current is None:
            return False
        record = _parse_contract_entry(current, contract.key, pointer)
        if record.state == ContractState.CANCELLED:
            return False
        parsed_participant = parse_endpoint_address(participant)
        if parsed_participant not in record.participants:
            raise ConcordConflict(
                ConcordConflictCode.PARTICIPANT_NOT_NAMED,
                "participant is not named by the Concord contract",
                key=contract.key,
                expected_pointer=pointer,
            )
        cancelled = record.model_copy(
            update={
                "state": ContractState.CANCELLED,
                "cancelled_by": parsed_participant,
                "cancelled_at": _now_utc(),
                "cancel_revision": current.revision,
                "cancel_reason": reason,
            }
        )
        _assert_contract_replacement_identity(record, cancelled, pointer, contract.key)
        await self.contract_exact.update(
            contract.key,
            cancelled,
            revision=current.revision,
        )
        return True

    async def validate_exact(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
    ) -> ContractValidity:
        assertions = _freeze_session_assertions(current_sessions)
        pointer = ContractPointer(
            contractId=contract.contract_id,
            generation=contract.generation,
        )
        key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        try:
            entry = await self.contract_exact.get_exact(key)
        except ConcordUnavailable as exc:
            contract_observation = ContractObservation(
                key,
                None,
                ConcordObservationState.UNAVAILABLE,
                diagnostic=str(exc),
            )
            token_observations: tuple[ParticipantTokenObservation, ...] = ()
        else:
            contract_observation = contract_observation_from_entry(key, entry)
            participants = (
                contract_observation.record.participants
                if contract_observation.record is not None
                else contract.participants
            )
            token_observations = await self._exact_token_observations(
                pointer,
                participants,
            )
        return evaluate_contract_validity(
            expected_key=contract.key,
            expected_pointer=pointer,
            contract=contract_observation,
            tokens=token_observations,
            session_assertions=assertions,
        )

    async def _exact_token_observations(
        self,
        pointer: ContractPointer,
        participants: tuple[EndpointAddress, ...],
    ) -> tuple[ParticipantTokenObservation, ...]:
        observations: list[ParticipantTokenObservation] = []
        for participant in sorted(participants, key=str):
            key = concord_participant_token_key(
                contract_id=pointer.contract_id,
                generation=pointer.generation,
                participant=participant,
            )
            try:
                entry = await self.token_exact.get_exact(key)
            except ConcordUnavailable as exc:
                observations.append(
                    ParticipantTokenObservation(
                        key,
                        None,
                        participant,
                        ConcordObservationState.UNAVAILABLE,
                        diagnostic=str(exc),
                    )
                )
            else:
                observations.append(token_observation_from_entry(key, participant, entry))
        return tuple(observations)

    async def _read_contract(
        self,
        pointer: ContractPointer,
        key: str,
    ) -> tuple[KvEntry, ContractRecord]:
        entry = await self.contract_exact.get_exact(key)
        if entry is None:
            raise ConcordConflict(
                ConcordConflictCode.CONTRACT_MISSING,
                f"Concord contract {key!r} is missing",
                key=key,
                expected_pointer=pointer,
            )
        return entry, _parse_contract_entry(entry, key, pointer)


def _assert_contract_handle(contract: ContractHandle) -> ContractPointer:
    pointer = ContractPointer(
        contractId=contract.contract_id,
        generation=contract.generation,
    )
    if not contract_handle_has_canonical_identity(contract):
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            f"Concord contract {contract.key!r} changed identity",
            key=contract.key,
            expected_pointer=pointer,
        )
    return pointer


def _assert_participant_handle(handle: ParticipantHandle) -> ContractPointer:
    pointer = ContractPointer(
        contractId=handle.contract_id,
        generation=handle.generation,
    )
    if not participant_handle_has_canonical_identity(handle):
        raise _token_identity_conflict(handle, pointer)
    return pointer


def _parse_contract_entry(
    entry: KvEntry,
    key: str,
    pointer: ContractPointer,
) -> ContractRecord:
    _assert_entry_key(entry, key, token=False, pointer=pointer)
    try:
        record = ContractRecord.model_validate(entry.value)
    except (TypeError, ValueError) as exc:
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_INVALID,
            f"Concord contract {key!r} is malformed: {exc}",
            key=key,
            expected_pointer=pointer,
        ) from exc
    if (
        record.contract_id != pointer.contract_id
        or record.generation != pointer.generation
    ):
        observed = ContractPointer(
            contractId=record.contract_id,
            generation=record.generation,
        )
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            f"Concord contract {key!r} changed identity",
            key=key,
            expected_pointer=pointer,
            observed_pointer=observed,
        )
    return record


def _parse_token_entry(
    entry: KvEntry,
    *,
    key: str,
    contract_pointer: ContractPointer,
    participant: EndpointAddress,
) -> ParticipantTokenRecord:
    _assert_entry_key(entry, key, token=True, pointer=contract_pointer)
    try:
        token = ParticipantTokenRecord.model_validate(entry.value)
    except (TypeError, ValueError) as exc:
        raise ConcordConflict(
            ConcordConflictCode.TOKEN_INVALID,
            f"Concord participant token {key!r} is malformed: {exc}",
            key=key,
            expected_pointer=contract_pointer,
        ) from exc
    if (
        token.contract_id != contract_pointer.contract_id
        or token.generation != contract_pointer.generation
        or token.participant != participant
    ):
        raise ConcordConflict(
            ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
            "Concord participant token changed owner",
            key=key,
            expected_pointer=contract_pointer,
        )
    return token


def _parse_token_entry_for_handle(
    entry: KvEntry,
    handle: ParticipantHandle,
    pointer: ContractPointer,
) -> ParticipantTokenRecord:
    token = _parse_token_entry(
        entry,
        key=handle.key,
        contract_pointer=pointer,
        participant=handle.participant,
    )
    if not token_matches_handle(token, handle):
        raise _token_identity_conflict(handle, pointer)
    return token


def _assert_entry_key(
    entry: KvEntry,
    key: str,
    *,
    token: bool,
    pointer: ContractPointer | None = None,
) -> None:
    if entry.key == key:
        return
    raise ConcordConflict(
        (
            ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
            if token
            else ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH
        ),
        f"Concord KV entry key {entry.key!r} differs from requested key {key!r}",
        key=key,
        expected_pointer=pointer,
    )


def _assert_contract_replacement_identity(
    current: ContractRecord,
    replacement: ContractRecord,
    pointer: ContractPointer,
    key: str,
) -> None:
    if (
        replacement.contract_id == current.contract_id == pointer.contract_id
        and replacement.generation == current.generation == pointer.generation
    ):
        return
    raise ConcordConflict(
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
        f"Concord contract {key!r} replacement changed identity",
        key=key,
        expected_pointer=pointer,
    )


def _assert_token_replacement_identity(
    current: ParticipantTokenRecord,
    replacement: ParticipantTokenRecord,
    handle: ParticipantHandle,
    pointer: ContractPointer,
) -> None:
    if token_matches_handle(current, handle) and token_matches_handle(replacement, handle):
        return
    raise _token_identity_conflict(handle, pointer)


def _token_identity_conflict(
    handle: ParticipantHandle,
    pointer: ContractPointer,
) -> ConcordConflict:
    return ConcordConflict(
        ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
        "Concord participant token changed owner",
        key=handle.key,
        expected_pointer=pointer,
    )


def _token_matches_attach_request(
    token: ParticipantTokenRecord,
    *,
    record: ContractRecord,
    participant: EndpointAddress,
    session_id: str,
    token_id: str | None,
) -> bool:
    return (
        token.contract_id == record.contract_id
        and token.generation == record.generation
        and token.participant == participant
        and token.session_id == session_id
        and token_id is not None
        and token.token_id == token_id
        and token.terms_hash == record.terms_hash
    )


def _freeze_session_assertions(
    value: Mapping[str, str] | ConcordSessionAssertions | None,
) -> ConcordSessionAssertions:
    if isinstance(value, ConcordSessionAssertions):
        return value
    return ConcordSessionAssertions.from_mapping(value)
