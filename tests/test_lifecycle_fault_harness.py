from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import pytest

from deckr.beacon import Beacon, BeaconAdvertisementSpec
from deckr.concord import (
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordConflictCode,
    ConcordLifecyclePhase,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
    ConcordUnavailable,
    ContractRecord,
    ContractState,
    concord_participant_token_key,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvUnavailable
from deckr.testing import (
    LifecycleFaultHarness,
    LifecycleFaultPhase,
    LifecycleVirtualClock,
    MemoryJsonKvBucket,
)

CONTROLLER = controller_address("fault-client")
MANAGER = hardware_manager_address("fault-provider")
PROFILE = "dev.deckr.test.faults.v1"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    address: object
    session_id: str


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
            await concord.aclose()
            task_group.cancel_scope.cancel()


def _spec() -> ConcordAgreementSpec:
    return ConcordAgreementSpec(
        profile=PROFILE,
        participants=(CONTROLLER, MANAGER),
        participant_sessions={str(MANAGER): "provider-session"},
    )


@pytest.mark.asyncio
async def test_prepared_agreement_recovers_every_post_commit_ack_loss() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)
    contracts.fail_next("create", phase=LifecycleFaultPhase.AFTER)
    tokens.fail_next("create", phase=LifecycleFaultPhase.AFTER)
    contracts.fail_next("update", phase=LifecycleFaultPhase.AFTER)

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        ) as agreement,
    ):
        assert agreement.state.local_authority
        record = await concord.contract_record(agreement.contract)
        assert record is not None
        assert record.participant_sessions == {
            str(CONTROLLER): "client-session",
            str(MANAGER): "provider-session",
        }
        assert contracts.operation_counts["create"] == 1
        assert tokens.operation_counts["create"] == 1


@pytest.mark.asyncio
async def test_prepared_refresh_cancel_and_withdraw_recover_lost_acknowledgements() -> (
    None
):
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        await agreement.__aenter__()
        lease = agreement._lease  # noqa: SLF001 - exact retained ownership assertion
        assert lease is not None and lease.token is not None

        tokens.fail_next("update", phase=LifecycleFaultPhase.AFTER)
        refreshed = await lease.refresh()
        assert refreshed.refresh_seq == 2

        contracts.fail_next("update", phase=LifecycleFaultPhase.AFTER)
        assert await agreement.cancel("fault-test")
        record = await concord.contract_record(agreement.contract)
        assert record is not None and record.state == ContractState.CANCELLED

        tokens.fail_next("delete", phase=LifecycleFaultPhase.AFTER)
        token_key = refreshed.key
        await agreement.aclose()
        assert await tokens.get(token_key) is None


@pytest.mark.asyncio
async def test_inflight_refresh_cannot_restore_relinquished_authority() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        await agreement.__aenter__()
        lease = agreement._lease  # noqa: SLF001
        assert lease is not None and lease.token is not None
        barrier = tokens.block_next(
            "update",
            phase=LifecycleFaultPhase.BEFORE,
            key_contains=lease.token.key,
        )
        errors: list[BaseException] = []

        async def refresh() -> None:
            try:
                await lease.refresh()
            except BaseException as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(refresh)
            await barrier.wait()
            lease.relinquish_local_authority()
            barrier.unblock()

        assert len(errors) == 1
        assert isinstance(errors[0], ConcordConflict)
        assert errors[0].code == ConcordConflictCode.LEASE_CLOSED
        assert lease.token is None
        retained = lease.owned_token
        if retained is not None:
            assert await tokens.get(retained.key) is None
        await agreement.aclose()


@pytest.mark.asyncio
async def test_contract_cancelled_after_token_commit_aborts_attachment() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)
    barrier = tokens.block_next(
        "create",
        phase=LifecycleFaultPhase.AFTER,
    )

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        entered = anyio.Event()

        async def enter() -> None:
            await agreement.__aenter__()
            entered.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(enter)
            await barrier.wait()
            await concord._cancel(  # noqa: SLF001
                agreement.contract,
                CONTROLLER,
                reason="cancelled-during-attach",
            )
            barrier.unblock()
            await entered.wait()

        assert agreement.state.phase == ConcordLifecyclePhase.TERMINAL
        assert not agreement.state.local_authority
        token_key = concord_participant_token_key(
            contract_id=agreement.contract_id,
            generation=agreement.generation,
            participant=CONTROLLER,
        )
        assert await tokens.get(token_key) is None
        await agreement.aclose()


@pytest.mark.asyncio
async def test_unresolved_agreement_cancel_fails_closed_and_retries_cleanup() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        await agreement.__aenter__()
        contracts.fail_next("update", phase=LifecycleFaultPhase.BEFORE)

        with pytest.raises(ConcordUnavailable):
            await agreement.cancel("explicit-cancel")

        assert agreement.state.phase == ConcordLifecyclePhase.UNAVAILABLE
        assert not agreement.state.local_authority
        assert not agreement.state.valid
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(agreement.contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)
        await agreement.aclose()


@pytest.mark.asyncio
async def test_unresolved_participant_cancel_fails_closed_and_retries_cleanup() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    def accept_all(candidates) -> ConcordParticipantSelection:
        return ConcordParticipantSelection(
            candidate.contract.pointer for candidate in candidates
        )

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "provider-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=accept_all),
        ) as participant,
        concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        ) as agreement,
    ):
        await agreement.wait_for_state(lambda state: state.valid, timeout_seconds=2)
        await participant.wait_for_state(
            lambda state: any(
                item.contract.pointer == agreement.contract.pointer
                and item.local_authority
                for item in state.contracts
            ),
            timeout_seconds=2,
        )
        contracts.fail_next("update", phase=LifecycleFaultPhase.BEFORE)

        with pytest.raises(ConcordUnavailable):
            await participant.cancel(agreement.contract, reason="provider-cancel")

        assert participant.state.phase == ConcordLifecyclePhase.UNAVAILABLE
        retained = participant.managed_contract(agreement.contract)
        assert retained is None or not retained.local_authority
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(agreement.contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)


@pytest.mark.asyncio
async def test_blocked_pointer_cancellation_cannot_hold_public_authority() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    def accept_all(candidates) -> ConcordParticipantSelection:
        return ConcordParticipantSelection(
            candidate.contract.pointer for candidate in candidates
        )

    async with (
        _running_concord(contracts, tokens) as concord,
        concord.participant(
            _Endpoint(MANAGER, "provider-session"),
            ConcordParticipantPolicy(profile=PROFILE, select=accept_all),
        ) as participant,
    ):
        contract = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            contract_id="blocked-pointer-cancel",
            profile=PROFILE,
            created_by=CONTROLLER,
            participant_sessions={
                str(CONTROLLER): "client-session",
                str(MANAGER): "provider-session",
            },
        )
        owner_token = await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "client-session",
            token_id="blocked-pointer-owner-token",
        )
        await participant.wait_for_state(
            lambda state: any(
                item.contract.pointer == contract.pointer
                and item.local_authority
                for item in state.contracts
            ),
            timeout_seconds=2,
        )

        barrier = contracts.block_next(
            "update",
            phase=LifecycleFaultPhase.BEFORE,
            key_contains=contract.contract_id,
        )
        assert await tokens.expire(owner_token.key)
        with anyio.fail_after(2):
            await barrier.wait()

        # The pointer worker has reached blocked cancellation I/O. Public
        # authority must already be gone; cleanup completion is independent.
        state = participant.state
        assert all(
            item.contract.pointer != contract.pointer or not item.local_authority
            for item in state.contracts
        )
        barrier.unblock()
        with anyio.fail_after(2):
            while True:
                record = await concord.contract_record(contract)
                if record is not None and record.state == ContractState.CANCELLED:
                    break
                await anyio.sleep(0)


@pytest.mark.asyncio
async def test_prepared_create_before_commit_failure_never_allocates_a_successor() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)
    contracts.fail_next("create", phase=LifecycleFaultPhase.BEFORE)

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        with pytest.raises(ConcordUnavailable):
            await agreement.__aenter__()

        assert contracts.operation_counts["create"] == 1
        assert await contracts.items() == ()
        assert agreement.closed


@pytest.mark.asyncio
async def test_owner_bootstrap_retries_only_unavailable_stale_cleanup() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)

    async with _running_concord(contracts, tokens) as concord:
        stale = await concord._create_contract(  # noqa: SLF001
            (CONTROLLER, MANAGER),
            contract_id="stale-owner-contract",
            profile=PROFILE,
            created_by=CONTROLLER,
        )
        contracts.fail_next("update", phase=LifecycleFaultPhase.BEFORE)

        async with concord.propose(
            _Endpoint(CONTROLLER, "replacement-session"),
            _spec(),
        ) as replacement:
            assert replacement.contract_id != stale.contract_id
            record = await concord.contract_record(stale)
            assert record is not None
            assert record.state == ContractState.CANCELLED

        assert contracts.operation_counts["update"] >= 2


@pytest.mark.asyncio
async def test_prepared_create_conflicting_recovery_fails_explicitly() -> None:
    contracts = LifecycleFaultHarness(bucket="contracts")
    tokens = LifecycleFaultHarness(bucket="tokens", ttl_seconds=60)
    barrier = contracts.block_next(
        "create",
        phase=LifecycleFaultPhase.AFTER,
        error=KvUnavailable("create reply lost"),
    )

    async with _running_concord(contracts, tokens) as concord:
        agreement = concord.propose(
            _Endpoint(CONTROLLER, "client-session"),
            _spec(),
        )
        error: list[BaseException] = []

        async def enter() -> None:
            try:
                await agreement.__aenter__()
            except BaseException as exc:
                error.append(exc)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(enter)
            await barrier.wait()
            entries = await contracts.items()
            assert len(entries) == 1
            entry = entries[0]
            changed = ContractRecord.model_validate(entry.value).model_copy(
                update={"profile": "conflicting-profile"}
            )
            await MemoryJsonKvBucket.update(
                contracts,
                entry.key,
                changed,
                revision=entry.revision,
            )
            barrier.unblock()

        assert len(error) == 1
        assert isinstance(error[0], ConcordConflict)
        assert error[0].code == ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH
        assert contracts.operation_counts["create"] == 1


@pytest.mark.asyncio
async def test_beacon_prepared_create_and_withdraw_recover_lost_ack() -> None:
    advertisements = LifecycleFaultHarness(
        bucket="beacon",
        ttl_seconds=30,
    )
    beacon = Beacon(advertisements)
    endpoint = _Endpoint(MANAGER, "provider-session")

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_current()
        advertisements.fail_next("create", phase=LifecycleFaultPhase.AFTER)
        lease = beacon.advertise(
            endpoint,
            BeaconAdvertisementSpec(feature_id="dev.deckr.test.fault-beacon.v1"),
        )
        await lease.__aenter__()
        assert len(await advertisements.items()) == 1

        advertisements.fail_next("delete", phase=LifecycleFaultPhase.AFTER)
        await lease.aclose()
        assert await advertisements.items() == ()
        await beacon.aclose()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_lifecycle_virtual_clock_records_retry_and_deterministic_jitter() -> None:
    clock = LifecycleVirtualClock(jitter_ratios=(0.0, 1.0))

    assert clock.uniform(2, 4) == 2
    assert clock.uniform(2, 4) == 4
    await clock.sleep(0.25)

    assert clock.now == 0.25
    assert clock.sleeps == [0.25]
