from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

import anyio
from pydantic import Field, field_serializer, field_validator

from deckr._concord._keys import (
    concord_contract_key,
    concord_contract_prefix,
    concord_contracts_prefix,
    concord_participant_token_key,
    concord_stale_observation_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
    parse_concord_stale_observation_key,
)
from deckr._concord._models import (
    ConcordConflict,
    ConcordConflictCode,
    ConcordUnavailable,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    ParticipantTokenRecord,
    contract_handle,
    contract_handle_has_canonical_identity,
    require_text,
)
from deckr._concord._ports import (
    ConcordMaintenanceScanPort,
    ConcordRawMaintenanceStorePort,
)
from deckr._concord._store import concord_maintenance_scan_store
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
from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvEntry

CONCORD_STALE_OBSERVATION_SCHEMA_ID = "dev.deckr.concord.stale-observation.v1"
DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS = 900
DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS = 3600
DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS = 60
CONCORD_MAINTENANCE_ACTOR = "concord:maintenance"
CONCORD_REAPER_STALE_CONTRACT_REASON = "concord_reaper_stale_contract"

_MAX_OBSERVATION_CAS_ATTEMPTS = 8
_MAX_TOKEN_CLEANUP_SCAN_ATTEMPTS = 8

logger = logging.getLogger("deckr.concord")


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ConcordStaleObservationRecord(DeckrModel):
    schema_id: Literal[CONCORD_STALE_OBSERVATION_SCHEMA_ID] = Field(
        default=CONCORD_STALE_OBSERVATION_SCHEMA_ID,
        alias="schema",
    )
    contract_id: str = Field(alias="contractId")
    generation: int
    first_observed_stale_at: datetime = Field(alias="firstObservedStaleAt")
    status: ContractValidityStatus
    reason: str | None = None
    contract_revision: int | None = Field(default=None, alias="contractRevision")

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return require_text(value, field_name="contract id")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation must be greater than zero")
        return value

    @field_validator("first_observed_stale_at")
    @classmethod
    def _validate_first_observed_stale_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("firstObservedStaleAt must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_text(value, field_name="Concord stale observation reason")

    @field_validator("contract_revision")
    @classmethod
    def _validate_contract_revision(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("contractRevision must be non-negative")
        return value

    @field_serializer("first_observed_stale_at")
    def _serialize_first_observed_stale_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ConcordReaperConfig(DeckrModel):
    stale_grace_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS),
        ge=0,
        alias="staleGraceSeconds",
    )
    cancelled_retention_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS),
        ge=0,
        alias="cancelledRetentionSeconds",
    )
    scan_interval_seconds: float = Field(
        default=float(DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS),
        gt=0,
        alias="scanIntervalSeconds",
    )
    log_label: str = Field(default="ConcordReaper", alias="logLabel")

    @field_validator("log_label")
    @classmethod
    def _validate_log_label(cls, value: str) -> str:
        return require_text(value, field_name="Concord reaper log label")


@dataclass(frozen=True, slots=True)
class ConcordMaintenanceDeletionResult:
    deleted: bool
    deleted_token_key_count: int = 0


@dataclass(frozen=True, slots=True)
class ConcordReaperScanResult:
    scanned_contract_count: int = 0
    stale_observation_count: int = 0
    stale_observations_created: int = 0
    stale_observations_cleared: int = 0
    contracts_cancelled: int = 0
    contracts_deleted: int = 0
    token_keys_deleted: int = 0


STALE_OPEN_CONTRACT_STATUSES = frozenset(
    {
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.INVALID_CONTRACT,
    }
)


class ConcordMaintenance:
    """Exact/scanning Concord maintenance authority with no runtime watch."""

    def __init__(
        self,
        contract_store: ConcordRawMaintenanceStorePort | ConcordMaintenanceScanPort,
        token_store: ConcordRawMaintenanceStorePort | ConcordMaintenanceScanPort,
        maintenance_store: ConcordRawMaintenanceStorePort | ConcordMaintenanceScanPort,
    ) -> None:
        self._contract_store = concord_maintenance_scan_store(contract_store)
        self._token_store = concord_maintenance_scan_store(token_store)
        self._maintenance_store = concord_maintenance_scan_store(maintenance_store)

    @property
    def contract_bucket(self) -> str:
        return self._contract_store.bucket

    @property
    def token_bucket(self) -> str:
        return self._token_store.bucket

    @property
    def maintenance_bucket(self) -> str:
        return self._maintenance_store.bucket

    async def validate_exact(
        self,
        contract: ContractHandle,
        *,
        current_sessions: (
            Mapping[str | EndpointAddress, str] | ConcordSessionAssertions | None
        ) = None,
    ) -> ContractValidity:
        """Validate one known contract pointer using exact store reads only."""

        assertions = (
            current_sessions
            if isinstance(current_sessions, ConcordSessionAssertions)
            else ConcordSessionAssertions(current_sessions)
        )
        pointer = ContractPointer(
            contractId=contract.contract_id,
            generation=contract.generation,
        )
        key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        try:
            entry = await self._contract_store.get_exact(key)
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

    async def cancel_contract(
        self,
        contract: ContractHandle,
        *,
        reason: str = CONCORD_REAPER_STALE_CONTRACT_REASON,
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> bool:
        """Terminally cancel an open contract as the maintenance actor."""

        pointer = _assert_contract_handle(contract)
        current = await self._contract_store.get_exact(contract.key)
        if current is None:
            return False
        record = _parse_contract_entry(current, contract.key, pointer)
        if record.state == ContractState.CANCELLED:
            return False
        cancelled = record.model_copy(
            update={
                "state": ContractState.CANCELLED,
                "cancelled_by": CONCORD_MAINTENANCE_ACTOR,
                "cancelled_at": _ensure_utc(now or _now_utc()),
                "cancel_revision": current.revision,
                "cancel_reason": reason,
            }
        )
        _assert_contract_replacement_identity(record, cancelled, pointer, contract.key)
        await self._contract_store.update(
            contract.key,
            cancelled,
            revision=current.revision,
        )
        logger.info(
            "%s Concord maintenance cancelled contract profile=%s contract=%s "
            "generation=%s cancelled_by=%s reason=%s revision=%s",
            log_label,
            record.profile,
            record.contract_id,
            record.generation,
            CONCORD_MAINTENANCE_ACTOR,
            reason,
            current.revision,
        )
        return True

    async def delete_cancelled_contract(
        self,
        contract: ContractHandle,
        *,
        retention_seconds: float = float(
            DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS
        ),
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> ConcordMaintenanceDeletionResult:
        """Delete an eligible retained contract and its current participant tokens."""

        pointer = _assert_contract_handle(contract)
        observed = await self._contract_store.get_exact(contract.key)
        if observed is None:
            return ConcordMaintenanceDeletionResult(deleted=False)
        record = _parse_contract_entry(observed, contract.key, pointer)
        if record.state != ContractState.CANCELLED:
            return ConcordMaintenanceDeletionResult(deleted=False)
        current_time = _ensure_utc(now or _now_utc())
        if record.cancelled_at is None or (
            current_time - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < retention_seconds:
            return ConcordMaintenanceDeletionResult(deleted=False)

        validity = await self.validate_exact(contract)
        token_entries = await _concord_participant_token_entries(
            self._token_store,
            contract_id=contract.contract_id,
            generation=contract.generation,
            terms_hash=record.terms_hash,
        )
        _log_concord_contract_deletion_audit(
            log_label=log_label,
            contract_key=contract.key,
            record=record,
            state_revision=observed.revision,
            validation_status=validity.status,
            token_entries=token_entries,
            deleted_token_key_count=len(token_entries),
        )
        try:
            await self._contract_store.delete(
                contract.key,
                revision=observed.revision,
            )
        except ConcordConflict:
            logger.info(
                "%s Concord maintenance delete conflict; leaving contract "
                "for next scan contract_key=%s contract=%s generation=%s "
                "revision=%s",
                log_label,
                contract.key,
                contract.contract_id,
                contract.generation,
                observed.revision,
                exc_info=True,
            )
            return ConcordMaintenanceDeletionResult(deleted=False)

        deleted_token_count = await _delete_concord_participant_tokens_until_empty(
            self._token_store,
            contract_id=contract.contract_id,
            generation=contract.generation,
            terms_hash=record.terms_hash,
            initial_entries=token_entries,
            log_label=log_label,
        )
        return ConcordMaintenanceDeletionResult(
            deleted=True,
            deleted_token_key_count=deleted_token_count,
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
                entry = await self._token_store.get_exact(key)
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

    async def _contract_entries(self) -> tuple[KvEntry, ...]:
        return await self._contract_store.items_exact(concord_contracts_prefix())

    async def _stale_observation_entries(self) -> tuple[KvEntry, ...]:
        return await self._maintenance_store.items_exact("stale.")

    async def _observe_stale(
        self,
        *,
        contract_id: str,
        generation: int,
        status: ContractValidityStatus,
        reason: str | None,
        contract_revision: int | None,
        now: datetime,
    ) -> tuple[datetime, bool]:
        key = concord_stale_observation_key(
            contract_id=contract_id,
            generation=generation,
        )
        candidate_time = _ensure_utc(now)
        candidate = ConcordStaleObservationRecord(
            contractId=contract_id,
            generation=generation,
            firstObservedStaleAt=candidate_time,
            status=status,
            reason=reason,
            contractRevision=contract_revision,
        )
        last_conflict: ConcordConflict | None = None
        for _attempt in range(_MAX_OBSERVATION_CAS_ATTEMPTS):
            current = await self._maintenance_store.get_exact(key)
            if current is None:
                try:
                    await self._maintenance_store.create(key, candidate)
                except ConcordConflict as exc:
                    last_conflict = exc
                    continue
                return candidate_time, True

            record = _parse_stale_observation_entry(
                current,
                key=key,
                contract_id=contract_id,
                generation=generation,
            )
            if record.first_observed_stale_at <= candidate_time:
                return record.first_observed_stale_at, False
            try:
                await self._maintenance_store.update(
                    key,
                    candidate,
                    revision=current.revision,
                )
            except ConcordConflict as exc:
                last_conflict = exc
                continue
            return candidate_time, False

        raise ConcordConflict(
            ConcordConflictCode.REVISION_CHANGED,
            "Concord stale observation revision kept changing",
            key=key,
            expected_pointer=ContractPointer(
                contractId=contract_id,
                generation=generation,
            ),
        ) from last_conflict

    async def _clear_stale_observation(
        self,
        contract_id: str,
        generation: int,
    ) -> bool:
        key = concord_stale_observation_key(
            contract_id=contract_id,
            generation=generation,
        )
        current = await self._maintenance_store.get_exact(key)
        if current is None:
            return False
        _parse_stale_observation_entry(
            current,
            key=key,
            contract_id=contract_id,
            generation=generation,
        )
        try:
            await self._maintenance_store.delete(key, revision=current.revision)
        except ConcordConflict:
            return False
        return True

    async def _clear_orphaned_stale_observations(self) -> int:
        cleared = 0
        for entry in await self._stale_observation_entries():
            try:
                parsed = parse_concord_stale_observation_key(entry.key)
                if parsed is None:
                    continue
                observation = _parse_stale_observation_entry(
                    entry,
                    key=entry.key,
                    contract_id=parsed[0],
                    generation=parsed[1],
                )
            except ConcordConflict:
                continue
            contract_entry = await self._contract_store.get_exact(
                concord_contract_key(
                    contract_id=observation.contract_id,
                    generation=observation.generation,
                )
            )
            if contract_entry is not None:
                continue
            try:
                await self._maintenance_store.delete(
                    entry.key,
                    revision=entry.revision,
                )
            except ConcordConflict:
                continue
            cleared += 1
        return cleared


class ConcordReaperService:
    """Periodic low-frequency scanning over a Concord maintenance capability."""

    def __init__(
        self,
        maintenance: ConcordMaintenance,
        *,
        config: ConcordReaperConfig | Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._maintenance = maintenance
        self._config = (
            config
            if isinstance(config, ConcordReaperConfig)
            else ConcordReaperConfig.model_validate(dict(config or {}))
        )
        self._clock = clock
        self._closed = False

    @property
    def config(self) -> ConcordReaperConfig:
        return self._config

    async def aclose(self) -> None:
        self._closed = True

    async def run(self, *, stop_event: anyio.Event | None = None) -> None:
        while not self._closed:
            try:
                await self.scan_once()
            except ConcordUnavailable:
                logger.warning(
                    "%s Concord reaper state unavailable; scan will retry",
                    self._config.log_label,
                    exc_info=True,
                )
            if self._closed or (stop_event is not None and stop_event.is_set()):
                return
            if stop_event is None:
                await anyio.sleep(self._config.scan_interval_seconds)
                continue
            with anyio.move_on_after(self._config.scan_interval_seconds):
                await stop_event.wait()
            if stop_event.is_set():
                return

    async def scan_once(self) -> ConcordReaperScanResult:
        started_at = monotonic()
        now = _ensure_utc(self._clock())
        counts: dict[str, int] = {
            "scanned_contract_count": 0,
            "stale_observations_created": 0,
            "stale_observations_cleared": 0,
            "contracts_cancelled": 0,
            "contracts_deleted": 0,
            "token_keys_deleted": 0,
        }
        for entry in await self._maintenance._contract_entries():
            parsed = parse_concord_contract_key(entry.key)
            if parsed is None:
                continue
            counts["scanned_contract_count"] += 1
            contract_id, generation = parsed
            try:
                entry_counts = await self._scan_contract_entry(
                    entry,
                    contract_id=contract_id,
                    generation=generation,
                    now=now,
                )
            except ConcordConflict:
                logger.info(
                    "%s Concord reaper entry failed closed contract_key=%s "
                    "contract=%s generation=%s",
                    self._config.log_label,
                    entry.key,
                    contract_id,
                    generation,
                    exc_info=True,
                )
                continue
            for key, value in entry_counts.items():
                counts[key] += value
        counts["stale_observations_cleared"] += (
            await self._maintenance._clear_orphaned_stale_observations()
        )
        stale_observation_count = len(
            await self._maintenance._stale_observation_entries()
        )
        result = ConcordReaperScanResult(
            scanned_contract_count=counts["scanned_contract_count"],
            stale_observation_count=stale_observation_count,
            stale_observations_created=counts["stale_observations_created"],
            stale_observations_cleared=counts["stale_observations_cleared"],
            contracts_cancelled=counts["contracts_cancelled"],
            contracts_deleted=counts["contracts_deleted"],
            token_keys_deleted=counts["token_keys_deleted"],
        )
        logger.info(
            "%s Concord reaper scan completed scanned=%s cancelled=%s "
            "deleted=%s token_keys_deleted=%s stale_observations=%s "
            "stale_created=%s stale_cleared=%s elapsed_ms=%.1f",
            self._config.log_label,
            result.scanned_contract_count,
            result.contracts_cancelled,
            result.contracts_deleted,
            result.token_keys_deleted,
            result.stale_observation_count,
            result.stale_observations_created,
            result.stale_observations_cleared,
            (monotonic() - started_at) * 1000,
        )
        return result

    async def _scan_contract_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
    ) -> dict[str, int]:
        try:
            record = ContractRecord.model_validate(entry.value)
        except (TypeError, ValueError) as exc:
            return await self._scan_invalid_contract_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
                invalid_reason=str(exc),
            )
        if record.contract_id != contract_id or record.generation != generation:
            return await self._scan_invalid_contract_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
                invalid_reason="contract key and record identity differ",
            )
        handle = contract_handle(entry.key, record, entry.revision)
        if record.state == ContractState.CANCELLED:
            return await self._scan_cancelled_contract(handle, record, now=now)
        return await self._scan_open_contract(handle, now=now)

    async def _scan_open_contract(
        self,
        contract: ContractHandle,
        *,
        now: datetime,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        validity = await self._maintenance.validate_exact(contract)
        if _open_contract_validity_is_stale(validity):
            first_observed, created = await self._maintenance._observe_stale(
                contract_id=contract.contract_id,
                generation=contract.generation,
                status=validity.status,
                reason=validity.reason,
                contract_revision=contract.revision,
                now=now,
            )
            if created:
                counts["stale_observations_created"] += 1
            if (
                now - first_observed
            ).total_seconds() >= self._config.stale_grace_seconds:
                try:
                    cancelled = await self._maintenance.cancel_contract(
                        contract,
                        reason=CONCORD_REAPER_STALE_CONTRACT_REASON,
                        log_label=self._config.log_label,
                        now=now,
                    )
                except (ConcordConflict, ValueError):
                    logger.info(
                        "%s Concord reaper stale contract cancel conflict; "
                        "leaving for next scan contract=%s generation=%s "
                        "status=%s",
                        self._config.log_label,
                        contract.contract_id,
                        contract.generation,
                        validity.status.value,
                        exc_info=True,
                    )
                else:
                    if cancelled:
                        counts["contracts_cancelled"] += 1
                    if await self._maintenance._clear_stale_observation(
                        contract.contract_id,
                        contract.generation,
                    ):
                        counts["stale_observations_cleared"] += 1
            return counts

        if validity.status == ContractValidityStatus.UNAVAILABLE:
            return counts
        if await self._maintenance._clear_stale_observation(
            contract.contract_id,
            contract.generation,
        ):
            counts["stale_observations_cleared"] += 1
        return counts

    async def _scan_cancelled_contract(
        self,
        contract: ContractHandle,
        record: ContractRecord,
        *,
        now: datetime,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        if await self._maintenance._clear_stale_observation(
            contract.contract_id,
            contract.generation,
        ):
            counts["stale_observations_cleared"] += 1
        if record.cancelled_at is None or (
            now - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < self._config.cancelled_retention_seconds:
            return counts
        result = await self._maintenance.delete_cancelled_contract(
            contract,
            retention_seconds=self._config.cancelled_retention_seconds,
            log_label=self._config.log_label,
            now=now,
        )
        if result.deleted:
            counts["contracts_deleted"] += 1
            counts["token_keys_deleted"] += result.deleted_token_key_count
            if await self._maintenance._clear_stale_observation(
                contract.contract_id,
                contract.generation,
            ):
                counts["stale_observations_cleared"] += 1
        return counts

    async def _scan_invalid_contract_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
        invalid_reason: str,
    ) -> dict[str, int]:
        counts = _empty_reaper_counts()
        raw_state = str(entry.value.get("state", ContractState.OPEN.value))
        if raw_state == ContractState.CANCELLED.value:
            if await self._maintenance._clear_stale_observation(
                contract_id,
                generation,
            ):
                counts["stale_observations_cleared"] += 1
            cancelled_at = _datetime_from_raw_value(entry.value.get("cancelledAt"))
            if cancelled_at is None or (
                now - cancelled_at
            ).total_seconds() < self._config.cancelled_retention_seconds:
                return counts
            logger.info(
                "%s Concord maintenance retained malformed cancelled contract "
                "contract_key=%s contract=%s generation=%s revision=%s reason=%s",
                self._config.log_label,
                entry.key,
                contract_id,
                generation,
                entry.revision,
                invalid_reason,
            )
            return counts

        first_observed, created = await self._maintenance._observe_stale(
            contract_id=contract_id,
            generation=generation,
            status=ContractValidityStatus.INVALID_CONTRACT,
            reason=invalid_reason,
            contract_revision=entry.revision,
            now=now,
        )
        if created:
            counts["stale_observations_created"] += 1
        if (
            now - first_observed
        ).total_seconds() < self._config.stale_grace_seconds:
            return counts
        logger.info(
            "%s Concord reaper invalid contract cannot be cancelled safely; "
            "leaving for next scan contract=%s generation=%s",
            self._config.log_label,
            contract_id,
            generation,
        )
        return counts


ConcordTokenMaintenanceEntry = tuple[KvEntry, ParticipantTokenRecord]


def _open_contract_validity_is_stale(validity: ContractValidity) -> bool:
    if validity.status in STALE_OPEN_CONTRACT_STATUSES:
        return True
    return (
        validity.status == ContractValidityStatus.NOT_YET_FULFILLED
        and not validity.tokens
    )


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


def _parse_contract_entry(
    entry: KvEntry,
    key: str,
    pointer: ContractPointer,
) -> ContractRecord:
    if entry.key != key:
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            f"Concord KV entry key {entry.key!r} differs from requested key {key!r}",
            key=key,
            expected_pointer=pointer,
        )
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
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            f"Concord contract {key!r} changed identity",
            key=key,
            expected_pointer=pointer,
            observed_pointer=ContractPointer(
                contractId=record.contract_id,
                generation=record.generation,
            ),
        )
    return record


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


def _parse_stale_observation_entry(
    entry: KvEntry,
    *,
    key: str,
    contract_id: str,
    generation: int,
) -> ConcordStaleObservationRecord:
    pointer = ContractPointer(contractId=contract_id, generation=generation)
    if entry.key != key:
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            "Concord stale observation entry key differs from the requested key",
            key=key,
            expected_pointer=pointer,
        )
    try:
        record = ConcordStaleObservationRecord.model_validate(entry.value)
    except (TypeError, ValueError) as exc:
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_INVALID,
            f"Concord stale observation {key!r} is malformed: {exc}",
            key=key,
            expected_pointer=pointer,
        ) from exc
    if record.contract_id != contract_id or record.generation != generation:
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
            "Concord stale observation key and record identity differ",
            key=key,
            expected_pointer=pointer,
            observed_pointer=ContractPointer(
                contractId=record.contract_id,
                generation=record.generation,
            ),
        )
    return record


def _empty_reaper_counts() -> dict[str, int]:
    return {
        "stale_observations_created": 0,
        "stale_observations_cleared": 0,
        "contracts_cancelled": 0,
        "contracts_deleted": 0,
        "token_keys_deleted": 0,
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_log_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _datetime_from_raw_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not isinstance(value, str):
        return None
    source = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _actor_log_value(value: EndpointAddress | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


async def _concord_participant_token_entries(
    token_store: ConcordMaintenanceScanPort,
    *,
    contract_id: str,
    generation: int,
    terms_hash: str | None,
) -> tuple[ConcordTokenMaintenanceEntry, ...]:
    token_entries: list[ConcordTokenMaintenanceEntry] = []
    pointer = ContractPointer(contractId=contract_id, generation=generation)
    for entry in await token_store.items_exact(
        concord_contract_prefix(contract_id=contract_id, generation=generation)
    ):
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            continue
        parsed_contract_id, parsed_generation, participant = parsed
        if parsed_contract_id != contract_id or parsed_generation != generation:
            continue
        try:
            token = ParticipantTokenRecord.model_validate(entry.value)
        except (TypeError, ValueError) as exc:
            raise ConcordConflict(
                ConcordConflictCode.TOKEN_INVALID,
                f"Concord participant token {entry.key!r} is malformed: {exc}",
                key=entry.key,
                expected_pointer=pointer,
            ) from exc
        if (
            token.contract_id != contract_id
            or token.generation != generation
            or token.participant != participant
            or token.terms_hash != terms_hash
        ):
            raise ConcordConflict(
                ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
                "Concord participant token key and record identity differ",
                key=entry.key,
                expected_pointer=pointer,
            )
        token_entries.append((entry, token))
    return tuple(sorted(token_entries, key=lambda item: item[0].key))


async def _delete_concord_participant_tokens_until_empty(
    token_store: ConcordMaintenanceScanPort,
    *,
    contract_id: str,
    generation: int,
    terms_hash: str | None,
    initial_entries: tuple[ConcordTokenMaintenanceEntry, ...],
    log_label: str,
) -> int:
    entries = initial_entries
    deleted = 0
    for _attempt in range(_MAX_TOKEN_CLEANUP_SCAN_ATTEMPTS):
        for entry, _token in entries:
            try:
                marker_revision = await token_store.delete(
                    entry.key,
                    revision=entry.revision,
                )
            except ConcordConflict:
                continue
            if marker_revision is not None:
                deleted += 1
        entries = await _concord_participant_token_entries(
            token_store,
            contract_id=contract_id,
            generation=generation,
            terms_hash=terms_hash,
        )
        if not entries:
            return deleted
    logger.warning(
        "%s Concord maintenance token cleanup did not converge contract=%s "
        "generation=%s remaining_keys=%s attempts=%s",
        log_label,
        contract_id,
        generation,
        [entry.key for entry, _token in entries],
        _MAX_TOKEN_CLEANUP_SCAN_ATTEMPTS,
    )
    return deleted


def _token_log_summary(
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    participants: list[str] = []
    sessions: dict[str, str] = {}
    refresh_sequences: dict[str, int] = {}
    for _entry, token in token_entries:
        participant = str(token.participant)
        participants.append(participant)
        sessions[participant] = token.session_id
        refresh_sequences[participant] = token.refresh_seq
    return participants, sessions, refresh_sequences


def _log_concord_contract_deletion_audit(
    *,
    log_label: str,
    contract_key: str,
    record: ContractRecord,
    state_revision: int,
    validation_status: ContractValidityStatus,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
    deleted_token_key_count: int,
) -> None:
    token_participants, token_sessions, token_refresh_sequences = _token_log_summary(
        token_entries
    )
    logger.info(
        "%s Concord maintenance deleting cancelled contract contract_key=%s "
        "contract=%s generation=%s profile=%s state=%s participants=%s "
        "attached_participants=%s created_by=%s created_at=%s "
        "cancelled_by=%s cancelled_at=%s cancel_reason=%s cancel_revision=%s "
        "supersedes=%s terms_hash=%s validation_status=%s "
        "state_revision=%s token_participants=%s token_sessions=%s "
        "token_refresh_sequences=%s deleted_token_key_count=%s",
        log_label,
        contract_key,
        record.contract_id,
        record.generation,
        record.profile,
        record.state.value,
        [str(item) for item in record.participants],
        [str(item) for item in record.attached_participants],
        _actor_log_value(record.created_by),
        _datetime_log_value(record.created_at),
        _actor_log_value(record.cancelled_by),
        _datetime_log_value(record.cancelled_at),
        record.cancel_reason,
        record.cancel_revision,
        (
            record.supersedes.model_dump(by_alias=True, mode="json")
            if record.supersedes is not None
            else None
        ),
        record.terms_hash,
        validation_status.value,
        state_revision,
        token_participants,
        token_sessions,
        token_refresh_sequences,
        deleted_token_key_count,
    )


__all__ = [
    "CONCORD_MAINTENANCE_ACTOR",
    "CONCORD_REAPER_STALE_CONTRACT_REASON",
    "CONCORD_STALE_OBSERVATION_SCHEMA_ID",
    "DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS",
    "DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS",
    "DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS",
    "STALE_OPEN_CONTRACT_STATUSES",
    "ConcordMaintenance",
    "ConcordMaintenanceDeletionResult",
    "ConcordReaperConfig",
    "ConcordReaperScanResult",
    "ConcordReaperService",
    "ConcordStaleObservationRecord",
]
