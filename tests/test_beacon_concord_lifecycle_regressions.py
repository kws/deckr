from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import pytest

import deckr.beacon as beacon_module
import deckr.concord as concord_module
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
    ConcordParticipantChange,
    ConcordUnavailable,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    ParticipantTokenRecord,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvUnavailable
from deckr.testing import MemoryJsonKvBucket

PROFILE = "dev.deckr.test.lifecycle-regression.v1"
CONTROLLER = controller_address("controller-main")
MANAGER = hardware_manager_address("manager-main")
PARTICIPANTS = (CONTROLLER, MANAGER)


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
            bumped = token.model_copy(
                update={"refresh_seq": token.refresh_seq + 1}
            )
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


def _agreement_spec(
    *,
    supersedes: ContractPointer | None = None,
) -> ConcordAgreementSpec:
    return ConcordAgreementSpec(
        profile=PROFILE,
        participants=PARTICIPANTS,
        local_participant=CONTROLLER,
        local_session_id="controller-session",
        terms={"agreement": "test"},
        supersedes=supersedes,
        current_sessions={
            str(CONTROLLER): "controller-session",
            str(MANAGER): "manager-session",
        },
        log_label="LifecycleRegression",
    )


@pytest.mark.asyncio
async def test_beacon_heartbeat_uses_jittered_bucket_ttl(monkeypatch) -> None:
    samples: list[tuple[float, float]] = []

    def sample(lower: float, upper: float) -> float:
        samples.append((lower, upper))
        return 0.01

    monkeypatch.setattr(beacon_module.random, "uniform", sample)
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=1)

    async with _running_beacon(raw) as beacon:
        lease = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="manager-session",
                advertisement_id="advertisement-1",
            )
        )
        first = lease.handle

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

    async with _running_beacon(raw) as beacon:
        lease = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload={"state": "initial"},
            )
        )
        clock[0] = 100.1
        await lease.update(payload={"state": "updated"})

        assert await lease._next_heartbeat_delay() == 0.75  # noqa: SLF001

    assert samples[:2] == [(0.5, 0.75), (0.5, 0.75)]


@pytest.mark.asyncio
async def test_beacon_noop_updates_do_not_bypass_heartbeat_cadence() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)

    async with _running_beacon(raw) as beacon:
        lease = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="manager-session",
                advertisement_id="advertisement-1",
                payload={"state": "unchanged"},
            )
        )
        initial_handle = lease.handle
        initial_revision = raw.revision

        repeated = await lease.update()
        same_payload = await lease.update(payload={"state": "unchanged"})

        assert repeated == initial_handle
        assert same_payload == initial_handle
        assert raw.revision == initial_revision


@pytest.mark.asyncio
async def test_beacon_lease_recreates_missing_advertisement_key() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)

    async with _running_beacon(raw) as beacon:
        lease = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="manager-session",
                advertisement_id="advertisement-1",
                hints={"load": "light"},
            )
        )
        first = lease.handle
        assert await raw.expire(first.key)

        recovered = await lease.update(labels={"room": "lab"})

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
        stale = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="old-session",
                advertisement_id="stale-advertisement",
            )
        )
        fresh = await beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature.v1",
                endpoint=MANAGER,
                session_id="new-session",
                advertisement_id="fresh-advertisement",
            )
        )

        assert await raw.get(stale.handle.key) is None
        assert await raw.get(fresh.handle.key) is not None
        assert [
            candidate.advertisement.advertisement_id
            for candidate in beacon.candidates("dev.deckr.test.feature.v1")
        ] == ["fresh-advertisement"]


@pytest.mark.asyncio
async def test_concord_refresh_returns_latest_token_after_revision_race() -> None:
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

    refreshed = await concord._refresh_token(token)  # noqa: SLF001

    assert tokens.raced
    assert refreshed.refresh_seq == 2
    assert refreshed.revision != token.revision


@pytest.mark.asyncio
async def test_concord_participant_heartbeat_preserves_due_time_after_early_wake(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(concord_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        concord_module.random,
        "uniform",
        lambda _lower, upper: upper,
    )
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=1)
    concord = Concord(contracts, tokens)
    contract = await _create_contract(concord, "heartbeat-schedule")
    lease = concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
        refresh_interval=0.2,
    )
    first = await lease.attach_or_refresh()

    clock[0] = 100.2
    early = await lease.attach_or_refresh()
    assert early == first
    assert lease._next_heartbeat_delay() == pytest.approx(0.55)  # noqa: SLF001

    clock[0] = 100.75
    refreshed = await lease.attach_or_refresh()
    assert refreshed.refresh_seq == 2
    assert lease._next_heartbeat_delay() == pytest.approx(0.75)  # noqa: SLF001


@pytest.mark.asyncio
async def test_concord_participant_lease_cancels_on_refresh_unavailable() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = _FailingUpdateBucket(bucket="tokens", ttl_seconds=1)
    concord = Concord(contracts, tokens)
    contract = await _create_contract(concord, "refresh-unavailable")
    lease = concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
    )
    await lease.attach_or_refresh()
    lease._last_refresh_at = None  # noqa: SLF001
    tokens.fail_updates = True

    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        await lease.attach_or_refresh()

    assert lease.token is None
    with pytest.raises(ConcordConflict) as raised:
        await lease.attach_or_refresh()
    assert raised.value.code == ConcordConflictCode.LEASE_CLOSED
    entry = await contracts.get(contract.key)
    assert entry is not None
    record = ContractRecord.model_validate(entry.value)
    assert record.state == ContractState.CANCELLED
    assert record.cancel_reason == (
        concord_module.CONCORD_REFRESH_UNAVAILABLE_CANCEL_REASON
    )


@pytest.mark.asyncio
async def test_concord_participant_lease_closes_when_cancel_is_unavailable() -> None:
    contracts = _FailingUpdateBucket(bucket="contracts")
    tokens = _FailingUpdateBucket(bucket="tokens", ttl_seconds=1)
    concord = Concord(contracts, tokens)
    contract = await _create_contract(concord, "refresh-and-cancel-unavailable")
    lease = concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
    )
    await lease.attach_or_refresh()
    lease._last_refresh_at = None  # noqa: SLF001
    contracts.fail_updates = True
    tokens.fail_updates = True

    with pytest.raises(ConcordUnavailable, match="broker unavailable"):
        await lease.attach_or_refresh()

    assert lease.token is None
    with pytest.raises(ConcordConflict) as raised:
        await lease.attach_or_refresh()
    assert raised.value.code == ConcordConflictCode.LEASE_CLOSED
    entry = await contracts.get(contract.key)
    assert entry is not None
    assert ContractRecord.model_validate(entry.value).state == ContractState.OPEN


@pytest.mark.asyncio
async def test_concord_participant_close_does_not_withdraw_changed_token_owner() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    concord = Concord(contracts, tokens)
    contract = await _create_contract(concord, "changed-token-owner")
    lease = concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
    )
    token = await lease.attach_or_refresh()
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
async def test_same_session_participant_restart_cancels_without_adopting_token() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        contract = await _create_contract(concord, "same-session-restart")
        await concord._attach(  # noqa: SLF001
            contract,
            CONTROLLER,
            "controller-session",
            token_id="controller-token",
        )
        original = concord.participant(
            participant=MANAGER,
            session_id="manager-session",
            profile=PROFILE,
            accept_contract=lambda _contract, _record: True,
        )
        managed = (await original.reconcile(reason="initial runtime"))[0]
        manager_token = managed.token
        assert manager_token is not None

        restarted = concord.participant(
            participant=MANAGER,
            session_id="manager-session",
            profile=PROFILE,
            accept_contract=lambda _contract, _record: True,
        )

        assert await restarted.reconcile(reason="same-session restart") == ()
        assert restarted.managed_contracts == ()
        record = await concord.contract_record(contract)
        assert record is not None
        assert record.state == ContractState.CANCELLED
        assert record.cancel_reason == (
            concord_module.CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON
        )
        assert await tokens.get(manager_token.key) is not None


@pytest.mark.asyncio
async def test_participant_reacts_to_expiry_and_cancellation_without_repair() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    concord = Concord(contracts, tokens)

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        participant = concord.participant(
            participant=MANAGER,
            session_id="manager-session",
            profile=PROFILE,
            accept_contract=lambda _contract, _record: True,
            reconcile_interval=3_600,
        )
        participant.start(task_group)
        async with participant.watch() as snapshots:
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
            expired_record = await concord.contract_record(expired_contract)
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

        await participant.aclose()
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

    async def accept_contract(_contract, _record) -> bool:
        nonlocal block_reconcile
        if block_reconcile:
            block_reconcile = False
            reconcile_blocked.set()
            await resume_reconcile.wait()
        return True

    async with anyio.create_task_group() as task_group:
        concord.start(task_group)
        await concord.wait_current()
        participant = concord.participant(
            participant=MANAGER,
            session_id="manager-session",
            profile=PROFILE,
            accept_contract=accept_contract,
            reconcile_interval=3_600,
        )
        participant.start(task_group)
        async with participant.watch() as snapshots:
            assert (await anext(snapshots)).contracts == ()
            block_reconcile = True
            await _create_contract(concord, "overflow-000")
            with anyio.fail_after(2):
                await reconcile_blocked.wait()

            for index in range(1, 258):
                await _create_contract(concord, f"overflow-{index:03d}")
            resume_reconcile.set()

            saw_resnapshot = False
            with anyio.fail_after(15):
                while True:
                    snapshot = await anext(snapshots)
                    if isinstance(snapshot, ConcordParticipantChange):
                        saw_resnapshot |= snapshot.resnapshot_required
                    if len(snapshot.contracts) == 258:
                        break

            assert saw_resnapshot
            assert len(participant.managed_contracts) == 258

        await participant.aclose()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_agreement_successor_uses_fresh_opaque_id_after_token_loss() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = await concord.propose(_agreement_spec())
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        assert (await agreement.refresh()).status == ContractValidityStatus.VALID
        local_token = agreement.local_token
        assert local_token is not None
        marker_revision = await tokens.delete(
            local_token.key,
            revision=local_token.revision,
        )
        assert marker_revision is not None
        await concord._view.wait_token_revision(  # noqa: SLF001
            local_token.key,
            marker_revision,
        )
        assert (await concord.validate(agreement.contract)).status == (
            ContractValidityStatus.MISSING_TOKEN
        )

        successor = await concord.propose(
            _agreement_spec(supersedes=agreement.contract.pointer)
        )

        assert successor.contract_id != agreement.contract_id
        assert successor.generation == 1
        assert successor.local_token is not None
        successor_record = await concord.contract_record(successor.contract)
        assert successor_record is not None
        assert successor_record.supersedes == agreement.contract.pointer
        assert (await successor.refresh()).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )


@pytest.mark.asyncio
async def test_agreement_refresh_cancels_after_losing_local_token_handle() -> None:
    contracts = MemoryJsonKvBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = await concord.propose(_agreement_spec())
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        assert (await agreement.refresh()).status == ContractValidityStatus.VALID
        local_token = agreement.local_token
        assert local_token is not None
        agreement._lease._token = None  # noqa: SLF001
        agreement._lease._last_refresh_at = None  # noqa: SLF001

        validity = await agreement.refresh()

        assert validity.status == ContractValidityStatus.CANCELLED
        assert agreement.closed
        record = await concord.contract_record(agreement.contract)
        assert record is not None
        assert record.state == ContractState.CANCELLED
        assert record.cancel_reason == (
            concord_module.CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON
        )
        assert await tokens.get(local_token.key) is not None


@pytest.mark.asyncio
async def test_agreement_closes_when_lost_authority_cannot_be_cancelled() -> None:
    contracts = _FailingUpdateBucket(bucket="contracts")
    tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)

    async with _running_concord(contracts, tokens) as concord:
        agreement = await concord.propose(_agreement_spec())
        await concord._attach(  # noqa: SLF001
            agreement.contract,
            MANAGER,
            "manager-session",
            token_id="manager-token",
        )
        assert (await agreement.refresh()).status == ContractValidityStatus.VALID
        local_token = agreement.local_token
        assert local_token is not None
        contracts.fail_updates = True
        agreement._lease._token = None  # noqa: SLF001
        agreement._lease._last_refresh_at = None  # noqa: SLF001

        validity = await agreement.refresh()

        assert validity.status == ContractValidityStatus.INVALID_TOKEN
        assert validity.reason == (
            concord_module.CONCORD_AGREEMENT_LOST_PARTICIPANT_TOKEN_REASON
        )
        assert agreement.closed
        with pytest.raises(ConcordConflict) as raised:
            await agreement.refresh()
        assert raised.value.code == ConcordConflictCode.AGREEMENT_CLOSED
        entry = await contracts.get(agreement.contract.key)
        assert entry is not None
        assert ContractRecord.model_validate(entry.value).state == ContractState.OPEN
        assert await tokens.get(local_token.key) is not None


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
