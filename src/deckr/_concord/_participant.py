from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import anyio

from deckr._concord._agreement import ConcordEndpointSession
from deckr._concord._keys import concord_contract_key, parse_concord_contract_key
from deckr._concord._lease import (
    CONCORD_LIFECYCLE_CLEANUP_SECONDS,
    ConcordParticipantLease,
    ConcordParticipantLeasePhase,
)
from deckr._concord._models import (
    ConcordConflict,
    ConcordLifecyclePhase,
    ConcordUnavailable,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    participant_handle_matches,
    require_text,
)
from deckr._concord._store import ConcordKvStore
from deckr._concord._tasks import ConcordTaskGroup
from deckr._concord._validation import ConcordSessionAssertions
from deckr._concord._view import ConcordView, ConcordViewSnapshot
from deckr.contracts.authority import ContractPointer
from deckr.core.util.anyio import CoalescedStateBroadcaster

CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON = (
    "concord_managed_lost_participant_token"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConcordParticipantCandidate:
    contract: ContractHandle
    record: ContractRecord
    validity: ContractValidity
    managed: bool


@dataclass(frozen=True, slots=True)
class ConcordParticipantSelection:
    accepted: frozenset[ContractPointer]

    def __init__(self, accepted: Collection[ContractPointer]) -> None:
        object.__setattr__(self, "accepted", frozenset(accepted))


class ConcordParticipantSelector(Protocol):
    def __call__(
        self,
        candidates: tuple[ConcordParticipantCandidate, ...],
    ) -> ConcordParticipantSelection | Awaitable[ConcordParticipantSelection]: ...


@dataclass(frozen=True, slots=True)
class ConcordParticipantPolicy:
    profile: str | None
    select: ConcordParticipantSelector

    def __post_init__(self) -> None:
        if self.profile is not None:
            require_text(self.profile, field_name="Concord participant profile")
        if not callable(self.select):
            raise TypeError("Concord participant selector must be callable")


@dataclass(frozen=True, slots=True)
class ConcordManagedContract:
    contract: ContractHandle
    record: ContractRecord
    validity: ContractValidity
    local_authority: bool


@dataclass(frozen=True, slots=True)
class ConcordParticipantState:
    version: int
    phase: ConcordLifecyclePhase
    current: bool
    contracts: tuple[ConcordManagedContract, ...]
    reason: str | None = None


ConcordParticipantStatePredicate = Callable[[ConcordParticipantState], bool]


class ConcordParticipant:
    """Watch-driven owner of one endpoint's selected Concord tokens."""

    def __init__(
        self,
        store: ConcordKvStore,
        view: ConcordView,
        endpoint: ConcordEndpointSession,
        policy: ConcordParticipantPolicy,
        task_group: anyio.abc.TaskGroup | None,
    ) -> None:
        self._store = store
        self._view = view
        self._endpoint = endpoint
        self.policy = policy
        self.participant = endpoint.address
        self.session_id = endpoint.session_id
        self.profile = policy.profile
        self._managed: dict[str, ConcordManagedContract] = {}
        self._leases: dict[str, ConcordParticipantLease] = {}
        self._selected: frozenset[ContractPointer] = frozenset()
        self._pointer_locks: dict[ContractPointer, anyio.Lock] = {}
        self._failures: dict[ContractPointer, tuple[ConcordLifecyclePhase, str]] = {}
        self._state_bus = CoalescedStateBroadcaster[ContractPointer](current=False)
        self._state = ConcordParticipantState(
            version=0,
            phase=ConcordLifecyclePhase.STARTING,
            current=False,
            contracts=(),
        )
        self._tasks = ConcordTaskGroup(task_group)
        self._task_group: anyio.abc.TaskGroup | None = None
        self._selector_lock = anyio.Lock()
        self._entered = False
        self._closed = False

    @property
    def state(self) -> ConcordParticipantState:
        return self._state

    @property
    def managed_contracts(self) -> tuple[ConcordManagedContract, ...]:
        return self._state.contracts

    def managed_contract(
        self,
        contract: ContractHandle | ContractPointer,
    ) -> ConcordManagedContract | None:
        pointer = contract.pointer if isinstance(contract, ContractHandle) else contract
        return next(
            (
                managed
                for managed in self._state.contracts
                if managed.contract.pointer == pointer
            ),
            None,
        )

    async def __aenter__(self) -> ConcordParticipant:
        if self._entered:
            raise RuntimeError("Concord participant contexts cannot be re-entered")
        self._entered = True
        try:
            await self._view.wait_current()
            self._task_group = await self._tasks.start()
            await self._task_group.start(self._watch_loop)
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def cancel(
        self,
        contract: ContractHandle | ContractPointer,
        *,
        reason: str | None = None,
    ) -> bool:
        pointer = contract.pointer if isinstance(contract, ContractHandle) else contract
        managed = self.managed_contract(pointer)
        if managed is None:
            raise ValueError("Concord participant does not manage that contract")
        return await self._store.cancel(
            managed.contract,
            self.participant,
            reason=reason,
        )

    async def notify_policy_changed(self) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        await self._apply_snapshot(
            await self._view.snapshot(
                self.profile,
                participant=self.participant,
                state=ContractState.OPEN,
            )
        )

    @asynccontextmanager
    async def watch_state(
        self,
    ) -> AsyncIterator[AsyncIterator[ConcordParticipantState]]:
        async with self._state_bus.subscribe(
            lambda _version, _current: self._state
        ) as subscription:

            async def stream() -> AsyncIterator[ConcordParticipantState]:
                yield subscription.initial
                async for _change in subscription:
                    try:
                        yield await self._state_bus.capture(
                            lambda _version, _current: self._state
                        )
                    except anyio.ClosedResourceError:
                        return

            yield stream()

    async def wait_for_state(
        self,
        predicate: ConcordParticipantStatePredicate,
        *,
        timeout_seconds: float | None = None,
    ) -> ConcordParticipantState:
        async def wait() -> ConcordParticipantState:
            async with self._state_bus.subscribe(
                lambda _version, _current: self._state
            ) as subscription:
                state = subscription.initial
                if predicate(state):
                    return state
                async for _change in subscription:
                    state = await self._state_bus.capture(
                        lambda _version, _current: self._state
                    )
                    if predicate(state):
                        return state
                    if state.phase == ConcordLifecyclePhase.CLOSED:
                        raise anyio.ClosedResourceError
            raise anyio.ClosedResourceError

        if timeout_seconds is None:
            return await wait()
        with anyio.fail_after(timeout_seconds):
            return await wait()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._publish(
            (),
            phase=ConcordLifecyclePhase.CLOSING,
            current=False,
        )
        task_group = self._task_group
        self._task_group = None
        if task_group is not None:
            await self._tasks.aclose()

        async with self._state_bus.lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
            self._managed.clear()
            self._selected = frozenset()
            self._failures.clear()
        with anyio.move_on_after(
            CONCORD_LIFECYCLE_CLEANUP_SECONDS,
            shield=True,
        ):
            async with anyio.create_task_group() as cleanup_group:
                for lease in leases:
                    cleanup_group.start_soon(lease.aclose)
        await self._publish(
            (),
            phase=ConcordLifecyclePhase.CLOSED,
            current=False,
        )
        await self._state_bus.aclose()

    async def _watch_loop(
        self,
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        async with self._view.subscribe(
            self.profile,
            participant=self.participant,
            state=ContractState.OPEN,
        ) as subscription:
            await self._apply_snapshot(subscription.initial, raise_policy=True)
            task_status.started()
            async for _change in subscription:
                if self._closed:
                    return
                await self._apply_snapshot(
                    await self._view.snapshot(
                        self.profile,
                        participant=self.participant,
                        state=ContractState.OPEN,
                    )
                )

    async def _apply_snapshot(
        self,
        snapshot: ConcordViewSnapshot,
        *,
        raise_policy: bool = False,
    ) -> None:
        if self._closed:
            return
        async with self._selector_lock:
            async with self._state_bus.lock:
                managed_keys = frozenset(self._managed)
            candidates = tuple(
                ConcordParticipantCandidate(
                    contract=item.contract,
                    record=item.record,
                    validity=self._view.validate(
                        item.contract,
                        current_sessions=self._session_assertions(),
                    ),
                    managed=item.contract.key in managed_keys,
                )
                for item in snapshot.contracts
            )
            try:
                selected = self.policy.select(candidates)
                if hasattr(selected, "__await__"):
                    selected = await selected
                if not isinstance(selected, ConcordParticipantSelection):
                    raise TypeError(
                        "Concord participant selector must return "
                        "ConcordParticipantSelection"
                    )
                candidate_pointers = {
                    candidate.contract.pointer for candidate in candidates
                }
                unknown = selected.accepted - candidate_pointers
                if unknown:
                    raise ValueError(
                        "Concord participant selector returned unknown pointers: "
                        f"{sorted((item.contract_id, item.generation) for item in unknown)}"
                    )
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                await self._fail_policy(str(exc))
                if raise_policy:
                    raise
                return

            async with self._state_bus.lock:
                previous = self._selected
                self._selected = selected.accepted
                managed_pointers = {
                    managed.contract.pointer for managed in self._managed.values()
                }
            pointers = candidate_pointers | previous | managed_pointers
            async with anyio.create_task_group() as task_group:
                for pointer in sorted(
                    pointers,
                    key=lambda item: (item.contract_id, item.generation),
                ):
                    task_group.start_soon(self._reconcile_pointer, pointer)
            async with self._state_bus.lock:
                retained_pointers = (
                    candidate_pointers
                    | self._selected
                    | {
                        managed.contract.pointer
                        for managed in self._managed.values()
                    }
                    | set(self._failures)
                )
                self._pointer_locks = {
                    pointer: lock
                    for pointer, lock in self._pointer_locks.items()
                    if pointer in retained_pointers
                }
            phase, reason = await self._aggregate_phase(snapshot.current)
            await self._publish(
                pointers,
                phase=phase,
                current=snapshot.current,
                reason=reason,
            )

    async def _reconcile_pointer(self, pointer: ContractPointer) -> None:
        lock = self._pointer_locks.setdefault(pointer, anyio.Lock())
        async with lock:
            if self._closed:
                return
            key = concord_contract_key(
                contract_id=pointer.contract_id,
                generation=pointer.generation,
            )
            async with self._state_bus.lock:
                selected = pointer in self._selected
                managed_here = key in self._managed
            if not selected:
                await self._drop_pointer(
                    key,
                    cancel=managed_here,
                    reason="concord_participant_policy_rejected",
                )
                return
            contract = self._view.get_contract(pointer)
            if contract is None:
                await self._drop_pointer(key, cancel=False, reason="contract_missing")
                return
            record = self._view.record(contract)
            if record is None:
                await self._drop_pointer(key, cancel=False, reason="contract_invalid")
                return
            validity = self._view.validate(
                contract,
                current_sessions=self._session_assertions(),
            )
            if validity.status == ContractValidityStatus.UNAVAILABLE:
                async with self._state_bus.lock:
                    lease = self._leases.get(key)
                    self._managed.pop(key, None)
                    self._failures[pointer] = (
                        ConcordLifecyclePhase.UNAVAILABLE,
                        validity.reason or "Concord view is unavailable",
                    )
                if lease is not None:
                    await lease.fail_closed_unavailable(
                        validity.reason or "Concord view is unavailable"
                    )
                return
            existing = validity.tokens.get(str(self.participant))
            if _terminal_status(validity.status):
                await self._cancel_terminal(contract, validity)
                await self._drop_pointer(key, cancel=False, reason=validity.status.value)
                return

            async with self._state_bus.lock:
                lease = self._leases.get(key)
            if existing is not None:
                if (
                    lease is None
                    or lease.token is None
                    or not participant_handle_matches(lease.token, existing)
                ):
                    await self._cancel_lost_token(contract)
                    await self._drop_pointer(
                        key,
                        cancel=False,
                        reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
                    )
                    return
            if lease is None:
                lease = ConcordParticipantLease(
                    self._store,
                    self._view,
                    contract=contract,
                    participant=self.participant,
                    session_id=self.session_id,
                )
                async with self._state_bus.lock:
                    self._leases[key] = lease
                try:
                    await lease.attach()
                except ConcordUnavailable as exc:
                    async with self._state_bus.lock:
                        self._failures[pointer] = (
                            ConcordLifecyclePhase.UNAVAILABLE,
                            str(exc),
                        )
                    return
                except ConcordConflict as exc:
                    await self._drop_pointer(key, cancel=False, reason=str(exc))
                    return
                task_group = self._task_group
                if task_group is not None:
                    task_group.start_soon(self._heartbeat, key, lease)

            validity = self._view.validate(
                contract,
                current_sessions=self._session_assertions(),
            )
            record = validity.contract or record
            async with self._state_bus.lock:
                self._managed[key] = ConcordManagedContract(
                    contract=contract,
                    record=record,
                    validity=validity,
                    local_authority=lease.token is not None,
                )
                self._failures.pop(pointer, None)

    async def _heartbeat(self, key: str, lease: ConcordParticipantLease) -> None:
        await lease.heartbeat_loop()
        async with self._state_bus.lock:
            active = not self._closed and self._leases.get(key) is lease
        if not active:
            return
        if lease.state.phase in {
            ConcordParticipantLeasePhase.UNAVAILABLE,
            ConcordParticipantLeasePhase.TERMINAL,
        }:
            await self._cancel_lost_token(lease.contract)
            if lease.state.phase == ConcordParticipantLeasePhase.UNAVAILABLE:
                reason = (
                    lease.state.reason
                    or CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON
                )
                async with self._state_bus.lock:
                    self._managed.pop(key, None)
                    self._failures[lease.contract.pointer] = (
                        ConcordLifecyclePhase.UNAVAILABLE,
                        reason,
                    )
                await self._publish(
                    (lease.contract.pointer,),
                    phase=ConcordLifecyclePhase.UNAVAILABLE,
                    current=False,
                    reason=reason,
                )
                return
            await self._drop_pointer(
                key,
                cancel=False,
                reason=lease.state.reason
                or CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
            )
            await self._publish(
                (lease.contract.pointer,),
                phase=ConcordLifecyclePhase.READY,
                current=self._view.is_current(),
            )

    async def _cancel_terminal(
        self,
        contract: ContractHandle,
        validity: ContractValidity,
    ) -> None:
        if validity.status not in {
            ContractValidityStatus.INVALID_TOKEN,
            ContractValidityStatus.MISSING_TOKEN,
            ContractValidityStatus.GENERATION_MISMATCH,
            ContractValidityStatus.SESSION_MISMATCH,
            ContractValidityStatus.TERMS_HASH_MISMATCH,
        }:
            return
        try:
            await self._store.cancel(
                contract,
                self.participant,
                reason=f"concord_managed_{validity.status.value}",
            )
        except (ConcordConflict, ConcordUnavailable):
            logger.debug("Could not cancel terminal Concord contract", exc_info=True)

    async def _cancel_lost_token(self, contract: ContractHandle) -> None:
        try:
            await self._store.cancel(
                contract,
                self.participant,
                reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
            )
        except (ConcordConflict, ConcordUnavailable):
            logger.debug(
                "Could not cancel Concord contract after local token loss",
                exc_info=True,
            )

    async def _drop_pointer(
        self,
        key: str,
        *,
        cancel: bool,
        reason: str,
    ) -> None:
        pointer = _pointer_from_key(key)
        async with self._state_bus.lock:
            managed = self._managed.pop(key, None)
            lease = self._leases.pop(key, None)
            self._failures.pop(pointer, None)
        if cancel and managed is not None:
            try:
                await self._store.cancel(
                    managed.contract,
                    self.participant,
                    reason=reason,
                )
            except (ConcordConflict, ConcordUnavailable):
                logger.debug("Could not cancel released Concord contract", exc_info=True)
        if lease is not None:
            await lease.aclose(withdraw=not cancel)

    async def _fail_policy(self, reason: str) -> None:
        async with self._state_bus.lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
            self._managed.clear()
            self._selected = frozenset()
            self._failures.clear()
        for lease in leases:
            await lease.aclose()
        await self._publish(
            (),
            phase=ConcordLifecyclePhase.UNAVAILABLE,
            current=False,
            reason=reason,
        )

    def _session_assertions(self) -> ConcordSessionAssertions:
        return ConcordSessionAssertions.from_mapping(
            {str(self.participant): self.session_id}
        )

    async def _aggregate_phase(
        self,
        current: bool,
    ) -> tuple[ConcordLifecyclePhase, str | None]:
        if not current:
            return ConcordLifecyclePhase.UNAVAILABLE, "Concord view is unavailable"
        async with self._state_bus.lock:
            failures = tuple(self._failures.values())
        if failures:
            phase, reason = failures[0]
            return phase, reason
        return ConcordLifecyclePhase.READY, None

    async def _publish(
        self,
        changed: Collection[ContractPointer],
        *,
        phase: ConcordLifecyclePhase,
        current: bool,
        reason: str | None = None,
    ) -> None:
        async with self._state_bus.lock:
            if self._state.phase == ConcordLifecyclePhase.CLOSED:
                return
            if self._closed and phase not in {
                ConcordLifecyclePhase.CLOSING,
                ConcordLifecyclePhase.CLOSED,
            }:
                return
            version = self._state_bus.version + 1
            contracts = tuple(self._managed[key] for key in sorted(self._managed))
            state = ConcordParticipantState(
                version=version,
                phase=phase,
                current=current,
                contracts=contracts,
                reason=reason,
            )
            self._state = state
            self._state_bus.publish_locked(changed, current=current)


def _terminal_status(status: ContractValidityStatus) -> bool:
    return status not in {
        ContractValidityStatus.VALID,
        ContractValidityStatus.NOT_YET_FULFILLED,
        ContractValidityStatus.UNAVAILABLE,
    }


def _pointer_from_key(key: str) -> ContractPointer:
    parsed = parse_concord_contract_key(key)
    if parsed is None:
        raise ValueError(f"invalid Concord contract key: {key!r}")
    contract_id, generation = parsed
    return ContractPointer(contractId=contract_id, generation=generation)


__all__ = [
    "CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON",
    "ConcordManagedContract",
    "ConcordParticipant",
    "ConcordParticipantCandidate",
    "ConcordParticipantPolicy",
    "ConcordParticipantSelection",
    "ConcordParticipantState",
]
