from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Literal

import anyio
from pydantic import Field, field_serializer, field_validator

from deckr._authority_buckets import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
)
from deckr._concord._keys import (
    canonical_json_bytes,
    canonical_json_hash,
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
    CONCORD_CONTRACT_SCHEMA_ID,
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    ConcordConflict,
    ConcordConflictCode,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityReason,
    ContractValidityStatus,
    ParticipantHandle,
    ParticipantTokenRecord,
    TokenObservation,
)
from deckr._concord._models import (
    contract_handle as _contract_handle,
)
from deckr._concord._models import (
    participant_handle as _participant_handle,
)
from deckr._concord._models import (
    participant_handle_matches as _participant_handle_matches,
)
from deckr._concord._models import (
    require_text as _require_text,
)
from deckr._concord._ports import ConcordMaintenanceScanPort
from deckr._concord._store import (
    ConcordKvStore as _ConcordKvStore,
)
from deckr._concord._store import (
    concord_bucket_adapters,
)
from deckr._concord._validation import (
    ConcordObservationState,
    ConcordSessionAssertions,
    ContractObservation,
    contract_observation_from_entry,
    evaluate_contract_validity,
    token_observation_from_entry,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json
from deckr.substrates.nats_kv import (
    KvChange,
    KvEntry,
    NatsKvMaterializedBucket,
)

CONCORD_STALE_OBSERVATION_SCHEMA_ID = "dev.deckr.concord.stale-observation.v1"
DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS = 60.0
DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS = 15.0
DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS = 900
DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS = 3600
DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS = 60
CONCORD_MAINTENANCE_ACTOR = "concord:maintenance"
CONCORD_REAPER_STALE_CONTRACT_REASON = "concord_reaper_stale_contract"
CONCORD_REFRESH_UNAVAILABLE_CANCEL_REASON = "participant_token_refresh_unavailable"
CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON = (
    "concord_managed_lost_participant_token"
)
CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON = (
    "concord_agreement_lost_participant_token"
)
logger = logging.getLogger(__name__)


def _contract_lifecycle_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


def _contract_terminal_log_level(profile: str | None) -> int:
    del profile
    return logging.INFO


def _contract_pending_log_level(profile: str | None) -> int:
    if _is_chattery_contract_profile(profile):
        return logging.DEBUG
    return logging.INFO


def _token_refresh_log_level() -> int:
    return logging.DEBUG


def _contract_invalid_log_level(
    profile: str | None,
    status: ContractValidityStatus | None,
) -> int:
    if (
        status == ContractValidityStatus.MISSING_TOKEN
        and _is_chattery_contract_profile(profile)
    ):
        return logging.DEBUG
    return logging.WARNING


def _is_chattery_contract_profile(profile: str | None) -> bool:
    return profile is not None and profile.endswith(".service_use.v1")


class ConcordEventType(StrEnum):
    CONTRACT_PROPOSED = "contract_proposed"
    CONTRACT_UPDATED = "contract_updated"
    CONTRACT_VALID = "contract_valid"
    CONTRACT_PENDING = "contract_pending"
    CONTRACT_INVALID = "contract_invalid"
    CONTRACT_CANCELLED = "contract_cancelled"
    CONTRACT_DELETED = "contract_deleted"
    TOKEN_ATTACHED = "token_attached"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_WITHDRAWN = "token_withdrawn"


class ConcordManagedContractEventType(StrEnum):
    VALID = "valid"
    PENDING = "pending"
    INVALID = "invalid"
    CANCELLED = "cancelled"
    RELEASED = "released"


class _ConcordNotificationSource(StrEnum):
    CONTRACT = "contract"
    TOKEN = "token"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _concord_token_refresh_delay(
    *,
    requested: float,
    ttl_seconds: int | float,
) -> float:
    ttl = float(ttl_seconds)
    upper = ttl * 0.75
    lower = min(max(float(requested), ttl * 0.5), upper)
    return random.uniform(lower, upper)


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
        return _require_text(value, field_name="contract id")

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
        return _require_text(value, field_name="Concord stale observation reason")

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
        return _require_text(value, field_name="Concord reaper log label")


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


@dataclass(frozen=True, slots=True)
class ConcordEvent:
    event_type: ConcordEventType
    contract: ContractHandle | None = None
    record: ContractRecord | None = None
    validity: ContractValidity | None = None
    token: ParticipantHandle | None = None
    profile: str | None = None
    participant: EndpointAddress | None = None
    reason: str | None = None
    change: KvChange | None = None


@dataclass(frozen=True, slots=True)
class _ConcordContractNotification:
    source: _ConcordNotificationSource
    operation: Literal["put", "delete", "expire"]
    contract_id: str
    generation: int
    contract: ContractHandle | None = None
    participant: EndpointAddress | None = None
    profile: str | None = None
    change: KvChange | None = None


@dataclass(frozen=True, slots=True)
class ConcordManagedContract:
    contract: ContractHandle
    record: ContractRecord
    validity: ContractValidity
    token: ParticipantHandle | None = None


@dataclass(frozen=True, slots=True)
class ConcordManagedContractEvent:
    event_type: ConcordManagedContractEventType
    contract: ContractHandle
    record: ContractRecord | None = None
    validity: ContractValidity | None = None
    token: ParticipantHandle | None = None
    reason: str | None = None


class ConcordParticipantLease:
    """Owns one participant token and its heartbeat for a Concord contract."""

    def __init__(
        self,
        service: Concord,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        self._service = service
        self.contract = contract
        self.participant = parse_endpoint_address(participant)
        self.session_id = _require_text(session_id, field_name="Concord session id")
        self._token_id = token_id
        self._requested_refresh_interval = refresh_interval
        self._refresh_interval = refresh_interval
        self._log_label = log_label
        self._token: ParticipantHandle | None = None
        self._last_refresh_at: float | None = None
        self._lock = anyio.Lock()
        self._started = False
        self._closed = False

    @property
    def token(self) -> ParticipantHandle | None:
        return self._token

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object]) -> None:
        if self._started:
            return
        self._started = True
        start_soon(self.heartbeat_loop)

    async def aclose(self, *, withdraw: bool = True) -> None:
        async with self._lock:
            token = self._token
            self._closed = True
            self._token = None
            self._last_refresh_at = None
        if withdraw and token is not None:
            try:
                await self._service._withdraw_token(  # noqa: SLF001
                    token,
                    log_label=self._log_label,
                )
            except (ConcordConflict, ConcordUnavailable):
                logger.debug(
                    "%s Concord participant token cleanup failed contract=%s "
                    "generation=%s participant=%s session=%s token=%s",
                    self._log_label,
                    token.contract_id,
                    token.generation,
                    token.participant,
                    token.session_id,
                    token.token_id,
                    exc_info=True,
                )
        await self._service._forget_participant_lease(self)  # noqa: SLF001

    def adopt(self, token: ParticipantHandle) -> None:
        current = self._token
        if current is None:
            raise ValueError(
                "participant token cannot be adopted without an existing local handle"
            )
        if token.contract_id != self.contract.contract_id:
            raise ValueError("participant token belongs to a different contract")
        if token.generation != self.contract.generation:
            raise ValueError("participant token belongs to a different generation")
        if token.participant != self.participant:
            raise ValueError("participant token belongs to a different participant")
        if token.session_id != self.session_id:
            raise ValueError("participant token belongs to a different session")
        if not _participant_handle_matches(current, token):
            raise ValueError("participant token does not match local handle")
        if self._token == token:
            return
        self._refresh_interval = _concord_token_refresh_delay(
            requested=self._requested_refresh_interval,
            ttl_seconds=token.ttl_seconds,
        )
        self._token = token
        self._last_refresh_at = monotonic()

    async def attach_or_refresh(self) -> ParticipantHandle:
        async with self._lock:
            if self._closed:
                raise ConcordConflict(
                    ConcordConflictCode.LEASE_CLOSED,
                    "Concord participant lease is closed",
                )
            token = self._token
            if token is not None:
                try:
                    self.adopt(await self._service._validate_participant_token(token))
                    token = self._token
                    if token is None:
                        raise ConcordConflict(
                            ConcordConflictCode.TOKEN_MISSING,
                            "Concord participant token is missing",
                            key=token.key,
                        )
                    if not self._token_refresh_due():
                        return token
                    self._token = await self._service._refresh_token(
                        token,
                        log_label=self._log_label,
                    )
                    self._refresh_interval = _concord_token_refresh_delay(
                        requested=self._requested_refresh_interval,
                        ttl_seconds=self._token.ttl_seconds,
                    )
                    self._last_refresh_at = monotonic()
                    return self._token
                except ConcordConflict as exc:
                    self._token = None
                    self._last_refresh_at = None
                    if _is_terminal_participant_conflict(exc):
                        self._closed = True
                        logger.warning(
                            "%s Concord participant lease closed after token "
                            "refresh conflict contract=%s generation=%s "
                            "participant=%s session=%s reason=%s",
                            self._log_label,
                            self.contract.contract_id,
                            self.contract.generation,
                            self.participant,
                            self.session_id,
                            exc,
                        )
                    else:
                        logger.warning(
                            "%s Concord participant token refresh conflict; "
                            "authority lost contract=%s generation=%s participant=%s "
                            "session=%s",
                            self._log_label,
                            self.contract.contract_id,
                            self.contract.generation,
                            self.participant,
                            self.session_id,
                            exc_info=True,
                        )
                    raise
                except ConcordUnavailable:
                    self._token = None
                    self._last_refresh_at = None
                    self._closed = True
                    logger.warning(
                        "%s Concord participant token refresh unavailable; "
                        "authority lost contract=%s generation=%s participant=%s "
                        "session=%s",
                        self._log_label,
                        self.contract.contract_id,
                        self.contract.generation,
                        self.participant,
                        self.session_id,
                        exc_info=True,
                    )
                    try:
                        await self._service._cancel(  # noqa: SLF001
                            self.contract,
                            self.participant,
                            reason=CONCORD_REFRESH_UNAVAILABLE_CANCEL_REASON,
                            log_label=self._log_label,
                        )
                    except (ConcordConflict, ConcordUnavailable, ValueError):
                        logger.debug(
                            "%s could not cancel Concord contract after token "
                            "refresh became unavailable contract=%s generation=%s "
                            "participant=%s session=%s",
                            self._log_label,
                            self.contract.contract_id,
                            self.contract.generation,
                            self.participant,
                            self.session_id,
                            exc_info=True,
                        )
                    await self._service._forget_participant_lease(self)  # noqa: SLF001
                    raise
            try:
                self._token = await self._service._attach(
                    self.contract,
                    self.participant,
                    self.session_id,
                    token_id=self._token_id,
                    log_label=self._log_label,
                )
                self._refresh_interval = _concord_token_refresh_delay(
                    requested=self._requested_refresh_interval,
                    ttl_seconds=self._token.ttl_seconds,
                )
                self._last_refresh_at = monotonic()
            except ConcordConflict as exc:
                if _is_terminal_participant_conflict(exc):
                    self._closed = True
                    logger.warning(
                        "%s Concord participant lease closed after attach "
                        "conflict contract=%s generation=%s participant=%s "
                        "session=%s reason=%s",
                        self._log_label,
                        self.contract.contract_id,
                        self.contract.generation,
                        self.participant,
                        self.session_id,
                        exc,
                    )
                raise
            return self._token

    def _token_refresh_due(self) -> bool:
        return (
            self._last_refresh_at is None
            or monotonic() - self._last_refresh_at >= self._refresh_interval
        )

    def _next_heartbeat_delay(self) -> float:
        if self._last_refresh_at is None:
            return self._refresh_interval
        elapsed = monotonic() - self._last_refresh_at
        return max(0.0, self._refresh_interval - elapsed)

    async def heartbeat_loop(self) -> None:
        while not self._closed:
            await anyio.sleep(self._next_heartbeat_delay())
            if self._closed:
                return
            try:
                await self.attach_or_refresh()
            except ConcordConflict:
                if self._closed:
                    return
                logger.warning(
                    "%s Concord participant token conflict; heartbeat will retry "
                    "contract=%s generation=%s participant=%s session=%s",
                    self._log_label,
                    self.contract.contract_id,
                    self.contract.generation,
                    self.participant,
                    self.session_id,
                    exc_info=True,
                )
            except ConcordUnavailable:
                if self._closed:
                    return
                logger.warning(
                    "%s Concord participant token unavailable; heartbeat will retry "
                    "contract=%s generation=%s participant=%s session=%s",
                    self._log_label,
                    self.contract.contract_id,
                    self.contract.generation,
                    self.participant,
                    self.session_id,
                    exc_info=True,
                )


@dataclass(slots=True, eq=False)
class _ConcordSubscriber:
    send: anyio.abc.ObjectSendStream[ConcordEvent]
    profile: str | None
    participant: EndpointAddress | None
    replay_pending: bool = False
    pending_events: list[ConcordEvent] = field(default_factory=list)


class Concord:
    """Runtime-facing Concord API with participant leases and semantic events."""

    def __init__(
        self,
        contract_bucket: NatsKvMaterializedBucket | Any,
        token_bucket: NatsKvMaterializedBucket | Any,
        maintenance_bucket: NatsKvMaterializedBucket | Any,
        *,
        buffer_size: int = 100,
    ) -> None:
        self._coordinator = _ConcordKvStore(
            contract_bucket,
            token_bucket,
        )
        maintenance = concord_bucket_adapters(maintenance_bucket)
        self._maintenance_source = maintenance.source
        self._maintenance_scan = maintenance.scan
        self._buffer_size = buffer_size
        self._ready = anyio.Event()
        self._started = False
        self._closed = False
        self._task_group: anyio.abc.TaskGroup | None = None
        self._agreement_lock = anyio.Lock()
        self._lock = anyio.Lock()
        self._subscribers: set[_ConcordSubscriber] = set()
        self._participant_leases: set[ConcordParticipantLease] = set()
        self._contract_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_contracts_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._contract_records_by_key: dict[str, ContractRecord] = {}
        self._contract_handles_by_key: dict[str, ContractHandle] = {}
        self._contract_keys_by_pointer: dict[tuple[str, int], str] = {}
        self._contract_keys_by_contract_id: dict[str, set[str]] = {}
        self._contract_keys_by_profile: dict[str | None, set[str]] = {}
        self._contract_keys_by_participant: dict[str, set[str]] = {}
        self._contract_keys_by_state: dict[ContractState, set[str]] = {}
        self._contract_revision_by_key: dict[str, int] = {}
        self._token_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_tokens_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._token_records_by_key: dict[str, ParticipantTokenRecord] = {}
        self._tokens_by_key: dict[str, ParticipantHandle] = {}
        self._token_keys_by_contract: dict[tuple[str, int], set[str]] = {}
        self._token_key_by_contract_participant: dict[tuple[str, int, str], str] = {}
        self._token_revision_by_key: dict[str, int] = {}
        self._maintenance_entries_by_key: dict[str, KvEntry] = {}
        self._maintenance_records_by_key: dict[str, ConcordStaleObservationRecord] = {}
        self._maintenance_revision_by_key: dict[str, int] = {}
        self._contract_bucket_generation = 0
        self._token_bucket_generation = 0
        self._maintenance_bucket_generation = 0
        self._last_status_by_contract_key: dict[str, ContractValidityStatus] = {}

    @property
    def contract_bucket(self) -> str:
        return self._coordinator.contract_source.bucket

    @property
    def token_bucket(self) -> str:
        return self._coordinator.token_source.bucket

    @property
    def maintenance_bucket(self) -> str:
        return self._maintenance_source.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        self._coordinator.contract_source.start(task_group)
        self._coordinator.token_source.start(task_group)
        self._maintenance_source.start(task_group)
        if not self._started:
            self._started = True
            task_group.start_soon(self._event_loop)
        for lease in tuple(self._participant_leases):
            lease.start(task_group)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_current(self) -> bool:
        return (
            self._ready.is_set()
            and self._state_views_current()
            and self._state_cache_generations_current()
        )

    async def wait_current(self) -> None:
        await self.wait_ready()
        while True:
            await self._coordinator.contract_source.wait_current()
            await self._coordinator.token_source.wait_current()
            await self._maintenance_source.wait_current()
            if self._state_cache_generations_current():
                return
            await self._rebuild_from_buckets()
            await anyio.sleep(0)

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            leases = tuple(self._participant_leases)
            self._participant_leases.clear()
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for lease in leases:
            await lease.aclose()
        for subscriber in subscribers:
            await subscriber.send.aclose()

    async def propose(
        self,
        spec: ConcordAgreementSpec,
        *,
        start_soon: Callable[..., object] | None = None,
    ) -> ConcordAgreementLease:
        """Create an owner-side agreement with an opaque Concord contract id.

        This method is the production lifecycle entry point for a participant
        that owns the contract. Each call opens a fresh contract; replacement
        relationships are represented only by an explicit ``supersedes`` pointer.
        """

        if self._started:
            await self.wait_current()
        async with self._agreement_lock:
            return await self._ensure_agreement_locked(spec, start_soon=start_soon)

    async def _ensure_agreement_locked(
        self,
        spec: ConcordAgreementSpec,
        *,
        start_soon: Callable[..., object] | None = None,
    ) -> ConcordAgreementLease:
        while True:
            contract, validity = await self._select_or_create_agreement_contract(spec)
            agreement = self._agreement_from_contract(spec, contract, validity)
            if start_soon is not None:
                agreement._lease.start_soon(start_soon)  # noqa: SLF001
            validity = await agreement.refresh()
            if _agreement_successor_status(validity.status):
                if not agreement.closed:
                    await self._cancel_agreement(
                        agreement,
                        reason=f"concord_agreement_{validity.status.value}",
                    )
                continue
            return agreement

    def participant(
        self,
        *,
        participant: str | EndpointAddress,
        session_id: str,
        accept_contract: ConcordContractPredicate,
        current_sessions: ConcordCurrentSessions | None = None,
        prepare_reconcile: ConcordPrepareReconcile | None = None,
        contract_sort_key: ConcordContractSortKey | None = None,
        profile: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        reconcile_interval: float = DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS,
        cancel_terminal_statuses: Collection[ContractValidityStatus] | None = None,
        log_label: str = "Concord",
    ) -> ConcordParticipant:
        return ConcordParticipant(
            concord=self,
            participant=participant,
            session_id=session_id,
            accept_contract=accept_contract,
            current_sessions=current_sessions,
            prepare_reconcile=prepare_reconcile,
            contract_sort_key=contract_sort_key,
            profile=profile,
            refresh_interval=refresh_interval,
            reconcile_interval=reconcile_interval,
            cancel_terminal_statuses=cancel_terminal_statuses,
            log_label=log_label,
        )

    async def _event_loop(self) -> None:
        contract_bucket = self._coordinator.contract_source
        token_bucket = self._coordinator.token_source
        await contract_bucket.wait_current()
        await token_bucket.wait_current()
        await self._maintenance_source.wait_current()
        async with (
            contract_bucket.subscribe() as contract_changes,
            token_bucket.subscribe() as token_changes,
            self._maintenance_source.subscribe() as maintenance_changes,
            anyio.create_task_group() as task_group,
        ):
            await self._rebuild_from_buckets()
            self._ready.set()
            task_group.start_soon(self._consume_contract_changes, contract_changes)
            task_group.start_soon(self._consume_token_changes, token_changes)
            task_group.start_soon(
                self._consume_maintenance_changes,
                maintenance_changes,
            )

    async def _consume_contract_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_contract_change(change)

    async def _consume_token_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_token_change(change)

    async def _consume_maintenance_changes(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
    ) -> None:
        async for change in changes:
            await self._apply_maintenance_change(change)

    async def _rebuild_from_buckets(self, *, publish_events: bool = False) -> None:
        contract_entries = self._coordinator.contract_source.items_cached(
            concord_contracts_prefix()
        )
        token_entries = self._coordinator.token_source.items_cached(
            concord_contracts_prefix()
        )
        maintenance_entries = self._maintenance_source.items_cached("stale.")
        contract_generation = self._coordinator.contract_source.generation
        token_generation = self._coordinator.token_source.generation
        maintenance_generation = self._maintenance_source.generation
        async with self._lock:
            previous_contract_handles = dict(self._contract_handles_by_key)
            previous_contract_records = dict(self._contract_records_by_key)
            previous_tokens = dict(self._tokens_by_key)
            previous_statuses = dict(self._last_status_by_contract_key)
            self._clear_indexes_locked()
            for entry in contract_entries:
                self._index_contract_entry_locked(entry)
            for entry in token_entries:
                self._index_token_entry_locked(entry)
            for entry in maintenance_entries:
                self._index_maintenance_entry_locked(entry)
            events = (
                self._rebuild_events_locked(
                    previous_contract_handles=previous_contract_handles,
                    previous_contract_records=previous_contract_records,
                    previous_tokens=previous_tokens,
                    previous_statuses=previous_statuses,
                )
                if publish_events
                else ()
            )
            self._last_status_by_contract_key = {
                key: self._validate_from_cache_locked(handle).status
                for key, handle in self._contract_handles_by_key.items()
            }
            self._contract_bucket_generation = contract_generation
            self._token_bucket_generation = token_generation
            self._maintenance_bucket_generation = maintenance_generation
            deliveries = self._subscriber_deliveries_locked(events)
        if events:
            await self._publish_events(events, deliveries)

    def _rebuild_events_locked(
        self,
        *,
        previous_contract_handles: Mapping[str, ContractHandle],
        previous_contract_records: Mapping[str, ContractRecord],
        previous_tokens: Mapping[str, ParticipantHandle],
        previous_statuses: Mapping[str, ContractValidityStatus],
    ) -> tuple[ConcordEvent, ...]:
        events: list[ConcordEvent] = []
        status_emitted: set[str] = set()
        current_contract_keys = set(self._contract_handles_by_key)
        previous_contract_keys = set(previous_contract_handles)
        for key in sorted(current_contract_keys):
            handle = self._contract_handles_by_key[key]
            record = self._contract_records_by_key.get(key)
            previous = previous_contract_handles.get(key)
            previous_record = previous_contract_records.get(key)
            if previous is None or previous.revision != handle.revision:
                event_type = (
                    ConcordEventType.CONTRACT_PROPOSED
                    if previous is None
                    else ConcordEventType.CONTRACT_UPDATED
                )
                if record is not None and record.state == ContractState.CANCELLED:
                    event_type = ConcordEventType.CONTRACT_CANCELLED
                events.append(
                    ConcordEvent(
                        event_type,
                        contract=handle,
                        record=record,
                        profile=handle.profile,
                        reason=(
                            record.cancel_reason
                            if record is not None
                            and record.state == ContractState.CANCELLED
                            else None
                        ),
                        change=_rebuild_change(
                            self.contract_bucket,
                            key,
                            handle.revision,
                            "put",
                            self._contract_entries_by_key.get(key),
                        ),
                    )
                )
            elif record != previous_record:
                events.append(
                    ConcordEvent(
                        ConcordEventType.CONTRACT_UPDATED,
                        contract=handle,
                        record=record,
                        profile=handle.profile,
                        change=_rebuild_change(
                            self.contract_bucket,
                            key,
                            handle.revision,
                            "put",
                            self._contract_entries_by_key.get(key),
                        ),
                    )
                )
            validity = self._validate_from_cache_locked(handle)
            if previous_statuses.get(key) != validity.status:
                events.append(
                    ConcordEvent(
                        _concord_event_type(validity),
                        contract=handle,
                        record=validity.contract,
                        validity=validity,
                        profile=handle.profile,
                        reason=validity.reason,
                    )
                )
                status_emitted.add(key)
        for key in sorted(previous_contract_keys - current_contract_keys):
            previous = previous_contract_handles[key]
            events.append(
                ConcordEvent(
                    ConcordEventType.CONTRACT_DELETED,
                    contract=previous,
                    record=previous_contract_records.get(key),
                    profile=previous.profile,
                    reason="rebuild",
                    change=_rebuild_change(
                        self.contract_bucket,
                        key,
                        previous.revision + 1,
                        "delete",
                        None,
                    ),
                )
            )

        current_token_keys = set(self._tokens_by_key)
        previous_token_keys = set(previous_tokens)
        for key in sorted(current_token_keys):
            token = self._tokens_by_key[key]
            previous = previous_tokens.get(key)
            if previous is not None and previous.revision == token.revision:
                continue
            contract_key = self._contract_keys_by_pointer.get(
                (token.contract_id, token.generation)
            )
            contract = (
                self._contract_handles_by_key.get(contract_key)
                if contract_key is not None
                else None
            )
            events.append(
                ConcordEvent(
                    ConcordEventType.TOKEN_ATTACHED
                    if previous is None
                    else ConcordEventType.TOKEN_REFRESHED,
                    contract=contract,
                    token=token,
                    profile=contract.profile if contract is not None else None,
                    participant=token.participant,
                    change=_rebuild_change(
                        self.token_bucket,
                        key,
                        token.revision,
                        "put",
                        self._token_entries_by_key.get(key),
                    ),
                )
            )
            if contract is not None and contract.key not in status_emitted:
                validity = self._validate_from_cache_locked(contract)
                if previous_statuses.get(contract.key) != validity.status:
                    events.append(
                        ConcordEvent(
                            _concord_event_type(validity),
                            contract=contract,
                            record=validity.contract,
                            validity=validity,
                            profile=contract.profile,
                            reason=validity.reason,
                        )
                    )
                    status_emitted.add(contract.key)
        for key in sorted(previous_token_keys - current_token_keys):
            previous = previous_tokens[key]
            contract_key = self._contract_keys_by_pointer.get(
                (previous.contract_id, previous.generation)
            )
            contract = (
                self._contract_handles_by_key.get(contract_key)
                if contract_key is not None
                else previous_contract_handles.get(
                    concord_contract_key(
                        contract_id=previous.contract_id,
                        generation=previous.generation,
                    )
                )
            )
            events.append(
                ConcordEvent(
                    ConcordEventType.TOKEN_WITHDRAWN,
                    contract=contract,
                    token=previous,
                    profile=contract.profile if contract is not None else None,
                    participant=previous.participant,
                    reason="rebuild",
                    change=_rebuild_change(
                        self.token_bucket,
                        key,
                        previous.revision + 1,
                        "delete",
                        None,
                    ),
                )
            )
            if contract is not None and contract.key not in status_emitted:
                current = self._contract_handles_by_key.get(contract.key)
                if current is None:
                    continue
                validity = self._validate_from_cache_locked(current)
                if previous_statuses.get(current.key) != validity.status:
                    events.append(
                        ConcordEvent(
                            _concord_event_type(validity),
                            contract=current,
                            record=validity.contract,
                            validity=validity,
                            profile=current.profile,
                            reason=validity.reason,
                        )
                    )
                    status_emitted.add(current.key)
        return tuple(events)

    def _state_views_current(self) -> bool:
        return (
            self._coordinator.contract_source.is_current()
            and self._coordinator.token_source.is_current()
            and self._maintenance_source.is_current()
        )

    def _state_cache_generations_current(self) -> bool:
        return (
            self._contract_bucket_generation
            == self._coordinator.contract_source.generation
            and self._token_bucket_generation
            == self._coordinator.token_source.generation
            and self._maintenance_bucket_generation == self._maintenance_source.generation
        )

    def _clear_indexes_locked(self) -> None:
        self._contract_entries_by_key.clear()
        self._invalid_contracts_by_key.clear()
        self._contract_records_by_key.clear()
        self._contract_handles_by_key.clear()
        self._contract_keys_by_pointer.clear()
        self._contract_keys_by_contract_id.clear()
        self._contract_keys_by_profile.clear()
        self._contract_keys_by_participant.clear()
        self._contract_keys_by_state.clear()
        self._contract_revision_by_key.clear()
        self._token_entries_by_key.clear()
        self._invalid_tokens_by_key.clear()
        self._token_records_by_key.clear()
        self._tokens_by_key.clear()
        self._token_keys_by_contract.clear()
        self._token_key_by_contract_participant.clear()
        self._token_revision_by_key.clear()
        self._maintenance_entries_by_key.clear()
        self._maintenance_records_by_key.clear()
        self._maintenance_revision_by_key.clear()

    async def _apply_contract_change(self, change: KvChange) -> None:
        if change.operation == "put" and change.entry is None:
            entry = self._coordinator.contract_source.get_cached(change.key)
            if entry is None or entry.revision < change.revision:
                await self._rebuild_from_buckets(publish_events=True)
                return
            change = KvChange(
                change.bucket,
                change.key,
                entry.revision,
                change.operation,
                entry,
                change.marker_reason,
                change.view_generation,
            )
        if await self._rebuild_if_contract_generation_gap(change):
            return
        async with self._lock:
            events = self._apply_contract_change_locked(change)
            deliveries = self._subscriber_deliveries_locked(events)
        await self._publish_events(events, deliveries)

    async def _apply_token_change(self, change: KvChange) -> None:
        if await self._rebuild_if_token_generation_gap(change):
            return
        async with self._lock:
            events = self._apply_token_change_locked(change)
            deliveries = self._subscriber_deliveries_locked(events)
        await self._publish_events(events, deliveries)

    async def _apply_maintenance_change(self, change: KvChange) -> None:
        if await self._rebuild_if_maintenance_generation_gap(change):
            return
        async with self._lock:
            self._apply_maintenance_change_locked(change)

    async def _rebuild_if_contract_generation_gap(self, change: KvChange) -> bool:
        if change.view_generation is None:
            return False
        async with self._lock:
            gap = change.view_generation > self._contract_bucket_generation + 1
        if not gap:
            return False
        await self._rebuild_from_buckets(publish_events=True)
        return True

    async def _rebuild_if_token_generation_gap(self, change: KvChange) -> bool:
        if change.view_generation is None:
            return False
        async with self._lock:
            gap = change.view_generation > self._token_bucket_generation + 1
        if not gap:
            return False
        await self._rebuild_from_buckets(publish_events=True)
        return True

    async def _rebuild_if_maintenance_generation_gap(self, change: KvChange) -> bool:
        if change.view_generation is None:
            return False
        async with self._lock:
            gap = change.view_generation > self._maintenance_bucket_generation + 1
        if not gap:
            return False
        await self._rebuild_from_buckets(publish_events=True)
        return True

    def _apply_contract_change_locked(self, change: KvChange) -> tuple[ConcordEvent, ...]:
        if not self._should_apply_contract_change_locked(change):
            return ()
        current_revision = self._contract_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            self._advance_contract_bucket_generation_locked(change)
            return ()
        previous = self._contract_handles_by_key.get(change.key)
        previous_record = self._contract_records_by_key.get(change.key)
        self._remove_contract_key_locked(change.key)
        self._contract_revision_by_key[change.key] = change.revision
        events: list[ConcordEvent] = []
        if change.operation == "put" and change.entry is not None:
            handle = self._index_contract_entry_locked(change.entry)
            record = self._contract_records_by_key.get(change.key)
            if handle is None or record is None:
                if change.key not in self._invalid_contracts_by_key:
                    self._advance_contract_bucket_generation_locked(change)
                    return ()
                parsed = parse_concord_contract_key(change.key)
                contract_id, generation = parsed if parsed is not None else ("", 0)
                events.append(
                    ConcordEvent(
                        ConcordEventType.CONTRACT_INVALID,
                        reason=self._invalid_contracts_by_key[change.key][1],
                        change=change,
                    )
                )
                if contract_id and generation:
                    self._last_status_by_contract_key.pop(change.key, None)
                self._advance_contract_bucket_generation_locked(change)
                return tuple(events)
            event_type = (
                ConcordEventType.CONTRACT_PROPOSED
                if previous is None
                else ConcordEventType.CONTRACT_UPDATED
            )
            if record.state == ContractState.CANCELLED:
                event_type = ConcordEventType.CONTRACT_CANCELLED
            events.append(
                ConcordEvent(
                    event_type,
                    contract=handle,
                    record=record,
                    profile=handle.profile,
                    reason=record.cancel_reason if record.state == ContractState.CANCELLED else None,
                    change=change,
                )
            )
            status_event = self._status_event_locked(handle, change=change)
            if status_event is not None:
                events.append(status_event)
            self._advance_contract_bucket_generation_locked(change)
            return tuple(events)
        if change.operation in {"delete", "expire"}:
            if previous is None and parse_concord_contract_key(change.key) is None:
                self._advance_contract_bucket_generation_locked(change)
                return ()
            self._last_status_by_contract_key.pop(change.key, None)
            events.append(
                ConcordEvent(
                    ConcordEventType.CONTRACT_DELETED,
                    contract=previous,
                    record=previous_record,
                    profile=previous.profile if previous is not None else None,
                    reason=change.operation,
                    change=change,
                )
            )
        self._advance_contract_bucket_generation_locked(change)
        return tuple(events)

    def _apply_token_change_locked(self, change: KvChange) -> tuple[ConcordEvent, ...]:
        if not self._should_apply_token_change_locked(change):
            return ()
        current_revision = self._token_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            self._advance_token_bucket_generation_locked(change)
            return ()
        previous = self._tokens_by_key.get(change.key)
        parsed = parse_concord_participant_token_key(change.key)
        contract_key: str | None = None
        contract: ContractHandle | None = None
        if parsed is not None:
            contract_id, generation, participant = parsed
            contract_key = self._contract_keys_by_pointer.get((contract_id, generation))
            if contract_key is not None:
                contract = self._contract_handles_by_key.get(contract_key)
        else:
            participant = None
        self._remove_token_key_locked(change.key)
        self._token_revision_by_key[change.key] = change.revision
        events: list[ConcordEvent] = []
        if change.operation == "put" and change.entry is not None:
            token = self._index_token_entry_locked(change.entry)
            if token is None:
                events.append(
                    ConcordEvent(
                        ConcordEventType.CONTRACT_INVALID,
                        contract=contract,
                        profile=contract.profile if contract is not None else None,
                        reason=self._invalid_tokens_by_key[change.key][1],
                        change=change,
                    )
                )
            else:
                contract_key = self._contract_keys_by_pointer.get(
                    (token.contract_id, token.generation)
                )
                contract = (
                    self._contract_handles_by_key.get(contract_key)
                    if contract_key is not None
                    else None
                )
                events.append(
                    ConcordEvent(
                        ConcordEventType.TOKEN_ATTACHED
                        if previous is None
                        else ConcordEventType.TOKEN_REFRESHED,
                        contract=contract,
                        token=token,
                        profile=contract.profile if contract is not None else None,
                        participant=token.participant,
                        change=change,
                    )
                )
        elif change.operation in {"delete", "expire"}:
            events.append(
                ConcordEvent(
                    ConcordEventType.TOKEN_EXPIRED
                    if change.operation == "expire"
                    else ConcordEventType.TOKEN_WITHDRAWN,
                    contract=contract,
                    token=previous,
                    profile=contract.profile if contract is not None else None,
                    participant=(
                        previous.participant
                        if previous is not None
                        else participant
                    ),
                    reason=change.operation,
                    change=change,
                )
            )
        if contract is not None:
            status_event = self._status_event_locked(contract, change=change)
            if status_event is not None:
                events.append(status_event)
        self._advance_token_bucket_generation_locked(change)
        return tuple(events)

    def _apply_maintenance_change_locked(self, change: KvChange) -> None:
        if not self._should_apply_maintenance_change_locked(change):
            return
        current_revision = self._maintenance_revision_by_key.get(change.key, 0)
        if change.revision <= current_revision:
            self._advance_maintenance_bucket_generation_locked(change)
            return
        self._maintenance_entries_by_key.pop(change.key, None)
        self._maintenance_records_by_key.pop(change.key, None)
        self._maintenance_revision_by_key[change.key] = change.revision
        if change.operation == "put" and change.entry is not None:
            self._index_maintenance_entry_locked(change.entry)
        self._advance_maintenance_bucket_generation_locked(change)

    def _should_apply_contract_change_locked(self, change: KvChange) -> bool:
        return _change_is_next_generation(
            change,
            current_generation=self._contract_bucket_generation,
        )

    def _should_apply_token_change_locked(self, change: KvChange) -> bool:
        return _change_is_next_generation(
            change,
            current_generation=self._token_bucket_generation,
        )

    def _should_apply_maintenance_change_locked(self, change: KvChange) -> bool:
        return _change_is_next_generation(
            change,
            current_generation=self._maintenance_bucket_generation,
        )

    def _advance_contract_bucket_generation_locked(self, change: KvChange) -> None:
        generation = _change_generation(
            change,
            self._coordinator.contract_source,
        )
        self._contract_bucket_generation = max(
            self._contract_bucket_generation,
            generation,
        )

    def _advance_token_bucket_generation_locked(self, change: KvChange) -> None:
        generation = _change_generation(
            change,
            self._coordinator.token_source,
        )
        self._token_bucket_generation = max(self._token_bucket_generation, generation)

    def _advance_maintenance_bucket_generation_locked(self, change: KvChange) -> None:
        generation = _change_generation(change, self._maintenance_source)
        self._maintenance_bucket_generation = max(
            self._maintenance_bucket_generation,
            generation,
        )

    def _index_contract_entry_locked(self, entry: KvEntry) -> ContractHandle | None:
        parsed = parse_concord_contract_key(entry.key)
        if parsed is None:
            return None
        self._contract_entries_by_key[entry.key] = entry
        self._contract_revision_by_key[entry.key] = entry.revision
        contract_id, generation = parsed
        try:
            record = ContractRecord.model_validate(entry.value)
        except ValueError as exc:
            self._invalid_contracts_by_key[entry.key] = (entry, str(exc))
            return None
        if record.contract_id != contract_id or record.generation != generation:
            self._invalid_contracts_by_key[entry.key] = (
                entry,
                "contract key and record identity differ",
            )
            return None
        handle = _contract_handle(entry.key, record, entry.revision)
        self._contract_records_by_key[entry.key] = record
        self._contract_handles_by_key[entry.key] = handle
        self._contract_keys_by_pointer[(record.contract_id, record.generation)] = entry.key
        self._contract_keys_by_contract_id.setdefault(record.contract_id, set()).add(
            entry.key
        )
        self._contract_keys_by_profile.setdefault(record.profile, set()).add(entry.key)
        self._contract_keys_by_state.setdefault(record.state, set()).add(entry.key)
        for participant in record.participants:
            self._contract_keys_by_participant.setdefault(str(participant), set()).add(
                entry.key
            )
        return handle

    def _remove_contract_key_locked(self, key: str) -> None:
        record = self._contract_records_by_key.pop(key, None)
        self._contract_entries_by_key.pop(key, None)
        self._invalid_contracts_by_key.pop(key, None)
        self._contract_handles_by_key.pop(key, None)
        if record is None:
            return
        self._contract_keys_by_pointer.pop((record.contract_id, record.generation), None)
        _discard_index_key(self._contract_keys_by_contract_id, record.contract_id, key)
        _discard_index_key(self._contract_keys_by_profile, record.profile, key)
        _discard_index_key(self._contract_keys_by_state, record.state, key)
        for participant in record.participants:
            _discard_index_key(
                self._contract_keys_by_participant,
                str(participant),
                key,
            )

    def _index_token_entry_locked(self, entry: KvEntry) -> ParticipantHandle | None:
        self._token_entries_by_key[entry.key] = entry
        self._token_revision_by_key[entry.key] = entry.revision
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            self._invalid_tokens_by_key[entry.key] = (
                entry,
                "token key is not a Concord participant token key",
            )
            return None
        contract_id, generation, participant = parsed
        try:
            record = ParticipantTokenRecord.model_validate(entry.value)
        except ValueError as exc:
            self._invalid_tokens_by_key[entry.key] = (entry, str(exc))
            return None
        if (
            record.contract_id != contract_id
            or record.generation != generation
            or record.participant != participant
        ):
            self._invalid_tokens_by_key[entry.key] = (
                entry,
                "token key and record identity differ",
            )
            return None
        token = _participant_handle(entry.key, record, entry.revision)
        self._token_records_by_key[entry.key] = record
        self._tokens_by_key[entry.key] = token
        self._token_keys_by_contract.setdefault((contract_id, generation), set()).add(
            entry.key
        )
        self._token_key_by_contract_participant[
            (contract_id, generation, str(participant))
        ] = entry.key
        return token

    def _remove_token_key_locked(self, key: str) -> None:
        record = self._token_records_by_key.pop(key, None)
        self._token_entries_by_key.pop(key, None)
        self._invalid_tokens_by_key.pop(key, None)
        self._tokens_by_key.pop(key, None)
        if record is None:
            return
        _discard_index_key(
            self._token_keys_by_contract,
            (record.contract_id, record.generation),
            key,
        )
        self._token_key_by_contract_participant.pop(
            (record.contract_id, record.generation, str(record.participant)),
            None,
        )

    def _index_maintenance_entry_locked(self, entry: KvEntry) -> None:
        self._maintenance_entries_by_key[entry.key] = entry
        self._maintenance_revision_by_key[entry.key] = entry.revision
        try:
            record = ConcordStaleObservationRecord.model_validate(entry.value)
        except ValueError:
            return
        self._maintenance_records_by_key[entry.key] = record

    def _status_event_locked(
        self,
        contract: ContractHandle,
        *,
        change: KvChange | None,
    ) -> ConcordEvent | None:
        validity = self._validate_from_cache_locked(contract)
        previous = self._last_status_by_contract_key.get(contract.key)
        self._last_status_by_contract_key[contract.key] = validity.status
        if previous == validity.status:
            return None
        return ConcordEvent(
            _concord_event_type(validity),
            contract=contract,
            record=validity.contract,
            validity=validity,
            profile=contract.profile,
            reason=validity.reason,
            change=change,
        )

    def _subscriber_deliveries_locked(
        self,
        events: tuple[ConcordEvent, ...],
    ) -> tuple[tuple[_ConcordSubscriber, ConcordEvent], ...]:
        deliveries: list[tuple[_ConcordSubscriber, ConcordEvent]] = []
        for event in events:
            for subscriber in tuple(self._subscribers):
                if not _event_matches_subscriber(subscriber, event):
                    continue
                if subscriber.replay_pending:
                    subscriber.pending_events.append(event)
                    continue
                deliveries.append((subscriber, event))
        return tuple(deliveries)

    async def _publish_events(
        self,
        events: tuple[ConcordEvent, ...],
        deliveries: tuple[tuple[_ConcordSubscriber, ConcordEvent], ...],
    ) -> None:
        for event in events:
            _log_concord_event(event)
        for subscriber, event in deliveries:
            try:
                subscriber.send.send_nowait(event)
            except anyio.WouldBlock:
                logger.warning(
                    "Concord watcher buffer full profile=%s event=%s",
                    subscriber.profile,
                    event.event_type.value,
                )
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.discard(subscriber)

    def _validate_from_cache_locked(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
    ) -> ContractValidity:
        pointer = ContractPointer(
            contractId=contract.contract_id,
            generation=contract.generation,
        )
        key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        observation = contract_observation_from_entry(
            key,
            self._contract_entries_by_key.get(key),
        )
        participants = (
            observation.record.participants
            if observation.record is not None
            else contract.participants
        )
        token_observations = tuple(
            token_observation_from_entry(
                token_key,
                participant,
                self._token_entries_by_key.get(token_key),
            )
            for participant in sorted(participants, key=str)
            for token_key in (
                concord_participant_token_key(
                    contract_id=pointer.contract_id,
                    generation=pointer.generation,
                    participant=participant,
                ),
            )
        )
        sources_current = (
            self._coordinator.contract_source.is_current()
            and self._coordinator.token_source.is_current()
            and self._contract_bucket_generation
            == self._coordinator.contract_source.generation
            and self._token_bucket_generation == self._coordinator.token_source.generation
        )
        if self._started and not sources_current:
            observation = ContractObservation(
                observation.key,
                observation.revision,
                ConcordObservationState.UNAVAILABLE,
                record=observation.record,
                diagnostic="Concord materialized contract or token source is stale",
            )
        return evaluate_contract_validity(
            expected_key=contract.key,
            expected_pointer=pointer,
            contract=observation,
            tokens=token_observations,
            session_assertions=(
                current_sessions
                if isinstance(current_sessions, ConcordSessionAssertions)
                else ConcordSessionAssertions.from_mapping(current_sessions)
            ),
        )

    async def _select_or_create_agreement_contract(
        self,
        spec: ConcordAgreementSpec,
    ) -> tuple[ContractHandle, ContractValidity]:
        current_sessions = await _agreement_current_sessions(spec)
        contract = await self._create_contract(
            spec.participants,
            generation=1,
            profile=spec.profile,
            terms=spec.terms,
            created_by=spec.created_by,
            supersedes=spec.supersedes,
            log_label=spec.log_label,
        )
        validity = await self._validate(
            contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
            log_invalid=False,
        )
        return contract, validity

    def _agreement_from_contract(
        self,
        spec: ConcordAgreementSpec,
        contract: ContractHandle,
        validity: ContractValidity,
    ) -> ConcordAgreementLease:
        lease = self._participant_lease(
            contract=contract,
            participant=spec.local_participant,
            session_id=spec.local_session_id,
            refresh_interval=spec.refresh_interval,
            log_label=spec.log_label,
        )
        return ConcordAgreementLease(
            self,
            spec=spec,
            contract=contract,
            lease=lease,
            validity=validity,
        )

    async def _confirm_agreement_terminal_validity(
        self,
        agreement: ConcordAgreementLease,
        validity: ContractValidity,
        *,
        current_sessions: Mapping[str, str],
    ) -> ContractValidity:
        if not _agreement_successor_status(validity.status):
            return validity
        exact = await self._coordinator.validate_exact(
            agreement.contract,
            current_sessions=current_sessions,
        )
        agreement._validity = exact  # noqa: SLF001
        return exact

    async def _refresh_agreement(
        self,
        agreement: ConcordAgreementLease,
    ) -> ContractValidity:
        if agreement.closed:
            raise ConcordConflict(
                ConcordConflictCode.AGREEMENT_CLOSED,
                "Concord agreement is closed",
                key=agreement.contract.key,
            )
        spec = agreement.spec
        current_sessions = await _agreement_current_sessions(spec)
        validity = await self._validate(
            agreement.contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
        )
        agreement._validity = validity  # noqa: SLF001
        validity = await self._confirm_agreement_terminal_validity(
            agreement,
            validity,
            current_sessions=current_sessions,
        )
        if validity.status == ContractValidityStatus.UNAVAILABLE:
            return validity
        if _agreement_successor_status(validity.status):
            await agreement._lease.aclose()  # noqa: SLF001
            return validity
        existing = validity.tokens.get(str(spec.local_participant))
        if existing is not None:
            if existing.session_id != spec.local_session_id:
                validity = ContractValidity(
                    ContractValidityStatus.SESSION_MISMATCH,
                    contract=validity.contract,
                    tokens=validity.tokens,
                    reason=str(spec.local_participant),
                    reason_code=ContractValidityReason.TOKEN_SESSION_MISMATCH,
                )
                agreement._validity = validity  # noqa: SLF001
                await agreement._lease.aclose()  # noqa: SLF001
                return validity
            if agreement.local_token is None:
                await self._cancel_lost_agreement_authority(agreement)
                return agreement.validity
            try:
                agreement._lease.adopt(existing)  # noqa: SLF001
            except ValueError:
                await self._cancel_lost_agreement_authority(agreement)
                return agreement.validity
        try:
            await agreement._lease.attach_or_refresh()  # noqa: SLF001
        except ConcordConflict as exc:
            validity = await self._validate(
                agreement.contract,
                current_sessions=current_sessions,
                log_label=spec.log_label,
            )
            agreement._validity = validity  # noqa: SLF001
            validity = await self._confirm_agreement_terminal_validity(
                agreement,
                validity,
                current_sessions=current_sessions,
            )
            logger.warning(
                "%s Concord agreement refresh failed contract=%s generation=%s "
                "participant=%s session=%s status=%s reason=%s conflict=%s",
                spec.log_label,
                agreement.contract.contract_id,
                agreement.contract.generation,
                spec.local_participant,
                spec.local_session_id,
                validity.status.value,
                validity.reason,
                exc,
            )
            if _agreement_successor_status(validity.status):
                await agreement._lease.aclose()  # noqa: SLF001
            raise
        validity = await self._validate(
            agreement.contract,
            current_sessions=current_sessions,
            log_label=spec.log_label,
        )
        agreement._validity = validity  # noqa: SLF001
        validity = await self._confirm_agreement_terminal_validity(
            agreement,
            validity,
            current_sessions=current_sessions,
        )
        if _agreement_successor_status(validity.status):
            await agreement._lease.aclose()  # noqa: SLF001
        return validity

    async def _cancel_agreement(
        self,
        agreement: ConcordAgreementLease,
        *,
        reason: str | None,
    ) -> bool:
        cancelled = await self._cancel(
            agreement.contract,
            agreement.spec.local_participant,
            reason=reason,
            log_label=agreement.spec.log_label,
        )
        await agreement.aclose()
        validity = await self._validate(
            agreement.contract,
            current_sessions=await _agreement_current_sessions(agreement.spec),
            log_label=agreement.spec.log_label,
            log_invalid=False,
        )
        agreement._validity = validity  # noqa: SLF001
        return cancelled

    async def _cancel_lost_agreement_authority(
        self,
        agreement: ConcordAgreementLease,
    ) -> ContractValidity:
        await agreement.aclose()
        try:
            await self._cancel(
                agreement.contract,
                agreement.spec.local_participant,
                reason=CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON,
                log_label=agreement.spec.log_label,
            )
            validity = await self._validate(
                agreement.contract,
                current_sessions=await _agreement_current_sessions(agreement.spec),
                log_label=agreement.spec.log_label,
                log_invalid=False,
            )
        except (ConcordConflict, ConcordUnavailable, ValueError):
            logger.debug(
                "%s could not cancel Concord agreement after local participant "
                "token authority was lost contract=%s generation=%s participant=%s "
                "session=%s",
                agreement.spec.log_label,
                agreement.contract.contract_id,
                agreement.contract.generation,
                agreement.spec.local_participant,
                agreement.spec.local_session_id,
                exc_info=True,
            )
            validity = _lost_agreement_authority_validity(agreement)
        if validity.status == ContractValidityStatus.UNAVAILABLE:
            validity = _lost_agreement_authority_validity(agreement)
        agreement._validity = validity  # noqa: SLF001
        return validity

    async def _create_contract(
        self,
        participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress],
        *,
        contract_id: str | None = None,
        generation: int = 1,
        profile: str | None = None,
        terms: Mapping[str, Any] | DeckrModel | None = None,
        created_by: str | EndpointAddress | None = None,
        supersedes: ContractPointer | Mapping[str, Any] | None = None,
        log_label: str = "Concord",
    ) -> ContractHandle:
        contract = await self._coordinator.create_contract(
            participants,
            contract_id=contract_id,
            generation=generation,
            profile=profile,
            terms=terms,
            created_by=created_by,
            supersedes=supersedes,
        )
        entry = self._coordinator.contract_source.get_cached(contract.key)
        if entry is not None:
            await self._apply_contract_change(
                KvChange(
                    self.contract_bucket,
                    contract.key,
                    entry.revision,
                    "put",
                    entry,
                )
            )
        logger.log(
            _contract_lifecycle_log_level(contract.profile),
            "%s Concord contract opened profile=%s contract=%s generation=%s "
            "participants=%s revision=%s created_by=%s",
            log_label,
            contract.profile,
            contract.contract_id,
            contract.generation,
            [str(item) for item in contract.participants],
            contract.revision,
            created_by,
        )
        return contract

    async def get_contract(
        self,
        pointer: ContractPointer | Mapping[str, Any],
    ) -> ContractHandle | None:
        if self._started:
            await self.wait_current()
        parsed = (
            pointer
            if isinstance(pointer, ContractPointer)
            else ContractPointer.model_validate(pointer)
        )
        async with self._lock:
            key = self._contract_keys_by_pointer.get(
                (parsed.contract_id, parsed.generation)
            )
            return (
                self._contract_handles_by_key.get(key)
                if key is not None
                else None
            )

    async def contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        if self._started:
            await self.wait_current()
        return await self._contract_record(contract)

    async def contracts(
        self,
        profile: str | None = None,
        *,
        contract_id: str | None = None,
        participant: str | EndpointAddress | None = None,
        state: ContractState | None = None,
    ) -> tuple[ContractHandle, ...]:
        if self._started:
            await self.wait_current()
        return await self._contracts_filtered(
            profile,
            contract_id=contract_id,
            participant=participant,
            state=state,
        )

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        reason: str | None = None,
        log_label: str = "Concord",
    ) -> bool:
        if self._started:
            await self.wait_ready()
        return await self._cancel(
            contract,
            participant,
            reason=reason,
            log_label=log_label,
        )

    async def attach(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> ConcordParticipantLease:
        if self._started:
            await self.wait_ready()
        lease = self._participant_lease(
            contract=contract,
            participant=participant,
            session_id=session_id,
            token_id=token_id,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )
        await lease.attach_or_refresh()
        async with self._lock:
            self._participant_leases.add(lease)
        if self._task_group is not None:
            lease.start(self._task_group)
        return lease

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        assertions = ConcordSessionAssertions.from_mapping(current_sessions)
        if self._started:
            await self.wait_ready()
        return await self._validate(contract, current_sessions=assertions)

    async def validate_exact(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        assertions = ConcordSessionAssertions.from_mapping(current_sessions)
        return await self._coordinator.validate_exact(
            contract,
            current_sessions=assertions,
        )

    async def maintenance_cancel_contract(
        self,
        contract: ContractHandle,
        *,
        reason: str = CONCORD_REAPER_STALE_CONTRACT_REASON,
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> bool:
        cancelled = await self._coordinator.maintenance_cancel(
            contract,
            reason=reason,
            cancelled_by=CONCORD_MAINTENANCE_ACTOR,
            now=now,
        )
        if cancelled:
            entry = self._coordinator.contract_source.get_cached(contract.key)
            if entry is not None:
                await self._apply_contract_change(
                    KvChange(
                        self.contract_bucket,
                        contract.key,
                        entry.revision,
                        "put",
                        entry,
                    )
                )
            logger.log(
                _contract_terminal_log_level(contract.profile),
                "%s Concord maintenance cancelled contract profile=%s contract=%s "
                "generation=%s cancelled_by=%s reason=%s revision=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                CONCORD_MAINTENANCE_ACTOR,
                reason,
                contract.revision,
            )
        return cancelled

    async def maintenance_delete_cancelled_contract(
        self,
        contract: ContractHandle,
        *,
        retention_seconds: float = float(
            DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS
        ),
        log_label: str = "Concord",
        now: datetime | None = None,
    ) -> ConcordMaintenanceDeletionResult:
        now = now or _now_utc()
        observed = await self._coordinator.maintenance_contract_entry(contract)
        if observed is None:
            return ConcordMaintenanceDeletionResult(deleted=False)
        current, record = observed
        if record.state != ContractState.CANCELLED:
            return ConcordMaintenanceDeletionResult(deleted=False)
        if record.cancelled_at is None or (
            now - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < retention_seconds:
            return ConcordMaintenanceDeletionResult(deleted=False)

        validity = await self._coordinator.validate_exact(contract)
        token_entries = await _concord_participant_token_entries(
            self._coordinator.token_scan,
            contract_id=contract.contract_id,
            generation=contract.generation,
            terms_hash=record.terms_hash,
        )
        _log_concord_contract_deletion_audit(
            log_label=log_label,
            contract_key=contract.key,
            record=record,
            state_revision=current.revision,
            validation_status=validity.status,
            token_entries=token_entries,
            deleted_token_key_count=len(token_entries),
        )
        try:
            await self._coordinator.contract_scan.delete(
                contract.key,
                revision=current.revision,
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
                current.revision,
                exc_info=True,
            )
            return ConcordMaintenanceDeletionResult(deleted=False)

        deleted_token_count = await _delete_concord_participant_token_entries(
            self._coordinator.token_scan,
            token_entries,
        )
        return ConcordMaintenanceDeletionResult(
            deleted=True,
            deleted_token_key_count=deleted_token_count,
        )

    async def _contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        async with self._lock:
            record = self._contract_records_by_key.get(contract.key)
            if record is None:
                return None
            if (
                record.contract_id != contract.contract_id
                or record.generation != contract.generation
            ):
                return None
            return record

    async def _contracts_filtered(
        self,
        profile: str | None = None,
        *,
        contract_id: str | None = None,
        participant: str | EndpointAddress | None = None,
        state: ContractState | None = None,
    ) -> tuple[ContractHandle, ...]:
        if self._started:
            await self.wait_current()
        if contract_id is not None:
            contract_id = _require_text(
                contract_id,
                field_name="Concord contract id",
            )
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        async with self._lock:
            keys: set[str] = set(self._contract_handles_by_key)
            if profile is not None:
                keys &= set(self._contract_keys_by_profile.get(profile, ()))
            if contract_id is not None:
                keys &= set(self._contract_keys_by_contract_id.get(contract_id, ()))
            if parsed_participant is not None:
                keys &= set(
                    self._contract_keys_by_participant.get(str(parsed_participant), ())
                )
            if state is not None:
                keys &= set(self._contract_keys_by_state.get(state, ()))
            return tuple(
                self._contract_handles_by_key[key]
                for key in sorted(keys)
                if key in self._contract_handles_by_key
            )

    async def _attach(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        *,
        token_id: str | None = None,
        log_label: str = "Concord",
    ) -> ParticipantHandle:
        token = await self._coordinator.attach(
            contract,
            participant,
            session_id,
            token_id=token_id,
        )
        contract_entry = self._coordinator.contract_source.get_cached(
            contract.key
        )
        if contract_entry is not None:
            await self._apply_contract_change(
                KvChange(
                    self.contract_bucket,
                    contract.key,
                    contract_entry.revision,
                    "put",
                    contract_entry,
                )
            )
        token_entry = self._coordinator.token_source.get_cached(token.key)
        if token_entry is not None:
            await self._apply_token_change(
                KvChange(
                    self.token_bucket,
                    token.key,
                    token_entry.revision,
                    "put",
                    token_entry,
                )
            )
        logger.log(
            _contract_lifecycle_log_level(contract.profile),
            "%s Concord participant token attached profile=%s contract=%s "
            "generation=%s participant=%s session=%s token=%s refresh=%s "
            "revision=%s ttl=%s",
            log_label,
            contract.profile,
            contract.contract_id,
            contract.generation,
            token.participant,
            token.session_id,
            token.token_id,
            token.refresh_seq,
            token.revision,
            token.ttl_seconds,
        )
        return token

    async def _refresh_token(
        self,
        handle: ParticipantHandle,
        *,
        log_label: str = "Concord",
    ) -> ParticipantHandle:
        refreshed = await self._coordinator.refresh(handle)
        token_entry = self._coordinator.token_source.get_cached(refreshed.key)
        if token_entry is not None:
            await self._apply_token_change(
                KvChange(
                    self.token_bucket,
                    refreshed.key,
                    token_entry.revision,
                    "put",
                    token_entry,
                )
            )
        logger.debug(
            "%s Concord participant token heartbeat contract=%s generation=%s "
            "participant=%s session=%s token=%s refresh=%s revision=%s ttl=%s",
            log_label,
            refreshed.contract_id,
            refreshed.generation,
            refreshed.participant,
            refreshed.session_id,
            refreshed.token_id,
            refreshed.refresh_seq,
            refreshed.revision,
            refreshed.ttl_seconds,
        )
        return refreshed

    async def _withdraw_token(
        self,
        handle: ParticipantHandle,
        *,
        log_label: str = "Concord",
    ) -> bool:
        withdrawn = await self._coordinator.withdraw(handle)
        if not withdrawn:
            return False
        marker_revision = self._coordinator.token_source.revision_cached(
            handle.key
        ) or (handle.revision + 1)
        await self._apply_token_change(
            KvChange(
                self.token_bucket,
                handle.key,
                marker_revision,
                "delete",
            )
        )
        logger.debug(
            "%s Concord participant token withdrawn contract=%s generation=%s "
            "participant=%s session=%s token=%s revision=%s",
            log_label,
            handle.contract_id,
            handle.generation,
            handle.participant,
            handle.session_id,
            handle.token_id,
            marker_revision,
        )
        return True

    async def _validate_participant_token(
        self,
        handle: ParticipantHandle,
    ) -> ParticipantHandle:
        return await self._coordinator.validate_participant_handle(handle)

    async def _cancel(
        self,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        *,
        reason: str | None = None,
        log_label: str = "Concord",
    ) -> bool:
        cancelled = await self._coordinator.cancel(
            contract,
            participant,
            reason=reason,
        )
        if cancelled:
            entry = self._coordinator.contract_source.get_cached(contract.key)
            if entry is not None:
                await self._apply_contract_change(
                    KvChange(
                        self.contract_bucket,
                        contract.key,
                        entry.revision,
                        "put",
                        entry,
                    )
                )
            logger.log(
                _contract_terminal_log_level(contract.profile),
                "%s Concord contract cancelled profile=%s contract=%s generation=%s "
                "participant=%s reason=%s revision=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                parse_endpoint_address(participant),
                reason,
                contract.revision,
            )
        return cancelled

    async def _validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
        log_label: str = "Concord",
        log_invalid: bool = True,
    ) -> ContractValidity:
        async with self._lock:
            validity = self._validate_from_cache_locked(
                contract,
                current_sessions=current_sessions,
            )
        if log_invalid and validity.status in {
            ContractValidityStatus.MISSING_TOKEN,
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityStatus.GENERATION_MISMATCH,
            ContractValidityStatus.SESSION_MISMATCH,
            ContractValidityStatus.TERMS_HASH_MISMATCH,
        }:
            logger.log(
                _contract_invalid_log_level(contract.profile, validity.status),
                "%s Concord contract invalid profile=%s contract=%s generation=%s "
                "status=%s reason=%s",
                log_label,
                contract.profile,
                contract.contract_id,
                contract.generation,
                validity.status.value,
                validity.reason,
            )
        return validity

    def _participant_lease(
        self,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        token_id: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        log_label: str = "Concord",
    ) -> ConcordParticipantLease:
        return ConcordParticipantLease(
            self,
            contract=contract,
            participant=participant,
            session_id=session_id,
            token_id=token_id,
            refresh_interval=refresh_interval,
            log_label=log_label,
        )

    async def _forget_participant_lease(self, lease: ConcordParticipantLease) -> None:
        async with self._lock:
            self._participant_leases.discard(lease)

    @asynccontextmanager
    async def watch(
        self,
        profile: str | None = None,
        *,
        participant: str | EndpointAddress | None = None,
        replay_current: bool = True,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ConcordEvent]]:
        if self._started:
            await self.wait_current()
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        send, receive = anyio.create_memory_object_stream[ConcordEvent](
            max_buffer_size=self._buffer_size
        )
        subscriber = _ConcordSubscriber(
            send,
            profile,
            parsed_participant,
            replay_pending=replay_current,
        )
        initial: tuple[ConcordEvent, ...] = ()
        async with self._lock:
            self._subscribers.add(subscriber)
            if replay_current:
                initial_events: list[ConcordEvent] = []
                for contract in self._contract_handles_by_key.values():
                    validity = self._validate_from_cache_locked(contract)
                    event = ConcordEvent(
                        _concord_event_type(validity),
                        contract=contract,
                        record=self._contract_records_by_key.get(contract.key),
                        validity=validity,
                        profile=contract.profile,
                    )
                    if _event_matches_subscriber(subscriber, event):
                        initial_events.append(event)
                initial = tuple(initial_events)
        try:
            async with send, receive:
                for event in initial:
                    await send.send(event)
                if replay_current:
                    await self._finish_subscriber_replay(subscriber)
                yield receive
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)

    @asynccontextmanager
    async def watch_contract_notifications_cached(
        self,
        profile: str | None = None,
        *,
        participant: str | EndpointAddress | None = None,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[_ConcordContractNotification]]:
        if self._started:
            await self.wait_current()
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        known_profiles = await self._known_notification_profiles(
            profile=profile,
            participant=parsed_participant,
        )
        known_lock = anyio.Lock()
        send, receive = anyio.create_memory_object_stream[_ConcordContractNotification](
            max_buffer_size=self._buffer_size
        )
        async with (
            self._coordinator.contract_source.subscribe() as contract_changes,
            self._coordinator.token_source.subscribe() as token_changes,
            anyio.create_task_group() as task_group,
            send,
            receive,
        ):
            task_group.start_soon(
                self._pump_contract_notifications,
                contract_changes,
                send,
                profile,
                parsed_participant,
                known_profiles,
                known_lock,
            )
            task_group.start_soon(
                self._pump_token_notifications,
                token_changes,
                send,
                profile,
                parsed_participant,
                known_profiles,
                known_lock,
            )
            try:
                yield receive
            finally:
                task_group.cancel_scope.cancel()

    async def _known_notification_profiles(
        self,
        *,
        profile: str | None,
        participant: EndpointAddress | None,
    ) -> dict[tuple[str, int], str | None]:
        async with self._lock:
            known: dict[tuple[str, int], str | None] = {}
            for contract in self._contract_handles_by_key.values():
                if not _contract_matches_notification_filter(
                    contract,
                    profile=profile,
                    participant=participant,
                ):
                    continue
                known[(contract.contract_id, contract.generation)] = contract.profile
            return known

    async def _pump_contract_notifications(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
        send: anyio.abc.ObjectSendStream[_ConcordContractNotification],
        profile: str | None,
        participant: EndpointAddress | None,
        known_profiles: dict[tuple[str, int], str | None],
        known_lock: anyio.Lock,
    ) -> None:
        async for change in changes:
            async with known_lock:
                notification = _contract_notification_from_change(
                    change,
                    profile_filter=profile,
                    participant_filter=participant,
                    known_profiles=known_profiles,
                )
            if notification is None:
                continue
            await send.send(notification)

    async def _pump_token_notifications(
        self,
        changes: anyio.abc.ObjectReceiveStream[KvChange],
        send: anyio.abc.ObjectSendStream[_ConcordContractNotification],
        profile: str | None,
        participant: EndpointAddress | None,
        known_profiles: dict[tuple[str, int], str | None],
        known_lock: anyio.Lock,
    ) -> None:
        async for change in changes:
            async with known_lock:
                notification = _token_notification_from_change(
                    change,
                    profile_filter=profile,
                    participant_filter=participant,
                    known_profiles=known_profiles,
                )
            if notification is None:
                continue
            await send.send(notification)

    async def _finish_subscriber_replay(
        self,
        subscriber: _ConcordSubscriber,
    ) -> None:
        while True:
            async with self._lock:
                pending = tuple(subscriber.pending_events)
                subscriber.pending_events.clear()
                if not pending:
                    subscriber.replay_pending = False
                    return
            for event in pending:
                await subscriber.send.send(event)


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


class ConcordReaperService:
    """Periodic Concord-only maintenance for stale and cancelled contracts."""

    def __init__(
        self,
        concord: Concord,
        *,
        config: ConcordReaperConfig | Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._concord = concord
        self._contract_bucket = concord._coordinator.contract_scan  # noqa: SLF001
        self._token_bucket = concord._coordinator.token_scan  # noqa: SLF001
        self._maintenance_bucket = concord._maintenance_scan  # noqa: SLF001
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

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        del task_group

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
        for entry in await self._contract_bucket.items_exact(concord_contracts_prefix()):
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
            await self._clear_orphaned_stale_observations()
        )
        stale_observation_count = len(
            await self._maintenance_bucket.items_exact("stale.")
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
        except ValueError as exc:
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
        handle = _contract_handle(entry.key, record, entry.revision)
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
        validity = await self._concord._coordinator.validate_exact(  # noqa: SLF001
            contract,
        )
        if _open_contract_validity_is_stale(validity):
            first_observed, created = await self._observe_stale(
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
                    cancelled = await self._concord.maintenance_cancel_contract(
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
                    if await self._clear_stale_observation(
                        contract.contract_id,
                        contract.generation,
                    ):
                        counts["stale_observations_cleared"] += 1
            return counts

        if validity.status == ContractValidityStatus.UNAVAILABLE:
            return counts
        if await self._clear_stale_observation(
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
        if await self._clear_stale_observation(contract.contract_id, contract.generation):
            counts["stale_observations_cleared"] += 1
        if record.cancelled_at is None or (
            now - record.cancelled_at.astimezone(UTC)
        ).total_seconds() < self._config.cancelled_retention_seconds:
            return counts
        result = await self._concord.maintenance_delete_cancelled_contract(
            contract,
            retention_seconds=self._config.cancelled_retention_seconds,
            log_label=self._config.log_label,
            now=now,
        )
        if result.deleted:
            counts["contracts_deleted"] += 1
            counts["token_keys_deleted"] += result.deleted_token_key_count
            if await self._clear_stale_observation(
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
            if await self._clear_stale_observation(contract_id, generation):
                counts["stale_observations_cleared"] += 1
            cancelled_at = _datetime_from_raw_value(entry.value.get("cancelledAt"))
            if cancelled_at is None or (
                now - cancelled_at
            ).total_seconds() < self._config.cancelled_retention_seconds:
                return counts
            result = await self._delete_invalid_cancelled_contract(
                entry,
                contract_id=contract_id,
                generation=generation,
                invalid_reason=invalid_reason,
            )
            if result.deleted:
                counts["contracts_deleted"] += 1
                counts["token_keys_deleted"] += result.deleted_token_key_count
            return counts

        first_observed, created = await self._observe_stale(
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
        try:
            await self._maintenance_cancel_invalid_entry(
                entry,
                contract_id=contract_id,
                generation=generation,
                now=now,
            )
        except ConcordConflict:
            logger.info(
                "%s Concord reaper invalid contract cancel conflict; "
                "leaving for next scan contract=%s generation=%s",
                self._config.log_label,
                contract_id,
                generation,
                exc_info=True,
            )
            return counts
        counts["contracts_cancelled"] += 1
        if await self._clear_stale_observation(contract_id, generation):
            counts["stale_observations_cleared"] += 1
        return counts

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
        current = await self._maintenance_bucket.get_exact(key)
        if current is not None:
            record = _parse_stale_observation_entry(
                current,
                key=key,
                contract_id=contract_id,
                generation=generation,
            )
            return record.first_observed_stale_at, False
        observation = ConcordStaleObservationRecord(
            contractId=contract_id,
            generation=generation,
            firstObservedStaleAt=now,
            status=status,
            reason=reason,
            contractRevision=contract_revision,
        )
        try:
            await self._maintenance_bucket.create(key, observation)
        except ConcordConflict:
            latest = await self._maintenance_bucket.get_exact(key)
            if latest is None:
                raise
            record = _parse_stale_observation_entry(
                latest,
                key=key,
                contract_id=contract_id,
                generation=generation,
            )
            return record.first_observed_stale_at, False
        return now, True

    async def _clear_stale_observation(
        self,
        contract_id: str,
        generation: int,
    ) -> bool:
        key = concord_stale_observation_key(
            contract_id=contract_id,
            generation=generation,
        )
        current = await self._maintenance_bucket.get_exact(key)
        if current is None:
            return False
        _parse_stale_observation_entry(
            current,
            key=key,
            contract_id=contract_id,
            generation=generation,
        )
        try:
            await self._maintenance_bucket.delete(key, revision=current.revision)
        except ConcordConflict:
            return False
        return True

    async def _clear_orphaned_stale_observations(self) -> int:
        cleared = 0
        for entry in await self._maintenance_bucket.items_exact("stale."):
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
            contract_entry = await self._contract_bucket.get_exact(
                concord_contract_key(
                    contract_id=observation.contract_id,
                    generation=observation.generation,
                )
            )
            if contract_entry is not None:
                continue
            try:
                await self._maintenance_bucket.delete(entry.key, revision=entry.revision)
            except ConcordConflict:
                continue
            cleared += 1
        return cleared

    async def _maintenance_cancel_invalid_entry(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        now: datetime,
    ) -> None:
        del now
        raise ConcordConflict(
            ConcordConflictCode.CONTRACT_INVALID,
            "Concord maintenance will not mutate a malformed contract record",
            key=entry.key,
            expected_pointer=ContractPointer(
                contractId=contract_id,
                generation=generation,
            ),
        )

    async def _delete_invalid_cancelled_contract(
        self,
        entry: KvEntry,
        *,
        contract_id: str,
        generation: int,
        invalid_reason: str,
    ) -> ConcordMaintenanceDeletionResult:
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
        return ConcordMaintenanceDeletionResult(deleted=False)


ConcordContractPredicate = Callable[
    [ContractHandle, ContractRecord],
    bool | Awaitable[bool],
]
ConcordCurrentSessions = Callable[
    [ContractHandle],
    Mapping[str, str] | None | Awaitable[Mapping[str, str] | None],
]
ConcordPrepareReconcile = Callable[[], None | Awaitable[None]]
ConcordContractSortKey = Callable[[ContractHandle], Any]
ConcordSessionEvidence = (
    Mapping[str | EndpointAddress, str]
    | Callable[
        [],
        Mapping[str | EndpointAddress, str]
        | Awaitable[Mapping[str | EndpointAddress, str]],
    ]
)


@dataclass(frozen=True, slots=True)
class ConcordAgreementSpec:
    """Owner-side Concord agreement request.

    Owner-side agreement creation opens a fresh opaque Concord contract. Use
    ``supersedes`` to explicitly link a replacement to a previous exact
    contract pointer.
    """

    profile: str | None
    participants: tuple[str | EndpointAddress, ...] | list[str | EndpointAddress]
    local_participant: str | EndpointAddress
    local_session_id: str
    terms: Mapping[str, Any] | DeckrModel | None = None
    supersedes: ContractPointer | Mapping[str, Any] | None = None
    current_sessions: ConcordSessionEvidence | None = None
    refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
    log_label: str = "Concord"
    created_by: str | EndpointAddress | None = None

    def __post_init__(self) -> None:
        participants = tuple(
            sorted(
                (parse_endpoint_address(item) for item in self.participants),
                key=str,
            )
        )
        if not participants:
            raise ValueError("Concord agreements require at least one participant")
        local_participant = parse_endpoint_address(self.local_participant)
        if local_participant not in participants:
            raise ValueError("local_participant must be named by participants")
        if self.refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if self.profile is not None:
            _require_text(self.profile, field_name="Concord agreement profile")
        supersedes = (
            self.supersedes
            if isinstance(self.supersedes, ContractPointer)
            else ContractPointer.model_validate(self.supersedes)
            if self.supersedes is not None
            else None
        )
        created_by = (
            parse_endpoint_address(self.created_by)
            if self.created_by is not None
            else local_participant
        )
        terms = (
            self.terms.model_dump(by_alias=True, exclude_none=True, mode="json")
            if isinstance(self.terms, DeckrModel)
            else self.terms
        )
        object.__setattr__(self, "participants", participants)
        object.__setattr__(self, "local_participant", local_participant)
        object.__setattr__(
            self,
            "local_session_id",
            _require_text(
                self.local_session_id,
                field_name="Concord agreement session id",
            ),
        )
        object.__setattr__(
            self,
            "terms",
            freeze_json(terms) if terms is not None else None,
        )
        object.__setattr__(self, "supersedes", supersedes)
        object.__setattr__(self, "created_by", created_by)


class ConcordAgreementLease:
    """Core-owned owner-side Concord agreement handle."""

    def __init__(
        self,
        service: Concord,
        *,
        spec: ConcordAgreementSpec,
        contract: ContractHandle,
        lease: ConcordParticipantLease,
        validity: ContractValidity,
    ) -> None:
        self._service = service
        self.spec = spec
        self.contract = contract
        self._lease = lease
        self._validity = validity
        self._closed = False

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    @property
    def generation(self) -> int:
        return self.contract.generation

    @property
    def profile(self) -> str | None:
        return self.contract.profile

    @property
    def validity(self) -> ContractValidity:
        return self._validity

    @property
    def valid(self) -> bool:
        return self._validity.valid

    @property
    def local_token(self) -> ParticipantHandle | None:
        return self._lease.token

    @property
    def closed(self) -> bool:
        return self._closed

    async def refresh(self) -> ContractValidity:
        return await self._service._refresh_agreement(self)  # noqa: SLF001

    async def cancel(self, reason: str | None = None) -> bool:
        return await self._service._cancel_agreement(self, reason=reason)  # noqa: SLF001

    async def aclose(self) -> None:
        self._closed = True
        await self._lease.aclose()


class ConcordParticipant:
    """Owns one local participant's token lifecycle for selected contracts."""

    def __init__(
        self,
        *,
        concord: Concord,
        participant: str | EndpointAddress,
        session_id: str,
        accept_contract: ConcordContractPredicate,
        current_sessions: ConcordCurrentSessions | None = None,
        prepare_reconcile: ConcordPrepareReconcile | None = None,
        contract_sort_key: ConcordContractSortKey | None = None,
        profile: str | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
        reconcile_interval: float = DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS,
        cancel_terminal_statuses: Collection[ContractValidityStatus] | None = None,
        log_label: str = "Concord",
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be greater than zero")
        if reconcile_interval <= 0:
            raise ValueError("reconcile_interval must be greater than zero")
        self._concord = concord
        self.participant = parse_endpoint_address(participant)
        self.session_id = _require_text(session_id, field_name="Concord session id")
        self.profile = profile
        self._accept_contract = accept_contract
        self._current_sessions = current_sessions
        self._prepare_reconcile = prepare_reconcile
        self._contract_sort_key = contract_sort_key
        self._refresh_interval = refresh_interval
        self._reconcile_interval = reconcile_interval
        self._cancel_terminal_statuses = (
            DEFAULT_CONCORD_MANAGED_CANCEL_TERMINAL_STATUSES
            if cancel_terminal_statuses is None
            else frozenset(cancel_terminal_statuses)
        )
        self._log_label = log_label
        self._managed: dict[str, ConcordManagedContract] = {}
        self._leases: dict[str, ConcordParticipantLease] = {}
        self._last_status: dict[str, ContractValidityStatus] = {}
        self._subscribers: set[anyio.abc.ObjectSendStream[ConcordManagedContractEvent]] = (
            set()
        )
        self._lock = anyio.Lock()
        self._start_soon: Callable[..., object] | None = None
        self._started = False
        self._closed = False

    @property
    def managed_contracts(self) -> tuple[ConcordManagedContract, ...]:
        return tuple(self._managed[key] for key in sorted(self._managed))

    def managed_contract(self, contract: ContractHandle) -> ConcordManagedContract | None:
        return self._managed.get(contract.key)

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object]) -> None:
        if self._started:
            return
        self._started = True
        self._start_soon = start_soon
        start_soon(self.watch_loop)
        start_soon(self.reconcile_loop)

    @asynccontextmanager
    async def watch(self) -> Any:
        send, receive = anyio.create_memory_object_stream[
            ConcordManagedContractEvent
        ](100)
        self._subscribers.add(send)
        async with send, receive:
            try:
                yield receive
            finally:
                self._subscribers.discard(send)

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            for lease in self._leases.values():
                await lease.aclose()
            self._leases.clear()
            self._managed.clear()
            self._last_status.clear()

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        reason: str | None = None,
    ) -> bool:
        return await self._concord._cancel(
            contract,
            self.participant,
            reason=reason,
            log_label=self._log_label,
        )

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        sessions: dict[str, str] = {}
        if current_sessions is not None:
            sessions.update(current_sessions)
        sessions[str(self.participant)] = self.session_id
        return await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )

    async def release(
        self,
        contract: ContractHandle | str,
        *,
        reason: str = "released",
        withdraw: bool = True,
    ) -> None:
        key = contract.key if isinstance(contract, ContractHandle) else contract
        async with self._lock:
            await self._release_locked(key, reason=reason, withdraw=withdraw)

    async def watch_loop(self) -> None:
        while not self._closed:
            try:
                async with self._concord.watch_contract_notifications_cached(
                    self.profile,
                    participant=self.participant,
                ) as stream:
                    await self.reconcile(reason="contract watch warmup")
                    async for notification in stream:
                        if self._closed:
                            return
                        try:
                            await self.reconcile_notification(notification)
                        except ConcordUnavailable:
                            logger.warning(
                                "%s Concord participant manager unavailable; "
                                "notification reconciliation will retry profile=%s "
                                "participant=%s",
                                self._log_label,
                                self.profile,
                                self.participant,
                                exc_info=True,
                            )
            except ConcordUnavailable:
                await anyio.sleep(self._reconcile_interval)

    async def reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile(reason="periodic reconcile")
            except ConcordUnavailable:
                logger.warning(
                    "%s Concord participant manager unavailable; "
                    "reconciliation will retry profile=%s participant=%s",
                    self._log_label,
                    self.profile,
                    self.participant,
                    exc_info=True,
                )
            await anyio.sleep(self._reconcile_interval)

    async def reconcile_notification(
        self,
        notification: _ConcordContractNotification,
    ) -> tuple[ConcordManagedContract, ...]:
        if self._concord._started:  # noqa: SLF001
            await self._concord.wait_current()
        async with self._lock:
            if self._closed:
                return ()
            if notification.source == _ConcordNotificationSource.CONTRACT:
                return await self._reconcile_contract_notification_locked(notification)
            return await self._reconcile_token_notification_locked(notification)

    async def _reconcile_contract_notification_locked(
        self,
        notification: _ConcordContractNotification,
    ) -> tuple[ConcordManagedContract, ...]:
        contract_key = concord_contract_key(
            contract_id=notification.contract_id,
            generation=notification.generation,
        )
        contract = notification.contract
        if contract is None:
            contract = await self._concord.get_contract(
                ContractPointer(
                    contractId=notification.contract_id,
                    generation=notification.generation,
                )
            )
        if contract is None:
            await self._release_locked(
                contract_key,
                reason=ContractValidityStatus.MISSING_CONTRACT.value,
                withdraw=False,
            )
            return self.managed_contracts
        if self._prepare_reconcile is not None:
            await _maybe_await(self._prepare_reconcile())
        managed = await self._reconcile_contract_locked(
            contract,
            reason=_notification_reason(notification),
        )
        if managed is None:
            if contract.key in self._managed or contract.key in self._leases:
                await self._release_locked(
                    contract.key,
                    reason="not_selected",
                    withdraw=True,
                )
            return self.managed_contracts
        self._managed[contract.key] = managed
        return self.managed_contracts

    async def _reconcile_token_notification_locked(
        self,
        notification: _ConcordContractNotification,
    ) -> tuple[ConcordManagedContract, ...]:
        contract_key = concord_contract_key(
            contract_id=notification.contract_id,
            generation=notification.generation,
        )
        managed = self._managed.get(contract_key)
        lease = self._leases.get(contract_key)
        contract = (
            managed.contract
            if managed is not None
            else lease.contract
            if lease is not None
            else None
        )
        if contract is None:
            return self.managed_contracts
        if self._prepare_reconcile is not None:
            await _maybe_await(self._prepare_reconcile())
        next_managed = await self._reconcile_contract_locked(
            contract,
            reason=_notification_reason(notification),
        )
        if next_managed is None:
            if contract.key in self._managed or contract.key in self._leases:
                await self._release_locked(
                    contract.key,
                    reason="not_selected",
                    withdraw=True,
                )
            return self.managed_contracts
        self._managed[contract.key] = next_managed
        return self.managed_contracts

    async def reconcile(
        self,
        *,
        reason: str = "manual reconcile",
    ) -> tuple[ConcordManagedContract, ...]:
        async with self._lock:
            if self._closed:
                return ()
            contracts = await self._reconcile_contract_candidates_locked()
            if self._prepare_reconcile is not None:
                await _maybe_await(self._prepare_reconcile())
            if self._contract_sort_key is not None:
                contracts = tuple(sorted(contracts, key=self._contract_sort_key))
            next_managed: dict[str, ConcordManagedContract] = {}
            next_leases: dict[str, ConcordParticipantLease] = {}

            for contract in contracts:
                managed = await self._reconcile_contract_locked(
                    contract,
                    reason=reason,
                )
                if managed is None:
                    continue
                next_managed[contract.key] = managed
                lease = self._leases.get(contract.key)
                if lease is not None:
                    next_leases[contract.key] = lease

            for key in tuple(self._leases):
                if key not in next_leases:
                    await self._release_locked(
                        key,
                        reason="not_selected",
                        withdraw=True,
                    )

            self._managed = next_managed
            self._leases = next_leases
            return self.managed_contracts

    async def _reconcile_contract_candidates_locked(self) -> tuple[ContractHandle, ...]:
        indexed = await self._concord.contracts(
            self.profile,
            participant=self.participant,
            state=ContractState.OPEN,
        )
        candidates = {contract.key: contract for contract in indexed}
        for managed in self._managed.values():
            candidates.setdefault(managed.contract.key, managed.contract)
        for lease in self._leases.values():
            candidates.setdefault(lease.contract.key, lease.contract)
        return tuple(candidates[key] for key in sorted(candidates))

    async def _reconcile_contract_locked(
        self,
        contract: ContractHandle,
        *,
        reason: str,
    ) -> ConcordManagedContract | None:
        if self.participant not in contract.participants:
            await self._release_locked(
                contract.key,
                reason="participant_not_named",
                withdraw=True,
            )
            return None

        try:
            record = await self._concord._contract_record(contract)
        except ValueError:
            await self._release_locked(
                contract.key,
                reason=ContractValidityStatus.INVALID_CONTRACT.value,
                withdraw=False,
            )
            return None
        if record is None:
            await self._release_locked(
                contract.key,
                reason=ContractValidityStatus.MISSING_CONTRACT.value,
                withdraw=False,
            )
            return None
        if self.profile is not None and record.profile != self.profile:
            await self._release_locked(
                contract.key,
                reason="profile_mismatch",
                withdraw=True,
            )
            return None

        if record.state == ContractState.CANCELLED:
            validity = ContractValidity(
                ContractValidityStatus.CANCELLED,
                contract=record,
                reason=record.cancel_reason or "contract is cancelled",
                reason_code=ContractValidityReason.CONTRACT_CANCELLED,
            )
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=None,
                reason=reason,
            )
            await self._release_locked(
                contract.key,
                reason=ContractState.CANCELLED.value,
                withdraw=False,
            )
            return None

        sessions = await self._current_sessions_for(contract)
        validity = await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )
        record = validity.contract or record

        existing = validity.tokens.get(str(self.participant))
        if _terminal_managed_status(validity.status):
            await self._publish_and_release_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=existing,
                reason=reason,
            )
            return None

        if not await _maybe_await(self._accept_contract(contract, record)):
            await self._release_locked(
                contract.key,
                reason="policy_rejected",
                withdraw=True,
            )
            return None

        lease = self._leases.get(contract.key)
        if lease is None:
            lease = self._concord._participant_lease(
                contract=contract,
                participant=self.participant,
                session_id=self.session_id,
                refresh_interval=self._refresh_interval,
                log_label=self._log_label,
            )
            if self._start_soon is not None:
                lease.start_soon(self._start_soon)
            self._leases[contract.key] = lease

        if existing is not None:
            if existing.session_id != self.session_id:
                validity = ContractValidity(
                    ContractValidityStatus.SESSION_MISMATCH,
                    contract=record,
                    tokens=validity.tokens,
                    reason=str(self.participant),
                    reason_code=ContractValidityReason.TOKEN_SESSION_MISMATCH,
                )
                await self._publish_and_release_terminal_locked(
                    contract,
                    record=record,
                    validity=validity,
                    token=existing,
                    reason=reason,
                )
                return None
            if lease.token is None:
                await self._cancel_and_release_lost_participant_token_locked(contract)
                return None
            try:
                lease.adopt(existing)
            except ValueError:
                await self._cancel_and_release_lost_participant_token_locked(contract)
                return None

        try:
            token = await lease.attach_or_refresh()
        except ConcordConflict:
            validity = await self._concord._validate(
                contract,
                current_sessions=sessions,
                log_label=self._log_label,
            )
            record = validity.contract or record
            if _terminal_managed_status(validity.status):
                await self._publish_and_release_terminal_locked(
                    contract,
                    record=record,
                    validity=validity,
                    token=None,
                    reason=reason,
                )
                return None
            await self._publish_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=None,
                reason=reason,
            )
            return None

        validity = await self._concord._validate(
            contract,
            current_sessions=sessions,
            log_label=self._log_label,
        )
        record = validity.contract or record
        managed = ConcordManagedContract(
            contract=contract,
            record=record,
            validity=validity,
            token=token,
        )
        if _terminal_managed_status(validity.status):
            await self._publish_and_release_terminal_locked(
                contract,
                record=record,
                validity=validity,
                token=token,
                reason=reason,
            )
            return None
        self._publish_status(managed, reason=reason)
        return managed

    async def _current_sessions_for(
        self,
        contract: ContractHandle,
    ) -> Mapping[str, str]:
        sessions: dict[str, str] = {}
        if self._current_sessions is not None:
            current = await _maybe_await(self._current_sessions(contract))
            if current is not None:
                sessions.update(current)
        sessions[str(self.participant)] = self.session_id
        return sessions

    async def _release_locked(
        self,
        key: str,
        *,
        reason: str,
        withdraw: bool,
    ) -> None:
        managed = self._managed.pop(key, None)
        lease = self._leases.pop(key, None)
        if lease is not None:
            await lease.aclose(withdraw=withdraw)
        self._last_status.pop(key, None)
        if managed is None:
            return
        self._publish(
            ConcordManagedContractEvent(
                ConcordManagedContractEventType.RELEASED,
                managed.contract,
                record=managed.record,
                validity=managed.validity,
                token=managed.token,
                reason=reason,
            )
        )

    async def _cancel_and_release_lost_participant_token_locked(
        self,
        contract: ContractHandle,
    ) -> None:
        try:
            await self.cancel(
                contract,
                reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
            )
        except (ConcordConflict, ConcordUnavailable, ValueError):
            logger.debug(
                "%s could not cancel Concord contract after local participant "
                "token authority was lost contract=%s generation=%s participant=%s "
                "session=%s",
                self._log_label,
                contract.contract_id,
                contract.generation,
                self.participant,
                self.session_id,
                exc_info=True,
            )
        await self._release_locked(
            contract.key,
            reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
            withdraw=False,
        )

    async def _publish_terminal_locked(
        self,
        contract: ContractHandle,
        *,
        record: ContractRecord,
        validity: ContractValidity,
        token: ParticipantHandle | None,
        reason: str,
    ) -> None:
        managed = ConcordManagedContract(
            contract=contract,
            record=record,
            validity=validity,
            token=token,
        )
        self._publish_status(managed, reason=reason)

    async def _publish_and_release_terminal_locked(
        self,
        contract: ContractHandle,
        *,
        record: ContractRecord,
        validity: ContractValidity,
        token: ParticipantHandle | None,
        reason: str,
    ) -> None:
        if validity.status in self._cancel_terminal_statuses:
            try:
                await self.cancel(
                    contract,
                    reason=f"concord_managed_{validity.status.value}",
                )
            except (ConcordConflict, ConcordUnavailable, ValueError):
                logger.debug(
                    "%s could not cancel terminal Concord contract %s",
                    self._log_label,
                    contract.contract_id,
                    exc_info=True,
                )
        await self._publish_terminal_locked(
            contract,
            record=record,
            validity=validity,
            token=token,
            reason=reason,
        )
        await self._release_locked(
            contract.key,
            reason=validity.status.value,
            withdraw=False,
        )

    def _publish_status(
        self,
        managed: ConcordManagedContract,
        *,
        reason: str,
    ) -> None:
        status = managed.validity.status
        previous = self._last_status.get(managed.contract.key)
        self._last_status[managed.contract.key] = status
        if previous == status:
            return
        self._publish(
            ConcordManagedContractEvent(
                _managed_event_type(status),
                managed.contract,
                record=managed.record,
                validity=managed.validity,
                token=managed.token,
                reason=reason,
            )
        )

    def _publish(self, event: ConcordManagedContractEvent) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.send_nowait(event)
            except anyio.WouldBlock:
                continue
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                self._subscribers.discard(subscriber)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _discard_index_key(index: dict[Any, set[str]], value: Any, key: str) -> None:
    keys = index.get(value)
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        index.pop(value, None)


def _change_is_next_generation(
    change: KvChange,
    *,
    current_generation: int,
) -> bool:
    if change.view_generation is None:
        return True
    if change.view_generation <= current_generation:
        return False
    return change.view_generation == current_generation + 1


def _change_generation(change: KvChange, bucket: Any) -> int:
    if change.view_generation is not None:
        return change.view_generation
    return int(getattr(bucket, "generation", 0))


def _rebuild_change(
    bucket: str,
    key: str,
    revision: int,
    operation: Literal["put", "delete", "expire"],
    entry: KvEntry | None,
) -> KvChange:
    return KvChange(
        bucket,
        key,
        revision,
        operation,
        entry,
        marker_reason="rebuild",
    )


def _contract_matches_notification_filter(
    contract: ContractHandle,
    *,
    profile: str | None,
    participant: EndpointAddress | None,
) -> bool:
    if profile is not None and contract.profile != profile:
        return False
    return not (participant is not None and participant not in contract.participants)


def _contract_notification_from_change(
    change: KvChange,
    *,
    profile_filter: str | None,
    participant_filter: EndpointAddress | None,
    known_profiles: dict[tuple[str, int], str | None],
) -> _ConcordContractNotification | None:
    parsed = parse_concord_contract_key(change.key)
    if parsed is None:
        return None
    contract_id, generation = parsed
    pointer = (contract_id, generation)
    was_known = pointer in known_profiles
    known_profile = known_profiles.get(pointer)
    contract = _contract_handle_from_change(change)
    if contract is not None:
        if _contract_matches_notification_filter(
            contract,
            profile=profile_filter,
            participant=participant_filter,
        ):
            known_profiles[pointer] = contract.profile
            profile = contract.profile
            profile_known = True
        else:
            known_profiles.pop(pointer, None)
            if not was_known:
                return None
            profile = contract.profile
            profile_known = True
    else:
        profile = known_profile
        profile_known = pointer in known_profiles
        if change.operation in {"delete", "expire"}:
            known_profiles.pop(pointer, None)
    if (
        profile_filter is not None
        and profile_known
        and profile != profile_filter
        and not was_known
    ):
        return None
    if participant_filter is not None and not was_known and contract is None:
        return None
    return _ConcordContractNotification(
        _ConcordNotificationSource.CONTRACT,
        change.operation,
        contract_id,
        generation,
        contract=contract,
        profile=profile,
        change=change,
    )


def _token_notification_from_change(
    change: KvChange,
    *,
    profile_filter: str | None,
    participant_filter: EndpointAddress | None,
    known_profiles: Mapping[tuple[str, int], str | None],
) -> _ConcordContractNotification | None:
    parsed = parse_concord_participant_token_key(change.key)
    if parsed is None:
        return None
    contract_id, generation, participant = parsed
    pointer = (contract_id, generation)
    profile_known = pointer in known_profiles
    profile = known_profiles.get(pointer)
    if participant_filter is not None and not profile_known:
        return None
    if profile_filter is not None and profile_known and profile != profile_filter:
        return None
    return _ConcordContractNotification(
        _ConcordNotificationSource.TOKEN,
        change.operation,
        contract_id,
        generation,
        participant=participant,
        profile=profile,
        change=change,
    )


def _contract_handle_from_change(change: KvChange) -> ContractHandle | None:
    if change.operation != "put" or change.entry is None:
        return None
    parsed = parse_concord_contract_key(change.entry.key)
    if parsed is None:
        return None
    contract_id, generation = parsed
    try:
        record = ContractRecord.model_validate(change.entry.value)
    except ValueError:
        return None
    if record.contract_id != contract_id or record.generation != generation:
        return None
    return _contract_handle(change.entry.key, record, change.entry.revision)


def _event_matches_subscriber(
    subscriber: _ConcordSubscriber,
    event: ConcordEvent,
) -> bool:
    if subscriber.profile is not None and event.profile != subscriber.profile:
        return False
    if subscriber.participant is None:
        return True
    participant = str(subscriber.participant)
    if event.participant is not None and str(event.participant) == participant:
        return True
    if event.contract is not None:
        return participant in {str(item) for item in event.contract.participants}
    if event.record is not None:
        return participant in {str(item) for item in event.record.participants}
    return False


async def _agreement_current_sessions(
    spec: ConcordAgreementSpec,
) -> dict[str, str]:
    sessions: dict[str, str] = {}
    evidence = spec.current_sessions
    if evidence is not None:
        raw = evidence() if callable(evidence) else evidence
        current = await _maybe_await(raw)
        sessions.update({str(key): value for key, value in current.items()})
    sessions[str(spec.local_participant)] = spec.local_session_id
    return sessions


def _agreement_successor_status(status: ContractValidityStatus) -> bool:
    return status in {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }


def _lost_agreement_authority_validity(
    agreement: ConcordAgreementLease,
) -> ContractValidity:
    return ContractValidity(
        ContractValidityStatus.INVALID_TOKEN,
        contract=agreement.validity.contract,
        tokens=agreement.validity.tokens,
        reason=CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON,
        reason_code=ContractValidityReason.TOKEN_LOCAL_AUTHORITY_LOST,
    )


DEFAULT_CONCORD_MANAGED_CANCEL_TERMINAL_STATUSES = frozenset(
    {
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }
)


def _terminal_managed_status(status: ContractValidityStatus) -> bool:
    return status in {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }


def _notification_reason(notification: _ConcordContractNotification) -> str:
    key = notification.change.key if notification.change is not None else "<snapshot>"
    return f"{notification.source.value} watch {notification.operation} {key}"


def _managed_event_type(
    status: ContractValidityStatus,
) -> ConcordManagedContractEventType:
    if status == ContractValidityStatus.VALID:
        return ConcordManagedContractEventType.VALID
    if status == ContractValidityStatus.CANCELLED:
        return ConcordManagedContractEventType.CANCELLED
    if status == ContractValidityStatus.NOT_YET_FULFILLED:
        return ConcordManagedContractEventType.PENDING
    return ConcordManagedContractEventType.INVALID


def _is_terminal_participant_conflict(exc: ConcordConflict) -> bool:
    return exc.code in {
        ConcordConflictCode.CONTRACT_MISSING,
        ConcordConflictCode.CONTRACT_CANCELLED,
        ConcordConflictCode.CONTRACT_INVALID,
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
        ConcordConflictCode.PARTICIPANT_NOT_NAMED,
        ConcordConflictCode.PARTICIPANT_ALREADY_ATTACHED,
        ConcordConflictCode.TOKEN_MISSING,
        ConcordConflictCode.TOKEN_INVALID,
        ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
    }


def _concord_event_type(validity: ContractValidity) -> ConcordEventType:
    if validity.status == ContractValidityStatus.VALID:
        return ConcordEventType.CONTRACT_VALID
    if validity.status == ContractValidityStatus.CANCELLED:
        return ConcordEventType.CONTRACT_CANCELLED
    if validity.status in {
        ContractValidityStatus.NOT_YET_FULFILLED,
    }:
        return ConcordEventType.CONTRACT_PENDING
    return ConcordEventType.CONTRACT_INVALID


def _log_concord_event(event: ConcordEvent) -> None:
    contract = event.contract
    if contract is None:
        return
    status = event.validity.status.value if event.validity is not None else None
    if event.event_type == ConcordEventType.CONTRACT_VALID:
        logger.log(
            _contract_lifecycle_log_level(event.profile),
            "Concord contract valid profile=%s contract=%s generation=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.CONTRACT_CANCELLED:
        logger.log(
            _contract_terminal_log_level(event.profile),
            "Concord contract cancelled profile=%s contract=%s generation=%s "
            "status=%s reason=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            status,
            event.reason,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.TOKEN_EXPIRED:
        logger.log(
            _contract_terminal_log_level(event.profile),
            "Concord participant token expired profile=%s contract=%s generation=%s "
            "participant=%s status=%s reason=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            event.participant,
            status,
            event.reason,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.TOKEN_ATTACHED:
        logger.log(
            _contract_lifecycle_log_level(event.profile),
            "Concord participant token attached profile=%s contract=%s generation=%s "
            "participant=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            event.participant,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.TOKEN_REFRESHED:
        logger.log(
            _token_refresh_log_level(),
            "Concord participant token refreshed profile=%s contract=%s generation=%s "
            "participant=%s revision=%s",
            event.profile,
            contract.contract_id,
            contract.generation,
            event.participant,
            contract.revision,
        )
        return
    if event.event_type == ConcordEventType.CONTRACT_PENDING:
        level = _contract_pending_log_level(event.profile)
    else:
        event_status = event.validity.status if event.validity is not None else None
        level = _contract_invalid_log_level(event.profile, event_status)
    logger.log(
        level,
        "Concord contract %s profile=%s contract=%s generation=%s status=%s "
        "reason=%s revision=%s",
        event.event_type.value,
        event.profile,
        contract.contract_id,
        contract.generation,
        status,
        event.reason,
        contract.revision,
    )


ConcordTokenMaintenanceEntry = tuple[KvEntry, ParticipantTokenRecord | None]


def _open_contract_validity_is_stale(validity: ContractValidity) -> bool:
    if validity.status in STALE_OPEN_CONTRACT_STATUSES:
        return True
    return (
        validity.status == ContractValidityStatus.NOT_YET_FULFILLED
        and not validity.tokens
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
    token_bucket: ConcordMaintenanceScanPort,
    *,
    contract_id: str,
    generation: int,
    terms_hash: str | None,
) -> tuple[ConcordTokenMaintenanceEntry, ...]:
    token_entries: list[ConcordTokenMaintenanceEntry] = []
    for entry in await token_bucket.items_exact(
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
                expected_pointer=ContractPointer(
                    contractId=contract_id,
                    generation=generation,
                ),
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
                expected_pointer=ContractPointer(
                    contractId=contract_id,
                    generation=generation,
                ),
            )
        token_entries.append((entry, token))
    return tuple(sorted(token_entries, key=lambda item: item[0].key))


async def _delete_concord_participant_token_entries(
    token_bucket: ConcordMaintenanceScanPort,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
) -> int:
    deleted = 0
    for entry, token in token_entries:
        if token is None:
            continue
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            continue
        contract_id, generation, participant = parsed
        if (
            token.contract_id != contract_id
            or token.generation != generation
            or token.participant != participant
        ):
            continue
        try:
            marker_revision = await token_bucket.delete(
                entry.key,
                revision=entry.revision,
            )
        except ConcordConflict:
            continue
        if marker_revision is None:
            continue
        deleted += 1
    return deleted


def _token_log_summary(
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
) -> tuple[list[str], dict[str, str], dict[str, int | None]]:
    participants: list[str] = []
    sessions: dict[str, str] = {}
    refresh_sequences: dict[str, int | None] = {}
    for entry, token in token_entries:
        if token is None:
            participants.append(f"<invalid:{entry.key}>")
            refresh_sequences[entry.key] = None
            continue
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


def _log_concord_raw_contract_deletion_audit(
    *,
    log_label: str,
    contract_key: str,
    contract_id: str,
    generation: int,
    raw_record: Mapping[str, Any],
    state_revision: int,
    validation_status: ContractValidityStatus,
    validation_reason: str,
    token_entries: tuple[ConcordTokenMaintenanceEntry, ...],
    deleted_token_key_count: int,
) -> None:
    record = thaw_json(raw_record)
    token_participants, token_sessions, token_refresh_sequences = _token_log_summary(
        token_entries
    )
    logger.info(
        "%s Concord maintenance deleting cancelled contract contract_key=%s "
        "contract=%s generation=%s profile=%s state=%s participants=%s "
        "attached_participants=%s created_by=%s created_at=%s "
        "cancelled_by=%s cancelled_at=%s cancel_reason=%s cancel_revision=%s "
        "supersedes=%s terms_hash=%s validation_status=%s validation_reason=%s "
        "state_revision=%s token_participants=%s token_sessions=%s "
        "token_refresh_sequences=%s deleted_token_key_count=%s",
        log_label,
        contract_key,
        contract_id,
        generation,
        record.get("profile"),
        record.get("state"),
        record.get("participants"),
        record.get("attachedParticipants"),
        record.get("createdBy"),
        record.get("createdAt"),
        record.get("cancelledBy"),
        record.get("cancelledAt"),
        record.get("cancelReason"),
        record.get("cancelRevision"),
        record.get("supersedes"),
        record.get("termsHash"),
        validation_status.value,
        validation_reason,
        state_revision,
        token_participants,
        token_sessions,
        token_refresh_sequences,
        deleted_token_key_count,
    )


__all__ = [
    "CONCORD_CONTRACT_SCHEMA_ID",
    "CONCORD_CONTRACT_BUCKET_POLICY",
    "CONCORD_MAINTENANCE_ACTOR",
    "CONCORD_MAINTENANCE_BUCKET_POLICY",
    "CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID",
    "CONCORD_REAPER_STALE_CONTRACT_REASON",
    "CONCORD_STALE_OBSERVATION_SCHEMA_ID",
    "CONCORD_TOKEN_BUCKET_POLICY",
    "DEFAULT_CONCORD_CONTRACT_BUCKET_NAME",
    "DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME",
    "DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS",
    "DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS",
    "DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS",
    "DEFAULT_CONCORD_TOKEN_BUCKET_NAME",
    "DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS",
    "ContractHandle",
    "ContractPointer",
    "ContractRecord",
    "ContractState",
    "ContractValidity",
    "ContractValidityReason",
    "ContractValidityStatus",
    "Concord",
    "ConcordAgreementLease",
    "ConcordAgreementSpec",
    "ConcordConflict",
    "ConcordConflictCode",
    "ConcordEvent",
    "ConcordEventType",
    "ConcordMaintenanceDeletionResult",
    "ConcordManagedContract",
    "ConcordManagedContractEvent",
    "ConcordManagedContractEventType",
    "ConcordParticipant",
    "ConcordReaperConfig",
    "ConcordReaperScanResult",
    "ConcordReaperService",
    "ConcordStaleObservationRecord",
    "ConcordUnavailable",
    "ConcordUnavailableCode",
    "ParticipantHandle",
    "ParticipantTokenRecord",
    "STALE_OPEN_CONTRACT_STATUSES",
    "TokenObservation",
    "canonical_json_bytes",
    "canonical_json_hash",
    "concord_contract_key",
    "concord_contract_prefix",
    "concord_contracts_prefix",
    "concord_participant_token_key",
    "concord_stale_observation_key",
    "parse_concord_contract_key",
    "parse_concord_participant_token_key",
]
