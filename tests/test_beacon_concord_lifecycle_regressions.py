from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import anyio
import pytest

import deckr._concord._lease as lease_module
import deckr.beacon as beacon_module
from deckr.beacon import (
    AdvertisementRecord,
    Beacon,
    BeaconAdvertisementSpec,
)
from deckr.concord import (
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordConflictCode,
    ConcordLifecyclePhase,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
    ConcordUnavailable,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
    concord_participant_token_key,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvConflict, KvUnavailable
from deckr.testing import MemoryJsonKvBucket

PROFILE = "dev.deckr.test.lifecycle-regression.v1"
CONTROLLER = controller_address("controller-main")
MANAGER = hardware_manager_address("manager-main")
PARTICIPANTS = (CONTROLLER, MANAGER)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    address: object
    session_id: str


def _accept_all(candidates) -> ConcordParticipantSelection:
    return ConcordParticipantSelection(
        candidate.contract.pointer for candidate in candidates
    )


class _FailingUpdateBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str, ttl_seconds: float | None = None) -> None:
        super().__init__(bucket=bucket, ttl_seconds=ttl_seconds)
        self.fail_updates = False

    async def update(self, *args, **kwargs):
        if self.fail_updates:
            raise KvUnavailable("broker unavailable")
        return await super().update(*args, **kwargs)


class _RacingTokenBucket(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.raced = False

    async def update(self, key, value, *, revision, ttl=None):
        if not self.raced and ".participants." in key:
            self.raced = True
            current = await self.get(key)
            assert current is not None
            token = ParticipantTokenRecord.model_validate(current.value)
            bumped = token.model_copy(update={"refresh_seq": token.refresh_seq + 1})
            await super().update(
                key,
                bumped,
                revision=current.revision,
                ttl=ttl,
            )
        return await super().update(
            key,
            value,
            revision=revision,
            ttl=ttl,
        )


class _AdvancingAttachConflictBucket(MemoryJsonKvBucket):
    def __init__(self, conflicts: int) -> None:
        super().__init__(bucket="contracts")
        self.conflicts = conflicts
        self.attach_attempts = 0

    async def update(self, key, value, *, revision, ttl=None):
        attached = getattr(value, "attached_participants", ())
        if attached:
            self.attach_attempts += 1
            if self.conflicts:
                current = await self.get(key)
                assert current is not None
                await super().update(
                    key,
                    current.value,
                    revision=current.revision,
                    ttl=ttl,
                )
                self.conflicts -= 1
                raise KvConflict("forced advancing attach conflict")
        return await super().update(
            key,
            value,
            revision=revision,
            ttl=ttl,
        )


class _UnavailableTokenDeleteBucket(MemoryJsonKvBucket):
    async def delete(self, key, *, revision=None):
        if ".participants." in key:
            raise KvUnavailable("token delete unavailable")
        return await super().delete(key, revision=revision)


class _ConflictingTokenDeleteBucket(MemoryJsonKvBucket):
    async def delete(self, key, *, revision=None):
        if ".participants." in key:
            raise KvConflict("token changed before exact withdrawal")
        return await super().delete(key, revision=revision)


class _BlockingTokenCreateBucket(MemoryJsonKvBucket):
    def __init__(self, blocked_contract_id: str) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.blocked_contract_id = blocked_contract_id
        self.blocked = anyio.Event()

    async def create(self, key, value, *, ttl=None):
        if f".{self.blocked_contract_id}." in key:
            self.blocked.set()
            await anyio.sleep_forever()
        return await super().create(key, value, ttl=ttl)


@asynccontextmanager
async def _running_beacon(
    raw: MemoryJsonKvBucket,
) -> AsyncIterator[Beacon]:
    beacon = Beacon(raw)
    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_current()
        try:
            yield beacon
        finally:
            task_group.cancel_scope.cancel()


@asynccontextmanager
async def _running_concord(
    contracts: MemoryJsonKvBucket,
    tokens: MemoryJsonKvBucket,
) -> AsyncIterator[Concord]:
    concord = Concord(contracts, tokens)
    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        try:
            yield concord
        finally:
            task_group.cancel_scope.cancel()


async def _create_contract(
    concord: Concord,
    contract_id: str,
) -> ContractHandle:
    return await concord._create_contract(  # noqa: SLF001
        PARTICIPANTS,
        contract_id=contract_id,
        profile=PROFILE,
        terms={"contract": contract_id},
        created_by=CONTROLLER,
    )


@pytest.mark.asyncio
async def test_concord_close_cancels_view_workers_blocked_on_state_lock() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    concord = Concord(contracts, tokens)

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        lock = concord._view._broadcaster.lock  # noqa: SLF001
        await lock.acquire()
        try:
            await tokens.put("malformed-token-change", {})
            with anyio.fail_after(1):
                while lock.statistics().tasks_waiting == 0:
                    await anyio.sleep(0)

            closed = anyio.Event()

            async def close() -> None:
                await concord.aclose()
                closed.set()

            task_group.start_soon(close)
            await anyio.sleep(0)
        finally:
            lock.release()

        with anyio.fail_after(1):
            await closed.wait()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_agreement_partial_attach_retries_only_after_view_progress() -> None:
    contracts = _AdvancingAttachConflictBucket(conflicts=2)
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        ) as agreement,
    ):
        assert contracts.attach_attempts == 3
        assert agreement.state.local_authority
        assert agreement._lease is not None  # noqa: SLF001
        assert agreement._lease.owned_token is not None  # noqa: SLF001
        assert agreement._lease.token is agreement._lease.owned_token  # noqa: SLF001


@pytest.mark.asyncio
async def test_agreement_partial_attach_exhaustion_withdraws_exact_token() -> None:
    contracts = _AdvancingAttachConflictBucket(conflicts=8)
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        )
        await agreement.__aenter__()
        try:
            assert contracts.attach_attempts == 8
            assert agreement.state.phase == ConcordLifecyclePhase.TERMINAL
            assert not agreement.state.local_authority
            assert agreement._lease is not None  # noqa: SLF001
            assert agreement._lease.owned_token is None  # noqa: SLF001
            token_key = concord_participant_token_key(
                contract_id=agreement.contract_id,
                generation=agreement.generation,
                participant=CONTROLLER,
            )
            assert await tokens.get(token_key) is None
        finally:
            await agreement.aclose()


@pytest.mark.asyncio
async def test_unavailable_partial_cleanup_cancels_and_retains_handle_until_close() -> (
    None
):
    contracts = _AdvancingAttachConflictBucket(conflicts=8)
    tokens = _UnavailableTokenDeleteBucket(
        bucket="tokens",
        ttl_seconds=120,
    )

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        )
        await agreement.__aenter__()
        lease = agreement._lease  # noqa: SLF001
        assert lease is not None
        assert agreement.state.phase == ConcordLifecyclePhase.UNAVAILABLE
        assert not agreement.state.local_authority
        assert lease.owned_token is not None
        record = await concord.contract_record(agreement.contract)
        assert record is not None
        assert record.state == ContractState.CANCELLED

        await agreement.aclose()
        await agreement.aclose()

        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED
        assert lease.owned_token is None


@pytest.mark.asyncio
async def test_conflicted_partial_cleanup_retains_handle_after_fallback_cancel() -> (
    None
):
    contracts = _AdvancingAttachConflictBucket(conflicts=8)
    tokens = _ConflictingTokenDeleteBucket(
        bucket="tokens",
        ttl_seconds=120,
    )

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        )
        await agreement.__aenter__()
        lease = agreement._lease  # noqa: SLF001
        assert lease is not None
        assert agreement.state.phase == ConcordLifecyclePhase.TERMINAL
        assert not agreement.state.local_authority
        assert lease.owned_token is not None
        assert await tokens.get(lease.owned_token.key) is not None
        record = await concord.contract_record(agreement.contract)
        assert record is not None
        assert record.state == ContractState.CANCELLED

        await agreement.aclose()

        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED
        assert lease.owned_token is None


@pytest.mark.asyncio
async def test_failed_agreement_enter_closes_state_and_waits_fail_after_close() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(controller_address("other"), "other-session"),
            _agreement_spec(),
        )
        with pytest.raises(ValueError, match="endpoint must be named"):
            await agreement.__aenter__()
        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED
        with pytest.raises(anyio.ClosedResourceError):
            await agreement.wait_for_state(lambda _state: False)


@pytest.mark.asyncio
async def test_agreement_state_watch_is_current_first_monotonic_and_times_out() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        )
        await agreement.__aenter__()
        async with agreement.watch_state() as states:
            initial = await anext(states)
            assert initial == agreement.state
            with pytest.raises(TimeoutError):
                await agreement.wait_for_state(
                    lambda state: state.valid,
                    timeout_seconds=0.01,
                )
            await agreement.cancel("test terminal transition")
            terminal = await agreement.wait_for_state(
                lambda state: state.phase == ConcordLifecyclePhase.TERMINAL
            )
            assert terminal.version > initial.version

        await agreement.aclose()
        assert agreement.state.phase == ConcordLifecyclePhase.CLOSED
        assert agreement.state.version > terminal.version
        assert not agreement.state.current
        assert not agreement.state.local_authority
        assert not agreement.state.valid


@pytest.mark.asyncio
async def test_agreement_terminal_state_cannot_return_to_ready() -> None:
    contracts = _AdvancingAttachConflictBucket(conflicts=8)
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        )
        await agreement.__aenter__()
        assert agreement.state.phase == ConcordLifecyclePhase.TERMINAL

        entry = await contracts.get(agreement.contract.key)
        assert entry is not None
        updated = await contracts.update(
            entry.key,
            entry.value,
            revision=entry.revision,
        )
        await concord._view.wait_contract_revision(  # noqa: SLF001
            entry.key,
            updated.revision,
        )
        await anyio.sleep(0)

        assert agreement.state.phase == ConcordLifecyclePhase.TERMINAL
        assert not agreement.state.valid
        await agreement.aclose()


@pytest.mark.asyncio
async def test_blocked_participant_selector_does_not_block_bounded_shutdown() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    selector_started = anyio.Event()
    control = {"blocked": False}

    async def selector(candidates) -> ConcordParticipantSelection:
        if control["blocked"]:
            selector_started.set()
            await anyio.sleep_forever()
        return _accept_all(candidates)

    async with _running_concord(contracts, tokens) as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=selector),
        )
        await participant.__aenter__()
        assert concord._view._broadcaster.subscription_count == 1  # noqa: SLF001

        control["blocked"] = True
        contract = await _create_contract(concord, "blocked-selector")
        with anyio.fail_after(1):
            await selector_started.wait()
        with anyio.fail_after(1):
            await participant.aclose()

        assert participant.state.phase == ConcordLifecyclePhase.CLOSED
        assert concord._view._broadcaster.subscription_count == 0  # noqa: SLF001
        with pytest.raises(anyio.ClosedResourceError):
            await participant.cancel(contract)


@pytest.mark.asyncio
async def test_blocked_selector_does_not_hide_existing_pointer_loss() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    selector_started = anyio.Event()
    selector_release = anyio.Event()
    control = {"blocked": False}

    async def selector(candidates) -> ConcordParticipantSelection:
        if control["blocked"]:
            selector_started.set()
            await selector_release.wait()
        return _accept_all(candidates)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=selector),
        ) as participant,
    ):
        existing = await _create_contract(concord, "existing-pointer")
        with anyio.fail_after(1):
            await participant.wait_for_state(
                lambda state: any(
                    managed.contract.pointer == existing.pointer
                    for managed in state.contracts
                )
            )

        control["blocked"] = True
        await _create_contract(concord, "blocks-selector")
        with anyio.fail_after(1):
            await selector_started.wait()

        await concord.cancel(
            existing,
            participant=CONTROLLER,
            reason="existing pointer cancelled",
        )
        with anyio.fail_after(1):
            await participant.wait_for_state(
                lambda state: all(
                    managed.contract.pointer != existing.pointer
                    for managed in state.contracts
                )
            )
        selector_release.set()


@pytest.mark.asyncio
async def test_slow_participant_subscriber_cannot_block_public_state() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        ) as participant,
        participant.watch_state() as unread,
        participant.watch_state() as observed,
    ):
        await anext(unread)
        await anext(observed)
        contract = await _create_contract(concord, "subscriber-isolation")
        with anyio.fail_after(1):
            while True:
                state = await anext(observed)
                if any(
                    managed.contract.pointer == contract.pointer
                    for managed in state.contracts
                ):
                    break
        assert participant.managed_contract(contract) is not None


@pytest.mark.asyncio
async def test_participant_rejects_malformed_selection_and_cleans_failed_entry() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=lambda _items: ()),
        )

        with pytest.raises(TypeError, match="ConcordParticipantSelection"):
            await participant.__aenter__()

        assert participant.state.phase == ConcordLifecyclePhase.CLOSED
        assert concord._view._broadcaster.subscription_count == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_participant_rejects_unknown_selection_and_cleans_failed_entry() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        await _create_contract(concord, "known")
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(
                profile=PROFILE,
                select=lambda _items: ConcordParticipantSelection(
                    {ContractPointer(contractId="unknown", generation=1)}
                ),
            ),
        )

        with pytest.raises(ValueError, match="unknown pointers"):
            await participant.__aenter__()

        assert participant.state.phase == ConcordLifecyclePhase.CLOSED
        assert concord._view._broadcaster.subscription_count == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_blocked_exact_store_does_not_stop_unrelated_pointer_or_shutdown() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = _BlockingTokenCreateBucket("blocked-store")

    async with _running_concord(contracts, tokens) as concord:
        participant = concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        )
        await participant.__aenter__()
        blocked = await _create_contract(concord, "blocked-store")
        free = await _create_contract(concord, "free-store")

        async with anyio.create_task_group() as task_group:
            with anyio.fail_after(1):
                await tokens.blocked.wait()
                free_token_key = concord_participant_token_key(
                    contract_id=free.contract_id,
                    generation=free.generation,
                    participant=MANAGER,
                )
                while await tokens.get(free_token_key) is None:
                    await anyio.sleep(0)
                while participant.managed_contract(free) is None:
                    await anyio.sleep(0)

            with anyio.fail_after(1):
                await participant.aclose()
            task_group.cancel_scope.cancel()

        blocked_token_key = concord_participant_token_key(
            contract_id=blocked.contract_id,
            generation=blocked.generation,
            participant=MANAGER,
        )
        assert await tokens.get(blocked_token_key) is None
        assert participant.state.phase == ConcordLifecyclePhase.CLOSED


@pytest.mark.asyncio
async def test_owner_bootstrap_runs_before_every_proposal_and_preserves_live_owners() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        stale_before = await _create_contract(concord, "stale-before")
        async with concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        ) as first:
            assert UUID(first.contract_id).version == 4
            stale_record = await concord.contract_record(stale_before)
            assert stale_record is not None
            assert stale_record.state == ContractState.CANCELLED

            stale_between = await _create_contract(concord, "stale-between")
            async with concord.propose(
                _Endpoint(CONTROLLER, "controller-session"),
                _agreement_spec(),
            ) as second:
                assert UUID(second.contract_id).version == 4
                assert second.contract_id != first.contract_id
                first_record = await concord.contract_record(first.contract)
                stale_record = await concord.contract_record(stale_between)
                assert first_record is not None
                assert first_record.state == ContractState.OPEN
                assert stale_record is not None
                assert stale_record.state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_owner_crash_replacement_cancels_stale_before_fresh_uuid() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    first = Concord(contracts, tokens)
    first_tasks_cm = anyio.create_task_group()
    first_tasks = await first_tasks_cm.__aenter__()
    first.start(first_tasks)
    await first.wait_current()
    abandoned = first.propose(
        _Endpoint(CONTROLLER, "controller-session"),
        _agreement_spec(),
    )
    await abandoned.__aenter__()
    abandoned_pointer = abandoned.contract.pointer
    first_tasks.cancel_scope.cancel()
    await first_tasks_cm.__aexit__(None, None, None)

    async with (
        _running_concord(contracts, tokens) as replacement,
        replacement.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(),
        ) as fresh,
    ):
        abandoned_record = await replacement.contract_record(abandoned.contract)
        assert abandoned_record is not None
        assert abandoned_record.state == ContractState.CANCELLED
        assert fresh.contract.pointer != abandoned_pointer
        assert fresh.generation == 1


@pytest.mark.asyncio
async def test_participant_restart_before_token_commit_cancels_without_creation() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = _BlockingTokenCreateBucket("crash-before-token")
    first = Concord(contracts, tokens)
    first_tasks_cm = anyio.create_task_group()
    first_tasks = await first_tasks_cm.__aenter__()
    first.start(first_tasks)
    await first.wait_current()
    participant = first.participant(
        _Endpoint(MANAGER, "manager-session"),
        ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
    )
    await participant.__aenter__()
    contract = await _create_contract(first, "crash-before-token")
    with anyio.fail_after(1):
        await tokens.blocked.wait()
    token_mutations = tokens.mutation_count
    first_tasks.cancel_scope.cancel()
    await first_tasks_cm.__aexit__(None, None, None)

    async with (
        _running_concord(contracts, tokens) as replacement,
        replacement.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        ) as restarted,
    ):
        assert restarted.managed_contracts == ()
        record = await replacement.contract_record(contract)
        assert record is not None and record.state == ContractState.CANCELLED
        assert tokens.mutation_count == token_mutations


def _agreement_spec(
    *,
    supersedes: ContractPointer | None = None,
) -> ConcordAgreementSpec:
    return ConcordAgreementSpec(
        profile=PROFILE,
        participants=PARTICIPANTS,
        terms={"agreement": "test"},
        supersedes=supersedes,
        participant_sessions={
            str(CONTROLLER): "controller-session",
            str(MANAGER): "manager-session",
        },
    )


@pytest.mark.asyncio
async def test_beacon_heartbeat_uses_jittered_bucket_ttl(monkeypatch) -> None:
    samples: list[tuple[float, float]] = []

    def sample(lower: float, upper: float) -> float:
        samples.append((lower, upper))
        return 0.01

    monkeypatch.setattr(beacon_module.random, "uniform", sample)
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=1)

    async with _running_beacon(raw) as beacon, beacon.advertise(
        _Endpoint(MANAGER, "manager-session"),
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.test.feature.v1",
        ),
    ) as lease:
        first = lease._handle  # noqa: SLF001
        assert first is not None

        with anyio.fail_after(1):
            while True:
                current = await raw.get(first.key)
                assert current is not None
                record = AdvertisementRecord.model_validate(current.value)
                if record.refresh_seq > first.refresh_seq:
                    break
                await anyio.sleep(0.01)

    assert samples[0] == (0.5, 0.75)


@pytest.mark.asyncio
async def test_beacon_manual_update_reschedules_heartbeat(monkeypatch) -> None:
    clock = [100.0]
    samples: list[tuple[float, float]] = []

    def sample(lower: float, upper: float) -> float:
        samples.append((lower, upper))
        return lower if len(samples) == 1 else upper

    monkeypatch.setattr(beacon_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(beacon_module.random, "uniform", sample)
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=1)

    async with _running_beacon(raw) as beacon, beacon.advertise(
        _Endpoint(MANAGER, "manager-session"),
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.test.feature.v1",
            payload={"state": "initial"},
        ),
    ) as lease:
        clock[0] = 100.1
        await lease.update(payload={"state": "updated"})

        assert await lease._next_heartbeat_delay() == 0.75  # noqa: SLF001

    assert samples[:2] == [(0.5, 0.75), (0.5, 0.75)]


@pytest.mark.asyncio
async def test_beacon_noop_updates_do_not_bypass_heartbeat_cadence() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)

    async with _running_beacon(raw) as beacon, beacon.advertise(
        _Endpoint(MANAGER, "manager-session"),
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.test.feature.v1",
            payload={"state": "unchanged"},
        ),
    ) as lease:
        initial_handle = lease._handle  # noqa: SLF001
        assert initial_handle is not None
        initial_revision = raw.revision

        await lease.update()
        await lease.update(payload={"state": "unchanged"})

        assert lease._handle == initial_handle  # noqa: SLF001
        assert raw.revision == initial_revision


@pytest.mark.asyncio
async def test_beacon_lease_recreates_missing_advertisement_key() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)

    async with _running_beacon(raw) as beacon, beacon.advertise(
        _Endpoint(MANAGER, "manager-session"),
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.test.feature.v1",
            hints={"load": "light"},
        ),
    ) as lease:
        first = lease._handle  # noqa: SLF001
        assert first is not None
        assert await raw.expire(first.key)

        await lease.update(labels={"room": "lab"})

        recovered = lease._handle  # noqa: SLF001
        assert recovered is not None
        assert recovered.key == first.key
        entry = await raw.get(first.key)
        assert entry is not None
        record = AdvertisementRecord.model_validate(entry.value)
        assert record.refresh_seq == 1
        assert record.labels == {"room": "lab"}
        assert record.hints == {"load": "light"}


@pytest.mark.asyncio
async def test_beacon_advertise_cleans_stale_same_endpoint_by_default() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)

    async with _running_beacon(raw) as beacon:
        stale = beacon.advertise(
            _Endpoint(MANAGER, "old-session"),
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
            ),
        )
        await stale.__aenter__()
        stale_handle = stale._handle  # noqa: SLF001
        assert stale_handle is not None
        fresh = beacon.advertise(
            _Endpoint(MANAGER, "new-session"),
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
            ),
        )
        await fresh.__aenter__()
        fresh_handle = fresh._handle  # noqa: SLF001
        assert fresh_handle is not None

        assert stale.closed
        assert await raw.get(stale_handle.key) is None
        assert await raw.get(fresh_handle.key) is not None
        assert [
            candidate.advertisement.session_id
            for candidate in beacon.candidates("dev.deckr.test.feature.v1")
        ] == ["new-session"]
        await fresh.aclose()


@pytest.mark.asyncio
async def test_concord_refresh_refuses_to_adopt_concurrent_revision() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = _RacingTokenBucket()
    concord = Concord(contracts, tokens)
    contract = await _create_contract(concord, "refresh-race")
    token = await concord._attach(  # noqa: SLF001
        contract,
        CONTROLLER,
        "controller-session",
        token_id="controller-token",
    )

    with pytest.raises(ConcordConflict) as exc_info:
        await concord._refresh_token(token)  # noqa: SLF001

    assert tokens.raced
    assert exc_info.value.code == ConcordConflictCode.REVISION_CHANGED
    current = await tokens.get(token.key)
    assert current is not None
    assert ParticipantTokenRecord.model_validate(current.value).refresh_seq == 2


@pytest.mark.asyncio
async def test_concord_participant_heartbeat_preserves_due_time_after_early_wake(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(lease_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lease_module.random,
        "uniform",
        lambda _lower, upper: upper,
    )
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=1)
    async with _running_concord(contracts, tokens) as concord:
        contract = await _create_contract(concord, "heartbeat-schedule")
        lease = lease_module.ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
        )
        await lease.attach()

        clock[0] = 100.2
        assert lease._next_heartbeat_delay() == pytest.approx(0.55)  # noqa: SLF001

        clock[0] = 100.75
        refreshed = await lease.refresh()
        assert refreshed.refresh_seq == 2
        assert lease._next_heartbeat_delay() == pytest.approx(0.75)  # noqa: SLF001
        await lease.aclose()


@pytest.mark.asyncio
async def test_concord_participant_lease_cancels_on_refresh_unavailable() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = _FailingUpdateBucket(bucket="tokens", ttl_seconds=1)
    async with _running_concord(contracts, tokens) as concord:
        contract = await _create_contract(concord, "refresh-unavailable")
        lease = lease_module.ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
        )
        await lease.attach()
        tokens.fail_updates = True

        with pytest.raises(ConcordUnavailable, match="broker unavailable"):
            await lease.refresh()

        assert lease.token is None
        assert lease.owned_token is not None
        assert (
            lease.state.phase == lease_module.ConcordParticipantLeasePhase.UNAVAILABLE
        )
        entry = await contracts.get(contract.key)
        assert entry is not None
        assert ContractRecord.model_validate(entry.value).state == ContractState.OPEN
        await lease.aclose()


@pytest.mark.asyncio
async def test_concord_participant_lease_closes_when_cancel_is_unavailable() -> None:
    contracts = _FailingUpdateBucket(bucket="contracts")
    tokens = _FailingUpdateBucket(bucket="tokens", ttl_seconds=1)
    async with _running_concord(contracts, tokens) as concord:
        contract = await _create_contract(concord, "refresh-and-cancel-unavailable")
        lease = lease_module.ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
        )
        await lease.attach()
        contracts.fail_updates = True
        tokens.fail_updates = True

        with pytest.raises(ConcordUnavailable, match="broker unavailable"):
            await lease.refresh()

        assert lease.token is None
        assert lease.owned_token is not None
        entry = await contracts.get(contract.key)
        assert entry is not None
        assert ContractRecord.model_validate(entry.value).state == ContractState.OPEN
        await lease.aclose()


@pytest.mark.asyncio
async def test_concord_participant_close_does_not_withdraw_changed_token_owner() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    async with _running_concord(contracts, tokens) as concord:
        contract = await _create_contract(concord, "changed-token-owner")
        lease = lease_module.ConcordParticipantLease(
            concord._coordinator,  # noqa: SLF001
            concord._view,  # noqa: SLF001
            contract=contract,
            participant=CONTROLLER,
            session_id="controller-session",
        )
        token = await lease.attach()
        entry = await tokens.get(token.key)
        assert entry is not None
        changed_owner = ParticipantTokenRecord.model_validate(entry.value).model_copy(
            update={"token_id": "replacement-owner-token"}
        )
        await tokens.update(token.key, changed_owner, revision=entry.revision)

        await lease.aclose()

        current = await tokens.get(token.key)
        assert current is not None
        assert ParticipantTokenRecord.model_validate(current.value).token_id == (
            "replacement-owner-token"
        )


@pytest.mark.asyncio
async def test_same_session_participant_restart_cancels_without_adopting_token() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        policy = ConcordParticipantPolicy(profile=PROFILE, select=_accept_all)
        async with concord.participant(
            _Endpoint(MANAGER, "manager-session"), policy
        ) as original:
            contract = await _create_contract(concord, "same-session-restart")
            await concord._attach(  # noqa: SLF001
                contract,
                CONTROLLER,
                "controller-session",
                token_id="controller-token",
            )
            with anyio.fail_after(1):
                while original.managed_contract(contract) is None:
                    await anyio.sleep(0)
            manager_token_key = concord_participant_token_key(
                contract_id=contract.contract_id,
                generation=contract.generation,
                participant=MANAGER,
            )

            async with concord.participant(
                _Endpoint(MANAGER, "manager-session"), policy
            ) as restarted:
                assert restarted.managed_contracts == ()
                with anyio.fail_after(2):
                    while True:
                        record = await concord.contract_record(contract)
                        if (
                            record is not None
                            and record.state == ContractState.CANCELLED
                        ):
                            break
                        await anyio.sleep(0)
                assert record.cancel_reason == (
                    "concord_managed_lost_participant_token"
                )
                manager_entry = await tokens.get(manager_token_key)
                if manager_entry is not None:
                    assert ParticipantTokenRecord.model_validate(
                        manager_entry.value
                    ).session_id == "manager-session"


@pytest.mark.asyncio
async def test_restarted_participant_accepts_one_fresh_successor_after_stale_cleanup() -> (
    None
):
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    first = Concord(contracts, tokens)
    first_tasks_cm = anyio.create_task_group()
    first_tasks = await first_tasks_cm.__aenter__()
    first.start(first_tasks)
    await first.wait_current()
    participant = first.participant(
        _Endpoint(MANAGER, "manager-session"),
        ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
    )
    await participant.__aenter__()
    abandoned = first.propose(
        _Endpoint(CONTROLLER, "controller-session"),
        _agreement_spec(),
    )
    await abandoned.__aenter__()
    await abandoned.wait_for_state(
        lambda state: (
            state.validity is not None
            and state.validity.status == ContractValidityStatus.VALID
        ),
        timeout_seconds=1,
    )
    first_tasks.cancel_scope.cancel()
    await first_tasks_cm.__aexit__(None, None, None)

    async with (
        _running_concord(contracts, tokens) as replacement,
        replacement.participant(
            _Endpoint(MANAGER, "manager-restarted"),
            ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
        ) as restarted,
    ):
        stale_record = await replacement.contract_record(abandoned.contract)
        assert stale_record is not None
        assert stale_record.state == ContractState.CANCELLED

        fresh_spec = ConcordAgreementSpec(
            profile=PROFILE,
            participants=PARTICIPANTS,
            terms={"agreement": "fresh-successor"},
            supersedes=abandoned.contract.pointer,
            participant_sessions={str(MANAGER): "manager-restarted"},
        )
        async with replacement.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            fresh_spec,
        ) as fresh:
            ready = await fresh.wait_for_state(
                lambda state: (
                    state.validity is not None
                    and state.validity.status == ContractValidityStatus.VALID
                ),
                timeout_seconds=1,
            )
            assert ready.local_authority
            managed = restarted.managed_contract(fresh.contract)
            assert managed is not None
            assert managed.local_authority
            assert fresh.contract_id != abandoned.contract_id


@pytest.mark.asyncio
async def test_participant_reacts_to_expiry_and_cancellation_without_repair() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    concord = Concord(contracts, tokens)

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        async with (
            concord.participant(
                _Endpoint(MANAGER, "manager-session"),
                ConcordParticipantPolicy(profile=PROFILE, select=_accept_all),
            ) as participant,
            participant.watch_state() as snapshots,
        ):
            assert (await anext(snapshots)).contracts == ()
            expired_contract = await _create_contract(concord, "expiry")
            controller_token = await concord._attach(  # noqa: SLF001
                expired_contract,
                CONTROLLER,
                "controller-session",
                token_id="controller-token-expiry",
            )
            await _receive_participant_status(
                snapshots,
                expired_contract,
                ContractValidityStatus.VALID,
            )

            assert await tokens.expire(controller_token.key)
            with anyio.fail_after(2):
                while participant.managed_contract(expired_contract) is not None:
                    await anyio.sleep(0)
            # Public authority fails closed before best-effort remote cleanup.
            # Observe cancellation independently rather than coupling the two
            # state transitions to one scheduling turn.
            with anyio.fail_after(2):
                while True:
                    expired_record = await concord.contract_record(expired_contract)
                    if (
                        expired_record is not None
                        and expired_record.state == ContractState.CANCELLED
                    ):
                        break
                    await anyio.sleep(0)
            assert expired_record is not None
            assert expired_record.state == ContractState.CANCELLED
            assert expired_record.cancel_reason == "concord_managed_missing_token"

            cancelled_contract = await _create_contract(concord, "cancelled")
            await concord._attach(  # noqa: SLF001
                cancelled_contract,
                CONTROLLER,
                "controller-session",
                token_id="controller-token-cancelled",
            )
            await _receive_participant_status(
                snapshots,
                cancelled_contract,
                ContractValidityStatus.VALID,
            )
            assert await concord._cancel(  # noqa: SLF001
                cancelled_contract,
                CONTROLLER,
                reason="owner cancelled",
            )
            with anyio.fail_after(2):
                while participant.managed_contract(cancelled_contract) is not None:
                    await anyio.sleep(0)

        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_participant_overflow_resnapshots_without_repair_polling() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts", buffer_size=1_024)
    tokens = MemoryJsonKvBucket(
        bucket="tokens",
        ttl_seconds=120,
        buffer_size=2_048,
    )
    concord = Concord(contracts, tokens)
    block_reconcile = False
    reconcile_blocked = anyio.Event()
    resume_reconcile = anyio.Event()

    async def select_contracts(candidates) -> ConcordParticipantSelection:
        nonlocal block_reconcile
        if block_reconcile:
            block_reconcile = False
            reconcile_blocked.set()
            await resume_reconcile.wait()
        return ConcordParticipantSelection(
            candidate.contract.pointer for candidate in candidates
        )

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        async with (
            concord.participant(
                _Endpoint(MANAGER, "manager-session"),
                ConcordParticipantPolicy(profile=PROFILE, select=select_contracts),
            ) as participant,
            participant.watch_state() as snapshots,
        ):
            assert (await anext(snapshots)).contracts == ()
            block_reconcile = True
            await _create_contract(concord, "overflow-000")
            with anyio.fail_after(2):
                await reconcile_blocked.wait()

            for index in range(1, 258):
                await _create_contract(concord, f"overflow-{index:03d}")
            resume_reconcile.set()

            with anyio.fail_after(15):
                while True:
                    snapshot = await anext(snapshots)
                    if len(snapshot.contracts) == 258:
                        break

            assert len(participant.managed_contracts) == 258

        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_participant_unresolved_policy_cleanup_is_publicly_unavailable() -> None:
    contracts = _FailingUpdateBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    accept = True

    def select_contracts(candidates) -> ConcordParticipantSelection:
        return ConcordParticipantSelection(
            candidate.contract.pointer for candidate in candidates if accept
        )

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=select_contracts),
        ) as participant,
    ):
        contract = await _create_contract(concord, "retained-policy-cleanup")
        await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "controller-session",
            token_id="controller-token",
        )
        await participant.wait_for_state(
            lambda state: any(
                managed.contract.pointer == contract.pointer
                and managed.local_authority
                and managed.validity.valid
                for managed in state.contracts
            ),
            timeout_seconds=2,
        )

        contracts.fail_updates = True
        accept = False
        await participant.notify_policy_changed()
        unavailable = await participant.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.UNAVAILABLE,
            timeout_seconds=2,
        )
        assert unavailable.contracts == ()
        assert unavailable.reason is not None

        contracts.fail_updates = False
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)
        await participant.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.READY,
            timeout_seconds=2,
        )


@pytest.mark.asyncio
async def test_participant_selector_failure_cancels_existing_authority() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    fail_selection = False

    def select_contracts(candidates) -> ConcordParticipantSelection:
        if fail_selection:
            raise RuntimeError("policy snapshot is invalid")
        return _accept_all(candidates)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "manager-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=select_contracts),
        ) as participant,
    ):
        contract = await _create_contract(concord, "selector-failure")
        await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "controller-session",
            token_id="controller-token",
        )
        await participant.wait_for_state(
            lambda state: any(
                managed.contract.pointer == contract.pointer
                and managed.local_authority
                and managed.validity.valid
                for managed in state.contracts
            ),
            timeout_seconds=2,
        )

        fail_selection = True
        await participant.notify_policy_changed()
        unavailable = await participant.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.UNAVAILABLE
            and state.reason == "policy snapshot is invalid",
            timeout_seconds=2,
        )
        assert unavailable.contracts == ()

        token_key = concord_participant_token_key(
            contract_id=contract.contract_id,
            generation=contract.generation,
            participant=MANAGER,
        )
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(contract)
                if (
                    record is not None
                    and record.state == ContractState.CANCELLED
                    and await tokens.get(token_key) is None
                ):
                    break
                await anyio.sleep(0)

        assert participant.managed_contract(contract) is None


@pytest.mark.asyncio
async def test_agreement_successor_uses_fresh_opaque_id_after_token_loss() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "controller-session"), _agreement_spec()
        ) as agreement,
    ):
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        await agreement.wait_for_state(lambda state: state.valid)
        local_token_key = concord_participant_token_key(
            contract_id=agreement.contract_id,
            generation=agreement.generation,
            participant=CONTROLLER,
        )
        local_entry = await tokens.get(local_token_key)
        assert local_entry is not None
        marker_revision = await tokens.delete(
            local_token_key,
            revision=local_entry.revision,
        )
        assert marker_revision is not None
        await agreement.wait_for_state(
            lambda state: state.phase == ConcordLifecyclePhase.TERMINAL
        )

        original_pointer = agreement.contract.pointer
        original_id = agreement.contract_id
        async with concord.propose(
            _Endpoint(CONTROLLER, "controller-session"),
            _agreement_spec(supersedes=original_pointer),
        ) as successor:
            assert successor.contract_id != original_id
            assert successor.generation == 1
            successor_record = await concord.contract_record(successor.contract)
            assert successor_record is not None
            assert successor_record.supersedes == original_pointer
            assert successor.state.validity is not None
            assert successor.state.validity.status == (
                ContractValidityStatus.NOT_YET_FULFILLED
            )


@pytest.mark.asyncio
async def test_agreement_refresh_cancels_after_losing_local_token_handle() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "controller-session"), _agreement_spec()
        ) as agreement,
    ):
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        await agreement.wait_for_state(lambda state: state.valid)
        local_token_key = concord_participant_token_key(
            contract_id=agreement.contract_id,
            generation=agreement.generation,
            participant=CONTROLLER,
        )
        local_entry = await tokens.get(local_token_key)
        assert local_entry is not None
        await tokens.delete(local_token_key, revision=local_entry.revision)

        state = await agreement.wait_for_state(
            lambda current: current.phase == ConcordLifecyclePhase.TERMINAL
        )
        assert not state.local_authority
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(agreement.contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)
            assert record.cancel_reason == ("concord_agreement_lost_participant_token")


@pytest.mark.asyncio
async def test_agreement_peer_token_loss_relinquishes_local_authority() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "controller-session"), _agreement_spec()
        ) as agreement,
    ):
        peer = await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        await agreement.wait_for_state(lambda state: state.valid)
        await tokens.delete(peer.key, revision=peer.revision)

        state = await agreement.wait_for_state(
            lambda current: current.phase == ConcordLifecyclePhase.TERMINAL
        )
        assert state.validity is not None
        assert state.validity.status == ContractValidityStatus.MISSING_TOKEN
        assert not state.local_authority
        assert not state.valid
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(agreement.contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)


@pytest.mark.asyncio
async def test_agreement_closes_when_lost_authority_cannot_be_cancelled() -> None:
    contracts = _FailingUpdateBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "controller-session"), _agreement_spec()
        ) as agreement,
    ):
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        await agreement.wait_for_state(lambda state: state.valid)
        local_token_key = concord_participant_token_key(
            contract_id=agreement.contract_id,
            generation=agreement.generation,
            participant=CONTROLLER,
        )
        local_entry = await tokens.get(local_token_key)
        assert local_entry is not None
        contracts.fail_updates = True
        await tokens.delete(local_token_key, revision=local_entry.revision)

        state = await agreement.wait_for_state(
            lambda current: current.phase == ConcordLifecyclePhase.TERMINAL
        )
        assert state.validity is not None
        assert state.validity.status == ContractValidityStatus.MISSING_TOKEN
        assert not state.local_authority
        entry = await contracts.get(agreement.contract.key)
        assert entry is not None
        assert ContractRecord.model_validate(entry.value).state == ContractState.OPEN


async def _receive_participant_status(
    snapshots,
    contract: ContractHandle,
    status: ContractValidityStatus,
):
    with anyio.fail_after(2):
        while True:
            snapshot = await anext(snapshots)
            if any(
                managed.contract.pointer == contract.pointer
                and managed.validity.status == status
                for managed in snapshot.contracts
            ):
                return snapshot
