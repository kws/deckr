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
    require_text,
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
            "exact_bucket",
            "get_cached",
            "items_cached",
            "revision_cached",
            "snapshot",
            "subscribe",
            "wait_for_revision",
        )
    )


class _ExactKvAdapter:
    def __init__(self, bucket: Any, *, source: Any | None = None) -> None:
        self._bucket = bucket
        self._source = source

    @property
    def bucket(self) -> str:
        return str(self._bucket.bucket)

    async def get_exact(self, key: str) -> KvEntry | None:
        get_exact = getattr(self._bucket, "get_exact", None)
        get_value = get_exact if get_exact is not None else self._bucket.get
        try:
            return await get_value(key)
        except (KvUnavailable, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
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
            entry = await self._bucket.create(key, value, ttl=ttl)
            await self._wait_observed(key, entry.revision)
            return entry
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.KEY_ALREADY_EXISTS,
                str(exc),
                key=key,
            ) from exc
        except (KvUnavailable, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
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
            entry = await self._bucket.update(
                key,
                value,
                revision=revision,
                ttl=ttl,
            )
            await self._wait_observed(key, entry.revision)
            return entry
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                str(exc),
                key=key,
            ) from exc
        except (KvUnavailable, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def delete(self, key: str, *, revision: int) -> int | None:
        try:
            marker_revision = await self._bucket.delete(key, revision=revision)
            if marker_revision is not None:
                await self._wait_observed(key, marker_revision)
            return marker_revision
        except KvConflict as exc:
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                str(exc),
                key=key,
            ) from exc
        except (KvUnavailable, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            raise ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                str(exc),
                bucket=self.bucket,
                key=key,
            ) from exc

    async def _wait_observed(self, key: str, revision: int) -> None:
        if self._source is None:
            return
        await self._source.wait_for_revision(key, revision)

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
    def version(self) -> int:
        return int(getattr(self._bucket, "version", 0))

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

    async def aclose(self) -> None:
        await self._bucket.aclose()

    def get_cached(self, key: str) -> KvEntry | None:
        return self._bucket.get_cached(key)

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]:
        return self._bucket.items_cached(prefix)

    def revision_cached(self, key: str) -> int | None:
        revision_cached = getattr(self._bucket, "revision_cached", None)
        return None if revision_cached is None else revision_cached(key)

    async def snapshot(self):
        return await self._bucket.snapshot()

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
        exact=_ExactKvAdapter(materialized.exact_bucket, source=materialized),
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
        participant_sessions: Mapping[str | EndpointAddress, str] | None = None,
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
            participantSessions={
                str(parse_endpoint_address(participant)): require_text(
                    session_id,
                    field_name="Concord participant session id",
                )
                for participant, session_id in (participant_sessions or {}).items()
            },
            createdAt=_now_utc(),
            supersedes=supersedes,
        )
        key = concord_contract_key(
            contract_id=record.contract_id,
            generation=record.generation,
        )
        try:
            entry = await self.contract_exact.create(key, record)
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.contract_exact,
                key,
                exc,
            )
            if recovered is None:
                raise
            recovered_record = _parse_contract_entry(
                recovered,
                key,
                ContractPointer(
                    contractId=record.contract_id,
                    generation=record.generation,
                ),
            )
            if recovered_record != record:
                raise ConcordConflict(
                    ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
                    "Concord contract create outcome conflicts with the prepared record",
                    key=key,
                    expected_pointer=ContractPointer(
                        contractId=record.contract_id,
                        generation=record.generation,
                    ),
                ) from exc
            entry = recovered
        _assert_entry_key(entry, key, token=False)
        return contract_handle(key, record, entry.revision)

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        pointer = _assert_contract_handle(contract)
        entry = await self.contract_exact.get_exact(contract.key)
        if entry is None:
            return None
        return _parse_contract_entry(entry, contract.key, pointer)

    async def cancel_pointer(
        self,
        pointer: ContractPointer,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
    ) -> bool:
        """Best-effort fenced cancellation when create acknowledgement was lost."""

        key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        entry = await self.contract_exact.get_exact(key)
        if entry is None:
            return False
        record = _parse_contract_entry(entry, key, pointer)
        return await self.cancel(
            contract_handle(key, record, entry.revision),
            participant,
            reason=reason,
        )

    async def attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
    ) -> ParticipantHandle:
        token = await self.prepare_attach(
            contract,
            participant,
            session_id,
            token_id=token_id,
        )
        try:
            for _attempt in range(MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS):
                try:
                    await self.mark_participant_attached(
                        contract,
                        token.participant,
                    )
                except ConcordConflict as exc:
                    if exc.code != ConcordConflictCode.REVISION_CHANGED:
                        raise
                    if _attempt + 1 == MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS:
                        raise
                    continue
                return token
        except BaseException:
            # The raw/conformance helper never abandons a token that was created
            # before the attached-participant CAS completed. Managed lifecycle
            # code uses ``prepare_attach`` directly so it can retain the same
            # fenced handle while waiting for relevant view progress.
            with anyio.CancelScope(shield=True):
                try:
                    await self.withdraw(token)
                except (ConcordConflict, ConcordUnavailable):
                    pass
            raise
        raise AssertionError("unreachable participant attach state")

    async def prepare_attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
    ) -> ParticipantHandle:
        """Create or recover the caller-owned token before contract attachment.

        Returning the exact handle transfers cleanup ownership to the caller.
        The token is not Concord authority until ``mark_participant_attached``
        succeeds and cached/exact validation observes the complete pair.
        """

        pointer = _assert_contract_handle(contract)
        _current, record = await self._read_contract(pointer, contract.key)
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
        expected_session = record.participant_sessions.get(str(parsed_participant))
        if expected_session is not None and expected_session != session_id:
            raise ConcordConflict(
                ConcordConflictCode.SESSION_MISMATCH,
                "participant session does not match the contract session fence",
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
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.token_exact,
                key,
                exc,
            )
            if recovered is None:
                raise
            recovered_token = _parse_token_entry(
                recovered,
                key=key,
                contract_pointer=pointer,
                participant=parsed_participant,
            )
            if recovered_token != token:
                raise ConcordConflict(
                    ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
                    "Concord token create outcome conflicts with the prepared token",
                    key=key,
                    expected_pointer=pointer,
                ) from exc
            token = recovered_token
            entry = recovered
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
        else:
            _assert_entry_key(entry, key, token=True, pointer=pointer)
        return participant_handle(key, token, entry.revision)

    async def mark_participant_attached(
        self,
        contract: ContractHandle,
        participant: EndpointAddress,
    ) -> int:
        """Perform one fenced attached-participant CAS attempt."""

        pointer = _assert_contract_handle(contract)
        contract_key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
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
            return current.revision
        attached = tuple(sorted((*record.attached_participants, participant), key=str))
        updated = record.model_copy(update={"attached_participants": attached})
        _assert_contract_replacement_identity(record, updated, pointer, contract_key)
        try:
            updated_entry = await self.contract_exact.update(
                contract_key,
                updated,
                revision=current.revision,
            )
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.contract_exact,
                contract_key,
                exc,
            )
            if recovered is None:
                raise
            recovered_record = _parse_contract_entry(
                recovered,
                contract_key,
                pointer,
            )
            if recovered_record != updated:
                raise
            return recovered.revision
        except ConcordConflict as exc:
            if exc.code != ConcordConflictCode.REVISION_CHANGED:
                raise
            message = (
                "Concord contract attached-participant revision changed during "
                f"the fenced CAS attempt: {exc}"
            )
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                message,
                key=contract_key,
                expected_pointer=pointer,
            ) from exc
        return updated_entry.revision

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
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.token_exact,
                handle.key,
                exc,
            )
            if recovered is None:
                raise
            latest = _parse_token_entry_for_handle(recovered, handle, pointer)
            if latest != refreshed:
                raise
            return participant_handle(handle.key, latest, recovered.revision)
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
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                "Concord participant token changed during exact refresh",
                key=handle.key,
                expected_pointer=pointer,
            ) from exc
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
        try:
            await self.token_exact.delete(handle.key, revision=token_entry.revision)
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.token_exact,
                handle.key,
                exc,
            )
            if recovered is not None:
                _parse_token_entry_for_handle(recovered, handle, pointer)
                raise
        return True

    async def withdraw_identity(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str,
    ) -> bool:
        """Withdraw a prepared token whose create result could not be observed."""

        pointer = _assert_contract_handle(contract)
        parsed_participant = parse_endpoint_address(participant)
        key = concord_participant_token_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
            participant=parsed_participant,
        )
        entry = await self.token_exact.get_exact(key)
        if entry is None:
            return False
        token = _parse_token_entry(
            entry,
            key=key,
            contract_pointer=pointer,
            participant=parsed_participant,
        )
        if (
            token.session_id != session_id
            or token.token_id != token_id
            or token.terms_hash != contract.terms_hash
        ):
            raise ConcordConflict(
                ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
                "prepared Concord token changed owner before cleanup",
                key=key,
                expected_pointer=pointer,
            )
        try:
            await self.token_exact.delete(key, revision=entry.revision)
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.token_exact,
                key,
                exc,
            )
            if recovered is not None:
                raise
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
        try:
            await self.contract_exact.update(
                contract.key,
                cancelled,
                revision=current.revision,
            )
        except ConcordUnavailable as exc:
            recovered = await _recover_exact_entry_after_unavailable(
                self.contract_exact,
                contract.key,
                exc,
            )
            if recovered is None:
                raise
            recovered_record = _parse_contract_entry(
                recovered,
                contract.key,
                pointer,
            )
            if recovered_record != cancelled:
                raise
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


async def _recover_exact_entry_after_unavailable(
    store: ExactConcordKvPort,
    key: str,
    original: ConcordUnavailable,
) -> KvEntry | None:
    """Resolve a possibly committed mutation without allocating new identity."""

    try:
        return await store.get_exact(key)
    except ConcordUnavailable as recovery_error:
        raise original from recovery_error


def _freeze_session_assertions(
    value: Mapping[str, str] | ConcordSessionAssertions | None,
) -> ConcordSessionAssertions:
    if isinstance(value, ConcordSessionAssertions):
        return value
    return ConcordSessionAssertions.from_mapping(value)
