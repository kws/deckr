from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio

from deckr._authority_buckets import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
)
from deckr._concord._agreement import (
    ConcordAgreementLease,
    ConcordAgreementSpec,
    ConcordAgreementState,
    ConcordEndpointSession,
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
    ConcordLifecyclePhase,
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
    require_text as _require_text,
)
from deckr._concord._participant import (
    ConcordManagedContract,
    ConcordParticipant,
    ConcordParticipantCandidate,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
    ConcordParticipantState,
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
from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import (
    NatsKvMaterializedBucket,
)

CONCORD_REFRESH_UNAVAILABLE_CANCEL_REASON = "participant_token_refresh_unavailable"
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
        await self._view.aclose()
        await self._coordinator.contract_source.aclose()
        await self._coordinator.token_source.aclose()

    def propose(
        self,
        endpoint: ConcordEndpointSession,
        spec: ConcordAgreementSpec,
    ) -> ConcordAgreementLease:
        """Return an async context owning one fresh agreement lifecycle."""

        return ConcordAgreementLease(
            self._coordinator,
            self._view,
            endpoint,
            spec,
            self._task_group,
        )

    def participant(
        self,
        endpoint: ConcordEndpointSession,
        policy: ConcordParticipantPolicy,
    ) -> ConcordParticipant:
        """Return an async context owning one endpoint's selected contracts."""

        return ConcordParticipant(
            self._coordinator,
            self._view,
            endpoint,
            policy,
            self._task_group,
        )


    def _validate_from_cache_locked(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
    ) -> ContractValidity:
        return self._view.validate(contract, current_sessions=current_sessions)

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


__all__ = [
    "CONCORD_CONTRACT_SCHEMA_ID",
    "CONCORD_CONTRACT_BUCKET_POLICY",
    "CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID",
    "CONCORD_TOKEN_BUCKET_POLICY",
    "DEFAULT_CONCORD_CONTRACT_BUCKET_NAME",
    "DEFAULT_CONCORD_TOKEN_BUCKET_NAME",
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
    "ConcordAgreementState",
    "ConcordConflict",
    "ConcordConflictCode",
    "ConcordContractState",
    "ConcordLifecyclePhase",
    "ConcordManagedContract",
    "ConcordParticipant",
    "ConcordParticipantCandidate",
    "ConcordParticipantPolicy",
    "ConcordParticipantSelection",
    "ConcordParticipantState",
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
