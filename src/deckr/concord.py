from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import anyio

from deckr._authority_buckets import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
)
from deckr._concord._keys import (
    canonical_json_bytes,
    canonical_json_hash,
    concord_contract_key,
    concord_contract_prefix,
    concord_contracts_prefix,
    concord_participant_token_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr._concord._models import (
    CONCORD_CONTRACT_SCHEMA_ID,
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    ConcordConflict,
    ConcordConflictCode,
    ConcordContractState,
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
    participant_handle_matches as _participant_handle_matches,
)
from deckr._concord._models import (
    require_text as _require_text,
)
from deckr._concord._store import (
    ConcordKvStore as _ConcordKvStore,
)
from deckr._concord._validation import (
    ConcordSessionAssertions,
)
from deckr._concord._view import ConcordView
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.contracts.models import DeckrModel, freeze_json
from deckr.core.util.anyio import CoalescedStateBroadcaster
from deckr.substrates.nats_kv import (
    NatsKvMaterializedBucket,
)

DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS = 60.0
DEFAULT_CONCORD_PARTICIPANT_RECONCILE_SECONDS = 15.0
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


@dataclass(frozen=True, slots=True)
class ConcordManagedContract:
    contract: ContractHandle
    record: ContractRecord
    validity: ContractValidity
    token: ParticipantHandle | None = None


@dataclass(frozen=True, slots=True)
class ConcordWatchSnapshot:
    version: int
    current: bool
    contracts: tuple[ConcordContractState, ...]


@dataclass(frozen=True, slots=True)
class ConcordWatchChange:
    version: int
    current: bool
    contracts: tuple[ConcordContractState, ...]
    changed_pointers: frozenset[ContractPointer]
    resnapshot_required: bool


@dataclass(frozen=True, slots=True)
class ConcordParticipantSnapshot:
    version: int
    current: bool
    contracts: tuple[ConcordManagedContract, ...]


@dataclass(frozen=True, slots=True)
class ConcordParticipantChange:
    version: int
    current: bool
    contracts: tuple[ConcordManagedContract, ...]
    changed_pointers: frozenset[ContractPointer]
    resnapshot_required: bool


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


class Concord:
    """Runtime-facing Concord API with participant leases and semantic events."""

    def __init__(
        self,
        contract_bucket: NatsKvMaterializedBucket | Any,
        token_bucket: NatsKvMaterializedBucket | Any,
    ) -> None:
        self._coordinator = _ConcordKvStore(
            contract_bucket,
            token_bucket,
        )
        self._view = ConcordView(
            self._coordinator.contract_source,
            self._coordinator.token_source,
        )
        self._started = False
        self._closed = False
        self._task_group: anyio.abc.TaskGroup | None = None
        self._agreement_lock = anyio.Lock()
        self._lock = anyio.Lock()
        self._participant_leases: set[ConcordParticipantLease] = set()

    @property
    def contract_bucket(self) -> str:
        return self._coordinator.contract_source.bucket

    @property
    def token_bucket(self) -> str:
        return self._coordinator.token_source.bucket

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        self._coordinator.contract_source.start(task_group)
        self._coordinator.token_source.start(task_group)
        if not self._started:
            self._started = True
            task_group.start_soon(self._view.run)
        for lease in tuple(self._participant_leases):
            lease.start(task_group)

    async def wait_ready(self) -> None:
        await self._view.wait_ready()

    def is_current(self) -> bool:
        return (
            self._coordinator.contract_source.is_current()
            and self._coordinator.token_source.is_current()
            and self._view.is_current()
        )

    async def wait_current(self) -> None:
        await self._view.wait_current()

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            leases = tuple(self._participant_leases)
            self._participant_leases.clear()
        for lease in leases:
            await lease.aclose()
        await self._view.aclose()
        await self._coordinator.contract_source.aclose()
        await self._coordinator.token_source.aclose()

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


    def _validate_from_cache_locked(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
    ) -> ContractValidity:
        return self._view.validate(contract, current_sessions=current_sessions)

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
        if self._started:
            await self._view.wait_contract_revision(contract.key, contract.revision)
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
        return self._view.get_contract(parsed)

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

    async def _contract_record(self, contract: ContractHandle) -> ContractRecord | None:
        return self._view.record(contract)

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
        return self._view.contracts(
            profile,
            contract_id=contract_id,
            participant=parsed_participant,
            state=state,
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
            if self._started:
                await self._view.wait_contract_revision(
                    contract.key,
                    contract_entry.revision,
                )
        token_entry = self._coordinator.token_source.get_cached(token.key)
        if token_entry is not None:
            if self._started:
                await self._view.wait_token_revision(token.key, token_entry.revision)
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
        if self._started:
            await self._view.wait_token_revision(refreshed.key, refreshed.revision)
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
        if self._started:
            await self._view.wait_token_revision(handle.key, marker_revision)
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
                if self._started:
                    await self._view.wait_contract_revision(
                        contract.key,
                        entry.revision,
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
        validity = self._view.validate(
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
    ) -> AsyncIterator[AsyncIterator[ConcordWatchSnapshot | ConcordWatchChange]]:
        if self._started:
            await self.wait_current()
        parsed_participant = (
            parse_endpoint_address(participant)
            if participant is not None
            else None
        )
        async with self._view.subscribe(
            profile,
            participant=parsed_participant,
        ) as subscription:

            async def stream() -> AsyncIterator[
                ConcordWatchSnapshot | ConcordWatchChange
            ]:
                known = {
                    state.contract.pointer
                    for state in subscription.initial.contracts
                }
                last_current = subscription.initial.current
                yield ConcordWatchSnapshot(
                    version=subscription.initial.version,
                    current=last_current,
                    contracts=subscription.initial.contracts,
                )
                async for wakeup in subscription:
                    try:
                        snapshot = await self._view.snapshot(
                            profile,
                            participant=parsed_participant,
                        )
                    except anyio.ClosedResourceError:
                        return
                    current_pointers = {
                        state.contract.pointer for state in snapshot.contracts
                    }
                    relevant = wakeup.changed & (known | current_pointers)
                    if (
                        not wakeup.resnapshot_required
                        and not relevant
                        and snapshot.current == last_current
                    ):
                        known = current_pointers
                        continue
                    known = current_pointers
                    last_current = snapshot.current
                    yield ConcordWatchChange(
                        version=snapshot.version,
                        current=snapshot.current,
                        contracts=snapshot.contracts,
                        changed_pointers=frozenset(relevant),
                        resnapshot_required=wakeup.resnapshot_required,
                    )

            yield stream()


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
        self._state = CoalescedStateBroadcaster[ContractPointer](
            current=False,
        )
        self._lock = self._state.lock
        self._start_soon: Callable[..., object] | None = None
        self._ready = anyio.Event()
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
    async def watch(
        self,
    ) -> AsyncIterator[
        AsyncIterator[ConcordParticipantSnapshot | ConcordParticipantChange]
    ]:
        if self._started:
            await self._ready.wait()
        async with self._state.subscribe(self._participant_snapshot_locked) as subscription:

            async def stream() -> AsyncIterator[
                ConcordParticipantSnapshot | ConcordParticipantChange
            ]:
                yield subscription.initial
                async for wakeup in subscription:
                    try:
                        snapshot = await self._state.capture(
                            self._participant_snapshot_locked
                        )
                    except anyio.ClosedResourceError:
                        return
                    yield ConcordParticipantChange(
                        version=snapshot.version,
                        current=snapshot.current,
                        contracts=snapshot.contracts,
                        changed_pointers=wakeup.changed,
                        resnapshot_required=wakeup.resnapshot_required,
                    )

            yield stream()

    def _participant_snapshot_locked(
        self,
        version: int,
        current: bool,
    ) -> ConcordParticipantSnapshot:
        return ConcordParticipantSnapshot(
            version=version,
            current=current,
            contracts=self.managed_contracts,
        )

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            for lease in self._leases.values():
                await lease.aclose()
            self._leases.clear()
            self._managed.clear()
            self._last_status.clear()
            self._state.publish_locked((), current=False)
        await self._state.aclose()

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
                async with self._concord._view.subscribe(  # noqa: SLF001
                    self.profile,
                    participant=self.participant,
                    state=ContractState.OPEN,
                ) as subscription:
                    await self.reconcile(reason="contract watch warmup")
                    await self._state.publish(
                        (
                            managed.contract.pointer
                            for managed in self.managed_contracts
                        ),
                        current=subscription.initial.current,
                        resnapshot_required=True,
                    )
                    self._ready.set()
                    async for wakeup in subscription:
                        if self._closed:
                            return
                        try:
                            if wakeup.resnapshot_required:
                                await self.reconcile(reason="contract view resnapshot")
                                changed = {
                                    managed.contract.pointer
                                    for managed in self.managed_contracts
                                }
                            else:
                                changed = self._relevant_changed_pointers(
                                    wakeup.changed
                                )
                                await self._reconcile_pointers(changed)
                            await self._state.publish(
                                changed,
                                current=wakeup.current,
                                resnapshot_required=wakeup.resnapshot_required,
                            )
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
            await anyio.sleep(self._reconcile_interval)
            if self._closed:
                return
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

    def _relevant_changed_pointers(
        self,
        pointers: frozenset[ContractPointer],
    ) -> frozenset[ContractPointer]:
        relevant: set[ContractPointer] = set()
        managed_pointers = {
            managed.contract.pointer for managed in self._managed.values()
        }
        lease_pointers = {lease.contract.pointer for lease in self._leases.values()}
        for pointer in pointers:
            if pointer in managed_pointers or pointer in lease_pointers:
                relevant.add(pointer)
                continue
            contract = self._concord._view.get_contract(pointer)  # noqa: SLF001
            if contract is None:
                continue
            if self.profile is not None and contract.profile != self.profile:
                continue
            if self.participant in contract.participants:
                relevant.add(pointer)
        return frozenset(relevant)

    async def _reconcile_pointers(
        self,
        pointers: frozenset[ContractPointer],
    ) -> None:
        if not pointers:
            return
        async with self._lock:
            if self._closed:
                return
            if self._prepare_reconcile is not None:
                await _maybe_await(self._prepare_reconcile())
            for pointer in sorted(
                pointers,
                key=lambda item: (item.contract_id, item.generation),
            ):
                key = concord_contract_key(
                    contract_id=pointer.contract_id,
                    generation=pointer.generation,
                )
                contract = self._concord._view.get_contract(pointer)  # noqa: SLF001
                if contract is None:
                    await self._release_locked(
                        key,
                        reason=ContractValidityStatus.MISSING_CONTRACT.value,
                        withdraw=False,
                    )
                    continue
                managed = await self._reconcile_contract_locked(
                    contract,
                    reason="contract view change",
                )
                if managed is None:
                    if key in self._managed or key in self._leases:
                        await self._release_locked(
                            key,
                            reason="not_selected",
                            withdraw=True,
                        )
                    continue
                self._managed[key] = managed


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
        del reason
        self._state.publish_locked(
            (managed.contract.pointer,),
            current=self._concord.is_current(),
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
        del reason
        self._state.publish_locked(
            (managed.contract.pointer,),
            current=self._concord.is_current(),
        )


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value



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


__all__ = [
    "CONCORD_CONTRACT_SCHEMA_ID",
    "CONCORD_CONTRACT_BUCKET_POLICY",
    "CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID",
    "CONCORD_TOKEN_BUCKET_POLICY",
    "DEFAULT_CONCORD_CONTRACT_BUCKET_NAME",
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
    "ConcordContractState",
    "ConcordManagedContract",
    "ConcordParticipant",
    "ConcordParticipantChange",
    "ConcordParticipantSnapshot",
    "ConcordUnavailable",
    "ConcordUnavailableCode",
    "ConcordWatchChange",
    "ConcordWatchSnapshot",
    "ParticipantHandle",
    "ParticipantTokenRecord",
    "TokenObservation",
    "canonical_json_bytes",
    "canonical_json_hash",
    "concord_contract_key",
    "concord_contract_prefix",
    "concord_contracts_prefix",
    "concord_participant_token_key",
    "parse_concord_contract_key",
    "parse_concord_participant_token_key",
]
