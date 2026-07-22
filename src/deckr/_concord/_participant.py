from __future__ import annotations

import logging
import random
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
    ConcordConflictCode,
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
        self._pointer_running: set[ContractPointer] = set()
        self._pointer_dirty: set[ContractPointer] = set()
        self._retired_pointers: set[ContractPointer] = set()
        self._cleanup_contracts: dict[ContractPointer, ContractHandle] = {}
        self._cleanup_leases: dict[ContractPointer, ConcordParticipantLease] = {}
        self._cleanup_running: set[ContractPointer] = set()
        self._cleanup_wakeups: dict[ContractPointer, anyio.Event] = {}
        self._failures: dict[ContractPointer, tuple[ConcordLifecyclePhase, str]] = {}
        self._policy_failure: str | None = None
        self._state_bus = CoalescedStateBroadcaster[ContractPointer](current=False)
        self._state = ConcordParticipantState(
            version=0,
            phase=ConcordLifecyclePhase.STARTING,
            current=False,
            contracts=(),
        )
        self._tasks = ConcordTaskGroup(task_group)
        self._task_group: anyio.abc.TaskGroup | None = None
        self._selector_pending_lock = anyio.Lock()
        self._selector_pending: ConcordViewSnapshot | None = None
        self._selector_waiters: list[anyio.Event] = []
        self._selector_wakeup = anyio.Event()
        self._entered = False
        self._closed = False
        self._close_started = False
        self._close_lock = anyio.Lock()
        self._close_done = anyio.Event()
        self._enter_cancel_scope: anyio.CancelScope | None = None
        self._enter_task_id: int | None = None
        self._enter_done = anyio.Event()

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
        with anyio.CancelScope() as scope:
            self._enter_cancel_scope = scope
            self._enter_task_id = anyio.get_current_task().id
            try:
                return await self._enter()
            except BaseException:
                if not self._close_started:
                    await self.aclose()
                raise
            finally:
                self._enter_cancel_scope = None
                self._enter_done.set()
        raise anyio.ClosedResourceError

    async def _enter(self) -> ConcordParticipant:
        await self._view.wait_current()
        if self._closed:
            raise anyio.ClosedResourceError
        self._task_group = await self._tasks.start()
        await self._task_group.start(self._watch_loop)
        if self._closed:
            raise anyio.ClosedResourceError
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def cancel(
        self,
        contract: ContractHandle | ContractPointer,
        *,
        reason: str | None = None,
    ) -> bool:
        if self._closed:
            raise anyio.ClosedResourceError
        pointer = contract.pointer if isinstance(contract, ContractHandle) else contract
        managed = self.managed_contract(pointer)
        if managed is None:
            raise ValueError("Concord participant does not manage that contract")
        try:
            return await self._store.cancel(
                managed.contract,
                self.participant,
                reason=reason,
            )
        except (ConcordConflict, ConcordUnavailable) as exc:
            async with self._state_bus.lock:
                lease = self._leases.get(managed.contract.key)
                if lease is not None:
                    lease.relinquish_local_authority()
                self._managed.pop(managed.contract.key, None)
            await self._retain_cleanup(
                managed.contract,
                error=exc,
                reason=reason or "concord_participant_cancel",
                lease=lease,
            )
            await self._publish(
                (pointer,),
                phase=ConcordLifecyclePhase.UNAVAILABLE,
                current=False,
                reason=str(exc),
            )
            raise

    async def notify_policy_changed(self) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        await self._queue_selector_snapshot(
            await self._view.snapshot(
                self.profile,
                participant=self.participant,
                state=ContractState.OPEN,
            ),
            wait=True,
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
            raise anyio.ClosedResourceError

        if timeout_seconds is None:
            return await wait()
        with anyio.fail_after(timeout_seconds):
            return await wait()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                if self._close_started:
                    first = False
                else:
                    first = True
                    self._close_started = True
                    self._closed = True
                    enter_scope = self._enter_cancel_scope
                    if enter_scope is not None:
                        enter_scope.cancel()
                    wait_for_enter = (
                        enter_scope is not None
                        and self._enter_task_id != anyio.get_current_task().id
                    )
            if not first:
                await self._close_done.wait()
                return
            try:
                self._tasks.cancel()
                async with self._state_bus.lock:
                    for lease in {
                        *self._leases.values(),
                        *self._cleanup_leases.values(),
                    }:
                        lease.relinquish_local_authority()
                    self._managed.clear()
                    self._selected = frozenset()
                await self._publish(
                    (),
                    phase=ConcordLifecyclePhase.CLOSING,
                    current=False,
                )
                with anyio.move_on_after(CONCORD_LIFECYCLE_CLEANUP_SECONDS):
                    if wait_for_enter:
                        await self._enter_done.wait()
                    async with self._state_bus.lock:
                        leases = tuple(
                            {
                                *self._leases.values(),
                                *self._cleanup_leases.values(),
                            }
                        )
                        cleanup_by_pointer = {
                            lease.contract.pointer: lease.contract for lease in leases
                        }
                        cleanup_by_pointer.update(self._cleanup_contracts)
                        cleanup_contracts = tuple(cleanup_by_pointer.values())
                    self._task_group = None
                    async with anyio.create_task_group() as cleanup_group:
                        cleanup_group.start_soon(self._tasks.aclose)
                        for lease in leases:
                            cleanup_group.start_soon(lease.aclose)
                        for contract in cleanup_contracts:
                            cleanup_group.start_soon(self._close_contract, contract)
                async with self._state_bus.lock:
                    self._leases.clear()
                    self._cleanup_leases.clear()
                    self._managed.clear()
                    self._selected = frozenset()
                    self._failures.clear()
                    self._cleanup_contracts.clear()
                    self._cleanup_wakeups.clear()
                await self._publish(
                    (),
                    phase=ConcordLifecyclePhase.CLOSED,
                    current=False,
                )
                await self._state_bus.aclose()
            finally:
                self._close_done.set()

    async def _close_contract(self, contract: ContractHandle) -> None:
        try:
            await self._store.cancel(
                contract,
                self.participant,
                reason="concord_participant_context_closed",
            )
        except (ConcordConflict, ConcordUnavailable):
            logger.debug(
                "Could not cancel retained Concord contract during close",
                exc_info=True,
            )

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
            self._retired_pointers.update(
                item.contract.pointer for item in subscription.initial.contracts
            )
            await self._apply_snapshot(subscription.initial, raise_policy=True)
            self._tasks.group.start_soon(self._selector_worker)
            task_status.started()
            async for change in subscription:
                if self._closed:
                    return
                snapshot = await self._view.snapshot(
                    self.profile,
                    participant=self.participant,
                    state=ContractState.OPEN,
                )
                await self._schedule_tracked_pointer_changes(change, snapshot.current)
                await self._queue_selector_snapshot(snapshot)

    async def _schedule_tracked_pointer_changes(
        self,
        change: object,
        current: bool,
    ) -> None:
        async with self._state_bus.lock:
            tracked = {
                *self._selected,
                *(managed.contract.pointer for managed in self._managed.values()),
                *(lease.contract.pointer for lease in self._leases.values()),
                *self._cleanup_contracts,
            }
        changed = getattr(change, "changed", frozenset())
        if getattr(change, "resnapshot_required", False) or not current:
            relevant = tracked
        else:
            relevant = tracked.intersection(changed)
        async with self._state_bus.lock:
            wakeups = tuple(
                self._cleanup_wakeups[pointer]
                for pointer in relevant
                if pointer in self._cleanup_wakeups
            )
        for wakeup in wakeups:
            wakeup.set()
        for pointer in sorted(
            relevant,
            key=lambda item: (item.contract_id, item.generation),
        ):
            await self._schedule_pointer(pointer, current)

    async def _queue_selector_snapshot(
        self,
        snapshot: ConcordViewSnapshot,
        *,
        wait: bool = False,
    ) -> None:
        waiter = anyio.Event() if wait else None
        async with self._selector_pending_lock:
            if self._closed:
                if wait:
                    raise anyio.ClosedResourceError
                return
            self._selector_pending = snapshot
            if waiter is not None:
                self._selector_waiters.append(waiter)
            self._selector_wakeup.set()
        if waiter is not None:
            await waiter.wait()

    async def _selector_worker(self) -> None:
        while not self._closed:
            await self._selector_wakeup.wait()
            async with self._selector_pending_lock:
                snapshot = self._selector_pending
                waiters = tuple(self._selector_waiters)
                self._selector_pending = None
                self._selector_waiters.clear()
                self._selector_wakeup = anyio.Event()
            if snapshot is None:
                for waiter in waiters:
                    waiter.set()
                continue
            try:
                await self._apply_snapshot(snapshot)
            finally:
                for waiter in waiters:
                    waiter.set()

    async def _apply_snapshot(
        self,
        snapshot: ConcordViewSnapshot,
        *,
        raise_policy: bool = False,
    ) -> None:
        if self._closed:
            return
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
            self._policy_failure = None
            managed_pointers = {
                managed.contract.pointer for managed in self._managed.values()
            }
        pointers = candidate_pointers | previous | managed_pointers
        if raise_policy:
            async with anyio.create_task_group() as task_group:
                for pointer in sorted(
                    pointers,
                    key=lambda item: (item.contract_id, item.generation),
                ):
                    task_group.start_soon(
                        self._reconcile_and_publish,
                        pointer,
                        snapshot.current,
                    )
        else:
            for pointer in sorted(
                pointers,
                key=lambda item: (item.contract_id, item.generation),
            ):
                await self._schedule_pointer(pointer, snapshot.current)
        phase, reason = await self._aggregate_phase(snapshot.current)
        await self._publish(
            pointers,
            phase=phase,
            current=snapshot.current,
            reason=reason,
        )

    async def _schedule_pointer(
        self,
        pointer: ContractPointer,
        current: bool,
    ) -> None:
        async with self._state_bus.lock:
            if pointer in self._pointer_running:
                self._pointer_dirty.add(pointer)
                return
            self._pointer_running.add(pointer)
            task_group = self._task_group
        if task_group is not None:
            task_group.start_soon(self._pointer_worker, pointer, current)

    async def _pointer_worker(
        self,
        pointer: ContractPointer,
        current: bool,
    ) -> None:
        try:
            while not self._closed:
                await self._reconcile_and_publish(pointer, current)
                async with self._state_bus.lock:
                    if pointer not in self._pointer_dirty:
                        return
                    self._pointer_dirty.discard(pointer)
                current = self._view.is_current()
        finally:
            async with self._state_bus.lock:
                self._pointer_running.discard(pointer)
                self._pointer_dirty.discard(pointer)
                self._pointer_locks.pop(pointer, None)

    async def _reconcile_and_publish(
        self,
        pointer: ContractPointer,
        current: bool,
    ) -> None:
        await self._reconcile_pointer(pointer)
        phase, reason = await self._aggregate_phase(current)
        await self._publish(
            (pointer,),
            phase=phase,
            current=current and self._view.is_current(),
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
            contract = self._view.get_contract(pointer)
            if contract is None:
                await self._drop_pointer(key)
                return
            record = self._view.record(contract)
            if record is None:
                await self._drop_pointer(key)
                return
            if not selected:
                lease = await self._relinquish_pointer(key)
                await self._cancel_or_retain(
                    contract,
                    reason="concord_participant_policy_rejected",
                    lease=lease,
                )
                await self._drop_pointer(key)
                return
            expected_session = record.participant_sessions.get(str(self.participant))
            if expected_session is not None and expected_session != self.session_id:
                self._retired_pointers.add(pointer)
                lease = await self._relinquish_pointer(key)
                await self._cancel_or_retain(
                    contract,
                    reason="concord_managed_session_mismatch",
                    lease=lease,
                )
                await self._drop_pointer(key)
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
                await self._publish(
                    (pointer,),
                    phase=ConcordLifecyclePhase.UNAVAILABLE,
                    current=False,
                    reason=validity.reason or "Concord view is unavailable",
                )
                await self._cancel_or_retain(
                    contract,
                    reason="concord_managed_view_unavailable",
                    lease=lease,
                )
                return
            existing = validity.tokens.get(str(self.participant))
            if _terminal_status(validity.status):
                await self._relinquish_pointer(key)
                await self._cancel_terminal(contract, validity)
                await self._drop_pointer(key)
                return

            async with self._state_bus.lock:
                lease = self._leases.get(key)
            if existing is not None:
                self._retired_pointers.add(pointer)
                if (
                    lease is None
                    or lease.token is None
                    or not participant_handle_matches(lease.token, existing)
                ):
                    lease = await self._relinquish_pointer(key)
                    await self._cancel_or_retain(
                        contract,
                        reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
                        lease=lease,
                    )
                    await self._drop_pointer(key)
                    return
            if lease is None:
                if pointer in self._retired_pointers:
                    await self._cancel_or_retain(
                        contract,
                        reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
                    )
                    return
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
                    if exc.code == ConcordConflictCode.SESSION_MISMATCH:
                        await self._cancel_or_retain(
                            contract,
                            reason="concord_managed_session_mismatch",
                            lease=lease,
                        )
                    await self._drop_pointer(key)
                    return
                self._retired_pointers.add(pointer)
                self._tasks.group.start_soon(self._heartbeat, key, lease)

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
                await self._cancel_or_retain(
                    lease.contract,
                    reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
                    lease=lease,
                )
                return
            await self._relinquish_pointer(key)
            await self._cancel_or_retain(
                lease.contract,
                reason=CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
                lease=lease,
            )
            await self._drop_pointer(key)
            await self._publish(
                (lease.contract.pointer,),
                phase=ConcordLifecyclePhase.READY,
                current=self._view.is_current(),
            )

    async def _relinquish_pointer(
        self,
        key: str,
    ) -> ConcordParticipantLease | None:
        """Remove public authority before any pointer-scoped remote I/O."""

        pointer = _pointer_from_key(key)
        async with self._state_bus.lock:
            lease = self._leases.get(key)
            if lease is not None:
                lease.relinquish_local_authority()
            self._managed.pop(key, None)
        phase, reason = await self._aggregate_phase(self._view.is_current())
        await self._publish(
            (pointer,),
            phase=phase,
            current=self._view.is_current(),
            reason=reason,
        )
        return lease

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
        await self._cancel_or_retain(
            contract,
            reason=f"concord_managed_{validity.status.value}",
        )

    async def _drop_pointer(self, key: str) -> None:
        pointer = _pointer_from_key(key)
        async with self._state_bus.lock:
            self._managed.pop(key, None)
            lease = self._leases.pop(key, None)
            if pointer not in self._cleanup_contracts:
                self._failures.pop(pointer, None)
                current = self._view.get_contract(pointer)
                if current is not None and current.state != ContractState.OPEN:
                    self._retired_pointers.discard(pointer)
        if lease is not None and pointer not in self._cleanup_leases:
            await lease.aclose()

    async def _cancel_or_retain(
        self,
        contract: ContractHandle,
        *,
        reason: str,
        lease: ConcordParticipantLease | None = None,
    ) -> bool:
        try:
            await self._store.cancel(
                contract,
                self.participant,
                reason=reason,
            )
        except (ConcordConflict, ConcordUnavailable) as exc:
            await self._retain_cleanup(
                contract,
                error=exc,
                reason=reason,
                lease=lease,
            )
            return False
        return True

    async def _retain_cleanup(
        self,
        contract: ContractHandle,
        *,
        error: ConcordConflict | ConcordUnavailable,
        reason: str,
        lease: ConcordParticipantLease | None,
    ) -> None:
        pointer = contract.pointer
        async with self._state_bus.lock:
            self._cleanup_contracts[pointer] = contract
            self._cleanup_wakeups.setdefault(pointer, anyio.Event())
            if lease is not None:
                self._cleanup_leases[pointer] = lease
            self._failures[pointer] = (
                ConcordLifecyclePhase.UNAVAILABLE,
                str(error),
            )
            start_retry = pointer not in self._cleanup_running
            if start_retry:
                self._cleanup_running.add(pointer)
            task_group = self._task_group
        if lease is not None:
            lease.relinquish_local_authority()
        if start_retry and task_group is not None:
            task_group.start_soon(self._cleanup_retry_loop, pointer, reason)

    async def _cleanup_retry_loop(
        self,
        pointer: ContractPointer,
        reason: str,
    ) -> None:
        delay = 0.1
        try:
            while not self._closed:
                async with self._state_bus.lock:
                    contract = self._cleanup_contracts.get(pointer)
                    lease = self._cleanup_leases.get(pointer)
                if contract is None:
                    return
                token_clean = lease is None
                if lease is not None:
                    try:
                        await lease.withdraw_retained()
                    except (ConcordConflict, ConcordUnavailable):
                        pass
                    else:
                        token_clean = True
                try:
                    await self._store.cancel(
                        contract,
                        self.participant,
                        reason=reason,
                    )
                except ConcordConflict as exc:
                    if exc.code != ConcordConflictCode.REVISION_CHANGED:
                        async with self._state_bus.lock:
                            self._failures[pointer] = (
                                ConcordLifecyclePhase.TERMINAL,
                                str(exc),
                            )
                        return
                except ConcordUnavailable as exc:
                    async with self._state_bus.lock:
                        self._failures[pointer] = (
                            ConcordLifecyclePhase.UNAVAILABLE,
                            str(exc),
                        )
                else:
                    if token_clean:
                        async with self._state_bus.lock:
                            self._cleanup_contracts.pop(pointer, None)
                            self._cleanup_wakeups.pop(pointer, None)
                            self._failures.pop(pointer, None)
                            self._retired_pointers.discard(pointer)
                            completed_lease = self._cleanup_leases.pop(pointer, None)
                        if completed_lease is not None:
                            await completed_lease.aclose(withdraw=False)
                        await self._publish(
                            (pointer,),
                            phase=ConcordLifecyclePhase.READY,
                            current=self._view.is_current(),
                        )
                        return
                async with self._state_bus.lock:
                    wakeup = self._cleanup_wakeups.setdefault(
                        pointer,
                        anyio.Event(),
                    )
                if wakeup.is_set():
                    async with self._state_bus.lock:
                        if self._cleanup_wakeups.get(pointer) is wakeup:
                            self._cleanup_wakeups[pointer] = anyio.Event()
                    delay = 0.1
                    continue
                with anyio.move_on_after(random.uniform(delay * 0.5, delay)):
                    await wakeup.wait()
                if wakeup.is_set():
                    async with self._state_bus.lock:
                        if self._cleanup_wakeups.get(pointer) is wakeup:
                            self._cleanup_wakeups[pointer] = anyio.Event()
                    delay = 0.1
                else:
                    delay = min(delay * 2, 5.0)
        finally:
            async with self._state_bus.lock:
                self._cleanup_running.discard(pointer)

    async def _fail_policy(self, reason: str) -> None:
        async with self._state_bus.lock:
            leases = tuple(self._leases.values())
            pointers = {
                *self._selected,
                *(managed.contract.pointer for managed in self._managed.values()),
                *(lease.contract.pointer for lease in leases),
            }
            self._policy_failure = reason
            self._managed.clear()
            self._selected = frozenset()
            for lease in leases:
                lease.relinquish_local_authority()
        await self._publish(
            pointers,
            phase=ConcordLifecyclePhase.UNAVAILABLE,
            current=False,
            reason=reason,
        )
        # Policy execution happens in the one coalescing selector worker.  It
        # must never perform remote cleanup itself: one blocked contract store
        # would then prevent every other pointer from failing closed.  Emptying
        # the selected set makes the existing pointer-scoped workers cancel and
        # withdraw independently while retaining ambiguous cleanup ownership.
        for pointer in sorted(
            pointers,
            key=lambda item: (item.contract_id, item.generation),
        ):
            await self._schedule_pointer(pointer, self._view.is_current())

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
            policy_failure = self._policy_failure
            failures = tuple(self._failures.values())
        if policy_failure is not None:
            return ConcordLifecyclePhase.UNAVAILABLE, policy_failure
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
