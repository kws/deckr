from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from deckr._concord import _agreement as agreement_module
from deckr._concord import _participant as participant_module
from deckr._concord._lease import (
    ConcordParticipantLease,
    ConcordParticipantLeasePhase,
)
from deckr._concord._owner import ConcordOwnerBootstrap
from deckr.concord import (
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordConflictCode,
    ConcordLifecyclePhase,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractState,
    concord_participant_token_key,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.testing import (
    LifecycleFaultHarness,
    LifecycleFaultPhase,
    MemoryJsonKvBucket,
)

CONTROLLER = controller_address("lifecycle-guards")
MANAGER = hardware_manager_address("lifecycle-guards")
PROFILE = "dev.deckr.test.lifecycle-guards.v1"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    address: object
    session_id: str


@asynccontextmanager
async def _running_concord(
    contracts: MemoryJsonKvBucket | None = None,
    tokens: MemoryJsonKvBucket | None = None,
) -> AsyncIterator[Concord]:
    concord = Concord(
        contracts or MemoryJsonKvBucket(bucket="contracts"),
        tokens or MemoryJsonKvBucket(bucket="tokens", ttl_seconds=60),
    )
    async with anyio.create_task_group() as tasks:
        concord.start(tasks)
        await concord.wait_current()
        try:
            yield concord
        finally:
            await concord.aclose()
            tasks.cancel_scope.cancel()


def _spec(**changes) -> ConcordAgreementSpec:
    values = {
        "profile": PROFILE,
        "participants": (CONTROLLER, MANAGER),
        "participant_sessions": {str(MANAGER): "manager-session"},
    }
    values.update(changes)
    return ConcordAgreementSpec(**values)


def _accept_all(candidates) -> ConcordParticipantSelection:
    return ConcordParticipantSelection(
        candidate.contract.pointer for candidate in candidates
    )


def test_agreement_intent_rejects_invalid_identity_and_session_shapes() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ConcordAgreementSpec(profile=PROFILE, participants=())
    with pytest.raises(ValueError, match="unique"):
        ConcordAgreementSpec(
            profile=PROFILE,
            participants=(CONTROLLER, CONTROLLER),
        )
    with pytest.raises(ValueError, match="profile"):
        ConcordAgreementSpec(profile="", participants=(CONTROLLER,))
    with pytest.raises(ValueError, match="named participants"):
        _spec(participant_sessions={"service:not-a-participant": "session"})
    with pytest.raises(ValueError, match="session id"):
        _spec(participant_sessions={str(MANAGER): ""})

    pointer = ContractPointer(contractId="predecessor", generation=1)
    assert _spec(supersedes=pointer).supersedes is pointer
    assert (
        _spec(supersedes=pointer.model_dump(by_alias=True)).supersedes == pointer
    )
    assert _spec(created_by=CONTROLLER).created_by == CONTROLLER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "spec", "message"),
    [
        (
            _Endpoint(controller_address("unnamed"), "session"),
            _spec(),
            "must be named",
        ),
        (
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(created_by=MANAGER),
            "creator must match",
        ),
        (
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(
                participant_sessions={
                    str(CONTROLLER): "different-session",
                    str(MANAGER): "manager-session",
                }
            ),
            "conflicts with the endpoint session",
        ),
    ],
)
async def test_failed_agreement_entry_is_closed_without_a_contract(
    endpoint: _Endpoint,
    spec: ConcordAgreementSpec,
    message: str,
) -> None:
    async with _running_concord() as concord:
        agreement = concord.propose(endpoint, spec)
        with pytest.raises(ValueError, match=message):
            await agreement.__aenter__()
        assert agreement.closed
        with pytest.raises(RuntimeError, match="has not been entered"):
            _ = agreement.contract


@pytest.mark.asyncio
async def test_agreement_and_participant_contexts_reject_reentry_and_closed_work() -> (
    None
):
    async with _running_concord() as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(),
        )
        await agreement.__aenter__()
        with pytest.raises(RuntimeError, match="re-entered"):
            await agreement.__aenter__()
        await agreement.aclose()
        with pytest.raises(ConcordConflict) as agreement_error:
            await agreement.cancel("too-late")
        assert agreement_error.value.code == ConcordConflictCode.AGREEMENT_CLOSED

        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        await participant.__aenter__()
        with pytest.raises(RuntimeError, match="re-entered"):
            await participant.__aenter__()
        with pytest.raises(ValueError, match="does not manage"):
            await participant.cancel(
                ContractPointer(contractId="unknown", generation=1)
            )
        await participant.aclose()
        with pytest.raises(anyio.ClosedResourceError):
            await participant.notify_policy_changed()
        with pytest.raises(anyio.ClosedResourceError):
            await participant.cancel(
                ContractPointer(contractId="unknown", generation=1)
            )


@pytest.mark.asyncio
async def test_close_cancels_an_entry_blocked_before_contract_commit() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    barrier = contracts.block_next("create", phase=LifecycleFaultPhase.BEFORE)
    async with _running_concord(contracts=contracts) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(),
        )
        enter_error: list[BaseException] = []

        async def enter() -> None:
            try:
                await agreement.__aenter__()
            except BaseException as exc:
                enter_error.append(exc)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(enter)
            await barrier.wait()
            await agreement.aclose()

        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED
        assert not agreement.state.local_authority
        assert agreement.closed
        assert contracts.mutation_count == 0
        assert enter_error


@pytest.mark.asyncio
async def test_agreement_public_subscribers_close_and_heartbeat_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _spec(profile=None).profile is None
    async with _running_concord() as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(),
        )
        # Refresh is deliberately inert before a contract identity has committed.
        await agreement._refresh_from_view()  # noqa: SLF001
        await agreement.__aenter__()

        lose_authority = AsyncMock()
        monkeypatch.setattr(agreement, "_lose_authority", lose_authority)
        original_lease = agreement._lease  # noqa: SLF001

        agreement._lease = None  # noqa: SLF001
        await agreement._heartbeat_loop()  # noqa: SLF001
        for phase in (
            ConcordParticipantLeasePhase.UNAVAILABLE,
            ConcordParticipantLeasePhase.TERMINAL,
        ):
            agreement._lease = SimpleNamespace(  # noqa: SLF001
                heartbeat_loop=AsyncMock(),
                state=SimpleNamespace(phase=phase, reason=None),
            )
            await agreement._heartbeat_loop()  # noqa: SLF001
        assert lose_authority.await_count == 2

        agreement._lease = SimpleNamespace(  # noqa: SLF001
            heartbeat_loop=AsyncMock(),
            state=SimpleNamespace(
                phase=ConcordParticipantLeasePhase.ACTIVE,
                reason=None,
            ),
        )
        await agreement._heartbeat_loop()  # noqa: SLF001
        agreement._closed = True  # noqa: SLF001
        await agreement._heartbeat_loop()  # noqa: SLF001
        agreement._closed = False  # noqa: SLF001
        agreement._lease = original_lease  # noqa: SLF001

        waiter_error: list[BaseException] = []

        async def wait_for_impossible_state() -> None:
            try:
                await agreement.wait_for_state(lambda _state: False)
            except BaseException as exc:
                waiter_error.append(exc)

        async with agreement.watch_state() as states:
            assert (await anext(states)).phase == ConcordLifecyclePhase.READY
            observed = []

            async def receive_change() -> None:
                observed.append((await anext(states)).phase)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(wait_for_impossible_state)
                tasks.start_soon(receive_change)
                await anyio.sleep(0)
                await agreement.aclose()

            async for state in states:
                observed.append(state.phase)
            assert ConcordLifecyclePhase.CLOSING in observed
        assert len(waiter_error) == 1
        assert isinstance(waiter_error[0], anyio.ClosedResourceError)

        # Closed is absorbing even if a late worker attempts another publish.
        await agreement._publish(  # noqa: SLF001
            ConcordLifecyclePhase.CLOSED,
            validity=agreement.state.validity,
            local_authority=False,
        )
        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED


@pytest.mark.asyncio
async def test_agreement_cleanup_retry_classifies_conflicts_and_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agreement_module.random, "uniform", lambda _low, _high: 0)
    async with _running_concord() as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _spec(),
        )
        await agreement.__aenter__()
        original_store = agreement._store  # noqa: SLF001
        original_lease = agreement._lease  # noqa: SLF001
        cancel = AsyncMock(
            side_effect=[
                ConcordConflict(
                    ConcordConflictCode.REVISION_CHANGED,
                    "retry",
                    key=agreement.contract.key,
                ),
                ConcordUnavailable(
                    ConcordUnavailableCode.STORE_UNAVAILABLE,
                    "offline",
                ),
                True,
            ]
        )
        withdraw = AsyncMock(
            side_effect=ConcordUnavailable(
                ConcordUnavailableCode.STORE_UNAVAILABLE,
                "offline",
            )
        )
        agreement._store = SimpleNamespace(cancel=cancel)  # noqa: SLF001
        agreement._lease = SimpleNamespace(withdraw_retained=withdraw)  # noqa: SLF001
        agreement._cleanup_required = True  # noqa: SLF001
        await agreement._cleanup_retry_loop("retry-test")  # noqa: SLF001
        assert cancel.await_count == 3
        assert withdraw.await_count == 1
        assert not agreement._cleanup_required  # noqa: SLF001

        # Cleanup can also complete before a participant lease exists (the
        # contract-create/lease-install boundary), and retention coalesces while
        # one retry worker is already active.
        agreement._lease = None  # noqa: SLF001
        agreement._store = SimpleNamespace(cancel=AsyncMock(return_value=True))  # noqa: SLF001
        agreement._cleanup_required = True  # noqa: SLF001
        await agreement._cleanup_retry_loop("pre-lease-cleanup")  # noqa: SLF001
        agreement._cleanup_running = True  # noqa: SLF001
        agreement._retain_contract_cleanup("already-running")  # noqa: SLF001
        agreement._cleanup_running = False  # noqa: SLF001

        class _WakeDuringWait:
            def __init__(self, owner, *, replace_event: bool) -> None:
                self.owner = owner
                self.replace_event = replace_event
                self.checked = 0

            def is_set(self) -> bool:
                self.checked += 1
                return self.checked > 1

            async def wait(self) -> None:
                if self.replace_event:
                    self.owner._cleanup_wakeup = anyio.Event()  # noqa: SLF001

        for replace_event in (False, True):
            agreement._store = SimpleNamespace(  # noqa: SLF001
                cancel=AsyncMock(
                    side_effect=[
                        ConcordUnavailable(
                            ConcordUnavailableCode.STORE_UNAVAILABLE,
                            "offline",
                        ),
                        True,
                    ]
                )
            )
            agreement._lease = None  # noqa: SLF001
            agreement._cleanup_required = True  # noqa: SLF001
            agreement._cleanup_wakeup = _WakeDuringWait(  # type: ignore[assignment]  # noqa: SLF001
                agreement,
                replace_event=replace_event,
            )
            await agreement._cleanup_retry_loop("wakeup-race")  # noqa: SLF001

        agreement._closed = True  # noqa: SLF001
        agreement._cleanup_required = True  # noqa: SLF001
        await agreement._cleanup_retry_loop("already-closed")  # noqa: SLF001
        agreement._closed = False  # noqa: SLF001

        agreement._cleanup_required = True  # noqa: SLF001
        agreement._cleanup_wakeup = anyio.Event()  # noqa: SLF001
        agreement._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=ConcordConflict(
                    ConcordConflictCode.CONTRACT_CANCELLED,
                    "terminal",
                    key=agreement.contract.key,
                )
            )
        )
        await agreement._cleanup_retry_loop("terminal-test")  # noqa: SLF001
        assert agreement._cleanup_required  # noqa: SLF001

        agreement._store = original_store  # noqa: SLF001
        agreement._lease = original_lease  # noqa: SLF001
        agreement._cleanup_required = False  # noqa: SLF001
        await agreement.aclose()


@pytest.mark.asyncio
async def test_participant_selector_heartbeat_and_closed_state_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ConcordParticipantPolicy(profile=None, select=_accept_all).profile is None
    async with _running_concord() as concord:
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        await participant.__aenter__()

        # A coalesced wakeup with no pending snapshot still wakes every caller
        # and does not poison the selector worker.
        first = anyio.Event()
        second = anyio.Event()
        async with participant._selector_pending_lock:  # noqa: SLF001
            participant._selector_waiters.extend((first, second))  # noqa: SLF001
            participant._selector_wakeup.set()  # noqa: SLF001
        with anyio.fail_after(1):
            await first.wait()
            await second.wait()

        key = contract.key
        fake_lease = SimpleNamespace(
            contract=contract,
            heartbeat_loop=AsyncMock(),
            state=SimpleNamespace(
                phase=ConcordParticipantLeasePhase.UNAVAILABLE,
                reason=None,
            ),
        )
        participant._leases[key] = fake_lease  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr(participant, "_cancel_or_retain", AsyncMock())
        await participant._heartbeat(key, fake_lease)  # type: ignore[arg-type]  # noqa: SLF001
        assert participant.state.phase == ConcordLifecyclePhase.UNAVAILABLE

        fake_lease.state = SimpleNamespace(  # type: ignore[misc]
            phase=ConcordParticipantLeasePhase.TERMINAL,
            reason="token lost",
        )
        participant._leases[key] = fake_lease  # type: ignore[assignment]  # noqa: SLF001
        relinquish = AsyncMock()
        drop = AsyncMock()
        publish = AsyncMock()
        original_publish = participant._publish  # noqa: SLF001
        monkeypatch.setattr(participant, "_relinquish_pointer", relinquish)
        monkeypatch.setattr(participant, "_drop_pointer", drop)
        monkeypatch.setattr(participant, "_publish", publish)
        await participant._heartbeat(key, fake_lease)  # type: ignore[arg-type]  # noqa: SLF001
        relinquish.assert_any_await(key)
        drop.assert_any_await(key)

        # A heartbeat that finishes after its pointer is replaced is inert.
        participant._leases.pop(key, None)  # noqa: SLF001
        await participant._heartbeat(key, fake_lease)  # type: ignore[arg-type]  # noqa: SLF001
        fake_lease.state = SimpleNamespace(  # type: ignore[misc]
            phase=ConcordParticipantLeasePhase.ACTIVE,
            reason=None,
        )
        participant._leases[key] = fake_lease  # type: ignore[assignment]  # noqa: SLF001
        await participant._heartbeat(key, fake_lease)  # type: ignore[arg-type]  # noqa: SLF001
        participant._leases.pop(key, None)  # noqa: SLF001

        monkeypatch.setattr(participant, "_publish", original_publish)
        await participant.aclose()
        await participant.aclose()
        await participant._publish(  # noqa: SLF001
            (),
            phase=ConcordLifecyclePhase.CLOSED,
            current=False,
        )
        assert participant.state.phase == ConcordLifecyclePhase.CLOSED


@pytest.mark.asyncio
async def test_participant_cleanup_retry_retains_every_partial_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(participant_module.random, "uniform", lambda _low, _high: 0)
    async with _running_concord() as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        pointer = contract.pointer

        # A stale retry registration with no retained contract is harmless.
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        await participant._cleanup_retry_loop(pointer, "missing")  # noqa: SLF001

        class _RetainedLease:
            def __init__(self) -> None:
                self.withdraw_retained = AsyncMock(
                    side_effect=[
                        ConcordUnavailable(
                            ConcordUnavailableCode.STORE_UNAVAILABLE,
                            "token offline",
                        ),
                        True,
                    ]
                )
                self.aclose = AsyncMock()

        retained = _RetainedLease()
        participant._cleanup_contracts[pointer] = contract  # noqa: SLF001
        participant._cleanup_leases[pointer] = retained  # type: ignore[assignment]  # noqa: SLF001
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        participant._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=[
                    True,
                    True,
                ]
            )
        )

        class _WakeDuringWait:
            def __init__(self) -> None:
                self.checked = 0

            def is_set(self) -> bool:
                self.checked += 1
                return self.checked > 1

            async def wait(self) -> None:
                await anyio.lowlevel.checkpoint()

        participant._cleanup_wakeups[pointer] = _WakeDuringWait()  # type: ignore[assignment]  # noqa: SLF001
        await participant._cleanup_retry_loop(pointer, "retry")  # noqa: SLF001
        assert retained.withdraw_retained.await_count == 2
        retained.aclose.assert_awaited_once_with(withdraw=False)
        assert pointer not in participant._cleanup_contracts  # noqa: SLF001

        participant._cleanup_contracts[pointer] = contract  # noqa: SLF001
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        participant._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=ConcordConflict(
                    ConcordConflictCode.CONTRACT_CANCELLED,
                    "terminal",
                    key=contract.key,
                )
            )
        )
        await participant._cleanup_retry_loop(pointer, "terminal")  # noqa: SLF001
        assert participant._failures[pointer][0] == ConcordLifecyclePhase.TERMINAL  # noqa: SLF001

        participant._cleanup_contracts[pointer] = contract  # noqa: SLF001
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        participant._cleanup_wakeups[pointer] = anyio.Event()  # noqa: SLF001
        participant._cleanup_wakeups[pointer].set()  # noqa: SLF001
        participant._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=[
                    ConcordConflict(
                        ConcordConflictCode.REVISION_CHANGED,
                        "retry",
                        key=contract.key,
                    ),
                    True,
                ]
            )
        )
        await participant._cleanup_retry_loop(pointer, "revision-race")  # noqa: SLF001

        participant._cleanup_contracts[pointer] = contract  # noqa: SLF001
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        participant._cleanup_wakeups[pointer] = anyio.Event()  # noqa: SLF001
        participant._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=[
                    ConcordUnavailable(
                        ConcordUnavailableCode.STORE_UNAVAILABLE,
                        "offline",
                    ),
                    True,
                ]
            )
        )
        await participant._cleanup_retry_loop(pointer, "timeout-retry")  # noqa: SLF001

        class _ReplaceOnCheck:
            def is_set(self) -> bool:
                participant._cleanup_wakeups[pointer] = anyio.Event()  # noqa: SLF001
                return True

            async def wait(self) -> None:
                return None

        participant._cleanup_contracts[pointer] = contract  # noqa: SLF001
        participant._cleanup_running.add(pointer)  # noqa: SLF001
        participant._cleanup_wakeups[pointer] = _ReplaceOnCheck()  # type: ignore[assignment]  # noqa: SLF001
        participant._store = SimpleNamespace(  # noqa: SLF001
            cancel=AsyncMock(
                side_effect=[
                    ConcordConflict(
                        ConcordConflictCode.REVISION_CHANGED,
                        "retry",
                        key=contract.key,
                    ),
                    True,
                ]
            )
        )
        await participant._cleanup_retry_loop(pointer, "replaced-wakeup")  # noqa: SLF001

        participant._closed = True  # noqa: SLF001
        await participant._cleanup_retry_loop(pointer, "closed")  # noqa: SLF001
        participant._closed = False  # noqa: SLF001
        await participant.aclose()


def test_participant_rejects_invalid_internal_contract_key() -> None:
    with pytest.raises(ValueError, match="invalid Concord contract key"):
        participant_module._pointer_from_key("not-a-contract-key")


@pytest.mark.asyncio
async def test_participant_pointer_reconciliation_handles_disappearing_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _running_concord() as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        pointer = contract.pointer
        drop = AsyncMock()

        with monkeypatch.context() as patch:
            patch.setattr(participant, "_drop_pointer", drop)
            patch.setattr(participant._view, "get_contract", lambda _pointer: None)  # noqa: SLF001
            await participant._reconcile_pointer(pointer)  # noqa: SLF001
        drop.assert_awaited_once_with(contract.key)

        drop.reset_mock()
        with monkeypatch.context() as patch:
            patch.setattr(participant, "_drop_pointer", drop)
            patch.setattr(
                participant._view,  # noqa: SLF001
                "get_contract",
                lambda _pointer: contract,
            )
            patch.setattr(participant._view, "record", lambda _contract: None)  # noqa: SLF001
            await participant._reconcile_pointer(pointer)  # noqa: SLF001
        drop.assert_awaited_once_with(contract.key)

        participant._selected = frozenset((pointer,))  # noqa: SLF001
        publish = AsyncMock()
        cancel = AsyncMock()
        with monkeypatch.context() as patch:
            patch.setattr(participant, "_publish", publish)
            patch.setattr(participant, "_cancel_or_retain", cancel)
            patch.setattr(
                participant._view,  # noqa: SLF001
                "get_contract",
                lambda _pointer: contract,
            )
            patch.setattr(
                participant._view,  # noqa: SLF001
                "record",
                lambda _contract: SimpleNamespace(
                    participant_sessions={},
                ),
            )
            patch.setattr(
                participant._view,  # noqa: SLF001
                "validate",
                lambda *_args, **_kwargs: SimpleNamespace(
                    status=participant_module.ContractValidityStatus.UNAVAILABLE,
                    reason="view stale",
                    tokens={},
                ),
            )
            await participant._reconcile_pointer(pointer)  # noqa: SLF001
        publish.assert_awaited()
        cancel.assert_awaited_once()
        await participant.aclose()


@pytest.mark.asyncio
async def test_lease_guards_callbacks_and_idempotent_close() -> None:
    sync_states = []

    async with _running_concord() as concord:
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        lease = ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
            on_change=sync_states.append,
        )
        token = await lease.attach()
        assert await lease.attach() is token
        assert lease.state.phase == ConcordParticipantLeasePhase.ACTIVE
        await lease.aclose()
        await lease.aclose()
        assert lease.state.phase == ConcordParticipantLeasePhase.CLOSED
        assert sync_states[-1].phase == ConcordParticipantLeasePhase.CLOSED
        with pytest.raises(ConcordConflict) as attach_error:
            await lease.attach()
        assert attach_error.value.code == ConcordConflictCode.LEASE_CLOSED
        with pytest.raises(ConcordConflict) as refresh_error:
            await lease.refresh()
        assert refresh_error.value.code == ConcordConflictCode.LEASE_CLOSED
        await lease.fail_closed_unavailable("already closed")
        await lease.heartbeat_loop()

        async_states = []

        async def changed(state) -> None:
            async_states.append(state)

        identity_only = ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=MANAGER,
            session_id="manager-session",
            on_change=changed,
        )
        assert not await identity_only.withdraw_retained()
        identity_only._token_create_started = True  # noqa: SLF001
        assert await identity_only.withdraw_retained()
        await identity_only._publish(  # noqa: SLF001
            ConcordParticipantLeasePhase.ATTACHING,
            local_authority=False,
        )
        assert async_states[-1].phase == ConcordParticipantLeasePhase.ATTACHING
        identity_only._refresh_delay = None  # noqa: SLF001
        assert identity_only._next_heartbeat_delay() == 0  # noqa: SLF001
        identity_only._refresh_delay = 5  # noqa: SLF001
        identity_only._last_refresh_at = None  # noqa: SLF001
        assert identity_only._next_heartbeat_delay() == 5  # noqa: SLF001
        await identity_only.aclose(withdraw=False)
        previous = identity_only.state
        await identity_only._publish(  # noqa: SLF001
            ConcordParticipantLeasePhase.ACTIVE,
            local_authority=True,
        )
        assert identity_only.state is previous

        partial = ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=MANAGER,
            session_id="manager-session",
        )
        partial._owned_token = token  # noqa: SLF001
        with pytest.raises(ConcordConflict) as partial_error:
            await partial.attach()
        assert partial_error.value.code == ConcordConflictCode.REVISION_CHANGED

        inactive = ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=MANAGER,
            session_id="manager-session",
        )
        inactive._closed = True  # noqa: SLF001
        inactive_state = inactive.state
        await inactive._publish(  # noqa: SLF001
            ConcordParticipantLeasePhase.ACTIVE,
            local_authority=True,
        )
        assert inactive.state is inactive_state
        with pytest.raises(ConcordConflict) as inactive_error:
            await inactive._abort_attach_if_inactive()  # noqa: SLF001
        assert inactive_error.value.code == ConcordConflictCode.LEASE_CLOSED


@pytest.mark.asyncio
async def test_heartbeat_rechecks_authority_after_sleep(monkeypatch) -> None:
    async with _running_concord() as concord:
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        lease = ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
        )
        token = await lease.attach()

        async def close_during_sleep(_delay: float) -> None:
            lease.relinquish_local_authority()

        monkeypatch.setattr("deckr._concord._lease.anyio.sleep", close_during_sleep)
        await lease.heartbeat_loop()
        assert lease.token is None
        assert lease.owned_token is token
        await lease.aclose()


@pytest.mark.asyncio
async def test_owner_bootstrap_revision_retry_and_terminal_conflict() -> None:
    pointer = ContractPointer(contractId="stale", generation=1)
    revision_changed = ConcordConflict(
        ConcordConflictCode.REVISION_CHANGED,
        "changed",
    )
    terminal = ConcordConflict(
        ConcordConflictCode.PARTICIPANT_NOT_NAMED,
        "wrong owner",
    )
    store = SimpleNamespace(
        cancel_pointer=AsyncMock(side_effect=[revision_changed, True, terminal])
    )
    owner = ConcordOwnerBootstrap(store, SimpleNamespace())

    await owner._cancel_until_resolved(pointer, CONTROLLER)  # noqa: SLF001
    with pytest.raises(ConcordConflict) as exc_info:
        await owner._cancel_until_resolved(pointer, CONTROLLER)  # noqa: SLF001
    assert exc_info.value.code == ConcordConflictCode.PARTICIPANT_NOT_NAMED
    assert store.cancel_pointer.await_count == 3


@pytest.mark.asyncio
async def test_materialized_view_staleness_immediately_revokes_agreement() -> None:
    async with _running_concord() as concord, concord.propose(
        _Endpoint(CONTROLLER, "controller-session"),
        _spec(),
    ) as agreement:
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        await agreement.wait_for_state(lambda state: state.valid)
        async with concord._view._broadcaster.lock:  # noqa: SLF001
            concord._view._token_current = False  # noqa: SLF001
            concord._view._publish_locked(set())  # noqa: SLF001

        unavailable = await agreement.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.UNAVAILABLE,
            timeout_seconds=2,
        )
        assert not unavailable.local_authority
        assert not unavailable.valid


@pytest.mark.asyncio
async def test_participant_rejects_frozen_session_before_token_creation() -> None:
    async with _running_concord() as concord, concord.participant(
        _Endpoint(MANAGER, "current-manager-session"),
        ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
    ) as participant:
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
            participant_sessions={
                str(CONTROLLER): "controller-session",
                str(MANAGER): "different-manager-session",
            },
        )
        await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "controller-session",
            token_id="controller-token",
        )
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)
        token_key = concord_participant_token_key(
            contract_id=contract.contract_id,
            generation=contract.generation,
            participant=MANAGER,
        )
        assert (  # noqa: SLF001
            await concord._coordinator.token_exact.get_exact(token_key) is None
        )
        assert participant.managed_contracts == ()


@pytest.mark.asyncio
async def test_initial_selector_failure_cleans_up_failed_participant_entry() -> None:
    def fail(_candidates):
        raise RuntimeError("policy failed during bootstrap")

    async with _running_concord() as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=fail),
        )
        with pytest.raises(RuntimeError, match="policy failed"):
            await participant.__aenter__()
        assert participant.state.phase == ConcordLifecyclePhase.CLOSED
        assert participant.managed_contracts == ()


@pytest.mark.asyncio
async def test_materialized_view_staleness_revokes_participant_before_cleanup() -> (
    None
):
    async with _running_concord() as concord, concord.participant(
        _Endpoint(MANAGER, "manager-session"),
        ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
    ) as participant:
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            profile=PROFILE,
            created_by=CONTROLLER,
            participant_sessions={
                str(CONTROLLER): "controller-session",
                str(MANAGER): "manager-session",
            },
        )
        await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "controller-session",
            token_id="controller-token",
        )
        await participant.wait_for_state(
            lambda state: any(
                item.contract.pointer == contract.pointer
                and item.local_authority
                for item in state.contracts
            ),
            timeout_seconds=2,
        )
        async with concord._view._broadcaster.lock:  # noqa: SLF001
            concord._view._token_current = False  # noqa: SLF001
            concord._view._publish_locked(set())  # noqa: SLF001

        unavailable = await participant.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.UNAVAILABLE,
            timeout_seconds=2,
        )
        assert all(not item.local_authority for item in unavailable.contracts)


@pytest.mark.asyncio
async def test_closed_participant_selector_queue_rejects_waiters() -> None:
    async with _running_concord() as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        await participant.__aenter__()
        snapshot = await concord._view.snapshot(  # noqa: SLF001
            PROFILE,
            participant=MANAGER,
            state=ContractState.OPEN,
        )
        await participant.aclose()
        await participant._queue_selector_snapshot(snapshot)  # noqa: SLF001
        with pytest.raises(anyio.ClosedResourceError):
            await participant._queue_selector_snapshot(  # noqa: SLF001
                snapshot,
                wait=True,
            )


def test_participant_policy_requires_valid_profile_and_selector() -> None:
    with pytest.raises(ValueError, match="profile"):
        ConcordParticipantPolicy(profile="", select=_accept_all)
    with pytest.raises(TypeError, match="callable"):
        ConcordParticipantPolicy(profile=PROFILE, select=None)  # type: ignore[arg-type]
