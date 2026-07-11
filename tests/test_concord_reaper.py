from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from message_bus_mocks import mock_deckr

from deckr._authority_buckets import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
    concord_maintenance_stores,
)
from deckr.components import ComponentContext
from deckr.concord import (
    CONCORD_CONTRACT_SCHEMA_ID,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractRecord,
    ContractState,
    ContractValidityStatus,
    canonical_json_hash,
    concord_contract_key,
)
from deckr.concord_maintenance import (
    CONCORD_REAPER_STALE_CONTRACT_REASON,
    ConcordMaintenance,
    ConcordReaperConfig,
    ConcordReaperService,
    ConcordStaleObservationRecord,
    concord_stale_observation_key,
)
from deckr.concord_reaper import CONCORD_REAPER_COMPONENT_ID, component
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.testing import ConcordMaintenanceHarness, MemoryJsonKvBucket

PROFILE = "com.example.reaper_test.v1"


class ManualClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class UnavailableGetKvBucket:
    def __init__(self, inner: MemoryJsonKvBucket) -> None:
        self._inner = inner
        self.bucket = inner.bucket

    async def ttl_seconds(self):
        return await self._inner.ttl_seconds()

    async def get(self, key: str):
        del key
        raise ConcordUnavailable(
            ConcordUnavailableCode.STORE_UNAVAILABLE,
            "state unavailable",
        )

    async def items(self, prefix: str = ""):
        return await self._inner.items(prefix)

    async def put(self, *args, **kwargs):
        return await self._inner.put(*args, **kwargs)

    async def create(self, *args, **kwargs):
        return await self._inner.create(*args, **kwargs)

    async def update(self, *args, **kwargs):
        return await self._inner.update(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._inner.delete(*args, **kwargs)

    def watch(self, *args, **kwargs):
        return self._inner.watch(*args, **kwargs)


class CountingNoWatchKvBucket(MemoryJsonKvBucket):
    def __init__(self, *, bucket: str) -> None:
        super().__init__(bucket=bucket)
        self.items_prefixes: list[str] = []
        self.watch_called = False

    async def items(self, prefix: str = ""):
        self.items_prefixes.append(prefix)
        return await super().items(prefix)

    def watch(self, *args, **kwargs):
        self.watch_called = True
        raise AssertionError("reaper scan must not start a materialized KV watch")


class RacingContractUpdateStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="contracts")
        self.race_on_update = False
        self.raced = False

    async def update(self, key, value, *, revision, ttl=None):
        if self.race_on_update and not self.raced:
            current = await self.get_exact(key)
            assert current is not None
            await super().put(key, current.value)
            self.raced = True
        return await super().update(key, value, revision=revision, ttl=ttl)


class RacingContractDeleteStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="contracts")
        self.race_on_delete = False
        self.raced = False

    async def delete(self, key, *, revision=None):
        if self.race_on_delete and not self.raced:
            current = await self.get_exact(key)
            assert current is not None
            await super().put(key, current.value)
            self.raced = True
        return await super().delete(key, revision=revision)


class RacingObservationCreateStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="maintenance")
        self.raced = False

    async def create(self, key, value, *, ttl=None):
        if not self.raced:
            candidate = ConcordStaleObservationRecord.model_validate(value)
            earlier = candidate.model_copy(
                update={
                    "first_observed_stale_at": (
                        candidate.first_observed_stale_at - timedelta(seconds=60)
                    )
                }
            )
            await super().create(key, earlier, ttl=ttl)
            self.raced = True
        return await super().create(key, value, ttl=ttl)


class RacingObservationDeleteStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="maintenance")
        self.race_on_delete = False
        self.raced = False

    async def delete(self, key, *, revision=None):
        if self.race_on_delete and not self.raced:
            current = await self.get_exact(key)
            assert current is not None
            observation = ConcordStaleObservationRecord.model_validate(current.value)
            replacement = observation.model_copy(update={"reason": "raced replacement"})
            await super().update(key, replacement, revision=current.revision)
            self.raced = True
        return await super().delete(key, revision=revision)


def _stores():
    return (
        MemoryJsonKvBucket(bucket="contracts"),
        MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120),
        MemoryJsonKvBucket(bucket="maintenance"),
    )


def _reaper(
    maintenance: ConcordMaintenance,
    clock: ManualClock,
    *,
    stale_grace_seconds: float = 900,
    cancelled_retention_seconds: float = 3600,
) -> ConcordReaperService:
    return ConcordReaperService(
        maintenance,
        config=ConcordReaperConfig(
            staleGraceSeconds=stale_grace_seconds,
            cancelledRetentionSeconds=cancelled_retention_seconds,
            scanIntervalSeconds=60,
            logLabel="TestReaper",
        ),
        clock=clock,
    )


def _harness(
    contract_bucket,
    token_bucket,
    maintenance_bucket,
) -> ConcordMaintenanceHarness:
    return ConcordMaintenanceHarness(
        contract_store=contract_bucket,
        token_store=token_bucket,
        maintenance_store=maintenance_bucket,
    )


async def _contract(harness: ConcordMaintenanceHarness, *, contract_id: str):
    return await harness.concord._create_contract(
        (controller_address("controller-main"), hardware_manager_address("manager-main")),
        contract_id=contract_id,
        profile=PROFILE,
        terms={"profile": PROFILE, "contract": contract_id},
        created_by=controller_address("controller-main"),
    )


async def _delete_token(harness: ConcordMaintenanceHarness, token) -> None:
    await harness.token_store.delete(token.key, revision=token.revision)


async def _stale_observation(maintenance_state, contract) -> ConcordStaleObservationRecord:
    entry = await maintenance_state.get(
        concord_stale_observation_key(
            contract_id=contract.contract_id,
            generation=contract.generation,
        )
    )
    assert entry is not None
    return ConcordStaleObservationRecord.model_validate(entry.value)


async def _assert_no_stale_observation(maintenance_state, contract) -> None:
    assert (
        await maintenance_state.get(
            concord_stale_observation_key(
                contract_id=contract.contract_id,
                generation=contract.generation,
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_reaper_scan_uses_raw_items_without_materialized_watches() -> None:
    clock = ManualClock()
    contract_state = CountingNoWatchKvBucket(bucket="contracts")
    token_state = CountingNoWatchKvBucket(bucket="tokens")
    maintenance_state = CountingNoWatchKvBucket(bucket="maintenance")
    harness = _harness(contract_state, token_state, maintenance_state)
    await _contract(harness, contract_id="raw-scan-contract")
    reaper = _reaper(harness.maintenance, clock)

    result = await reaper.scan_once()

    assert result.scanned_contract_count == 1
    assert result.stale_observations_created == 1
    assert contract_state.items_prefixes == ["contracts."]
    assert maintenance_state.items_prefixes == ["stale.", "stale."]
    assert not contract_state.watch_called
    assert not token_state.watch_called
    assert not maintenance_state.watch_called
    for store in (contract_state, token_state, maintenance_state):
        assert store.start_count == 0
        assert store.active_watch_count == 0
        assert store.active_subscription_count == 0


@pytest.mark.asyncio
async def test_pending_open_contract_with_valid_token_is_not_stale() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="pending-with-owner-token")
    await harness.concord._attach(
        contract,
        controller_address("controller-main"),
        "session",
    )
    reaper = _reaper(harness.maintenance, clock)

    validity = await harness.maintenance.validate_exact(contract)
    assert validity.status == ContractValidityStatus.NOT_YET_FULFILLED
    assert validity.tokens
    result = await reaper.scan_once()
    assert result.stale_observations_created == 0
    assert result.contracts_cancelled == 0
    await _assert_no_stale_observation(maintenance_state, contract)

    clock.advance(900)
    result = await reaper.scan_once()

    assert result.stale_observations_created == 0
    assert result.contracts_cancelled == 0
    current = await contract_state.get(contract.key)
    assert current is not None
    assert ContractRecord.model_validate(current.value).state == ContractState.OPEN
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_missing_token_contract_cancelled_only_after_stale_grace() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="missing-token-contract")
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    await harness.concord._attach(contract, controller, "controller-session")
    manager_token = await harness.concord._attach(
        contract,
        manager,
        "manager-session",
    )
    await _delete_token(harness, manager_token)
    reaper = _reaper(harness.maintenance, clock)

    assert (await harness.maintenance.validate_exact(contract)).status == (
        ContractValidityStatus.MISSING_TOKEN
    )
    assert (await reaper.scan_once()).contracts_cancelled == 0
    clock.advance(900)
    assert (await reaper.scan_once()).contracts_cancelled == 1
    persisted = await contract_state.get_exact(contract.key)
    assert persisted is not None
    assert ContractRecord.model_validate(persisted.value).state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_unavailable_status_does_not_create_or_advance_stale_observation() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(
        contract_state,
        UnavailableGetKvBucket(token_state),
        maintenance_state,
    )
    contract = await _contract(harness, contract_id="unavailable-contract")
    reaper = _reaper(harness.maintenance, clock)

    assert (await harness.maintenance.validate_exact(contract)).status == (
        ContractValidityStatus.UNAVAILABLE
    )
    result = await reaper.scan_once()
    assert result.scanned_contract_count == 1
    assert result.stale_observations_created == 0
    assert result.contracts_cancelled == 0
    await _assert_no_stale_observation(maintenance_state, contract)

    clock.advance(3600)
    result = await reaper.scan_once()

    assert result.stale_observations_created == 0
    assert result.contracts_cancelled == 0
    current = await contract_state.get(contract.key)
    assert current is not None
    assert ContractRecord.model_validate(current.value).state == ContractState.OPEN
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_malformed_open_contract_is_observed_but_not_mutated() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    key = concord_contract_key(contract_id="malformed-contract", generation=1)
    malformed = {
        "schema": CONCORD_CONTRACT_SCHEMA_ID,
        "contractId": "malformed-contract",
        "generation": 1,
        "state": "open",
    }
    seeded = await contract_state.create(key, malformed)
    harness = _harness(contract_state, token_state, maintenance_state)
    reaper = _reaper(harness.maintenance, clock, stale_grace_seconds=0)

    result = await reaper.scan_once()

    assert result.stale_observations_created == 1
    assert result.contracts_cancelled == 0
    assert await contract_state.get_exact(key) == seeded
    observation_entry = await maintenance_state.get_exact(
        concord_stale_observation_key(
            contract_id="malformed-contract",
            generation=1,
        )
    )
    assert observation_entry is not None
    observation = ConcordStaleObservationRecord.model_validate(observation_entry.value)
    assert observation.status == ContractValidityStatus.INVALID_CONTRACT


@pytest.mark.asyncio
async def test_malformed_token_makes_open_contract_stale_and_cancellable() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="malformed-token-contract")
    controller = controller_address("controller-main")
    token = await harness.concord._attach(
        contract,
        controller,
        "controller-session",
    )
    await token_state.put(
        token.key,
        {
            "schema": "dev.deckr.concord.participant-token.v1",
            "contractId": contract.contract_id,
        },
    )
    reaper = _reaper(harness.maintenance, clock, stale_grace_seconds=0)

    result = await reaper.scan_once()

    assert result.contracts_cancelled == 1
    persisted = await contract_state.get_exact(contract.key)
    assert persisted is not None
    assert ContractRecord.model_validate(persisted.value).state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_stale_observation_removed_when_contract_cancelled() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="participant-cancelled")
    reaper = _reaper(harness.maintenance, clock)
    await reaper.scan_once()

    assert await harness.concord._cancel(
        contract,
        controller_address("controller-main"),
        reason="participant_cancelled",
    )
    result = await reaper.scan_once()

    assert result.stale_observations_cleared == 1
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_stale_observation_removed_when_contract_deleted() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="externally-deleted")
    reaper = _reaper(harness.maintenance, clock)
    await reaper.scan_once()

    await contract_state.delete(
        contract.key,
        revision=contract.revision,
    )
    result = await reaper.scan_once()

    assert result.stale_observations_cleared == 1
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_cancelled_contract_deleted_after_retention_and_tokens_removed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="deckr.concord")
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="deleted-contract")
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    controller_token = await harness.concord._attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )
    manager_token = await harness.concord._attach(
        contract,
        manager,
        "manager-session",
        token_id="manager-token",
    )
    assert await harness.maintenance.cancel_contract(contract, now=clock())
    reaper = _reaper(harness.maintenance, clock)

    clock.advance(3599)
    result = await reaper.scan_once()
    assert result.contracts_deleted == 0
    assert await contract_state.get_exact(contract.key) is not None

    clock.advance(1)
    result = await reaper.scan_once()

    assert result.contracts_deleted == 1
    assert result.token_keys_deleted == 2
    assert await contract_state.get_exact(contract.key) is None
    assert await token_state.get_exact(controller_token.key) is None
    assert await token_state.get_exact(manager_token.key) is None
    await _assert_no_stale_observation(maintenance_state, contract)

    log_text = caplog.text
    assert "Concord maintenance deleting cancelled contract" in log_text
    assert "contract_key=" in log_text
    assert "contract=deleted-contract" in log_text
    assert f"profile={PROFILE}" in log_text
    assert "state=cancelled" in log_text
    assert "created_by=controller:controller-main" in log_text
    assert "cancelled_by=concord:maintenance" in log_text
    assert f"cancel_reason={CONCORD_REAPER_STALE_CONTRACT_REASON}" in log_text
    assert canonical_json_hash({"profile": PROFILE, "contract": "deleted-contract"}) in log_text
    assert "controller:controller-main" in log_text
    assert "hardware_manager:manager-main" in log_text
    assert "controller-session" in log_text
    assert "manager-session" in log_text
    assert "deleted_token_key_count=2" in log_text


@pytest.mark.asyncio
async def test_cancellation_revision_race_preserves_newer_contract() -> None:
    clock = ManualClock()
    contract_state = RacingContractUpdateStore()
    token_state = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    maintenance_state = MemoryJsonKvBucket(bucket="maintenance")
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="cancel-race")
    reaper = _reaper(harness.maintenance, clock, stale_grace_seconds=0)
    contract_state.race_on_update = True

    first = await reaper.scan_once()

    assert contract_state.raced
    assert first.contracts_cancelled == 0
    raced_entry = await contract_state.get_exact(contract.key)
    assert raced_entry is not None
    assert raced_entry.revision > contract.revision
    assert ContractRecord.model_validate(raced_entry.value).state == ContractState.OPEN

    second = await reaper.scan_once()

    assert second.contracts_cancelled == 1
    cancelled_entry = await contract_state.get_exact(contract.key)
    assert cancelled_entry is not None
    assert ContractRecord.model_validate(cancelled_entry.value).state == (
        ContractState.CANCELLED
    )


@pytest.mark.asyncio
async def test_retained_delete_revision_race_preserves_newer_contract() -> None:
    clock = ManualClock()
    contract_state = RacingContractDeleteStore()
    token_state = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    maintenance_state = MemoryJsonKvBucket(bucket="maintenance")
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="delete-race")
    assert await harness.maintenance.cancel_contract(contract, now=clock())
    contract_state.race_on_delete = True
    reaper = _reaper(
        harness.maintenance,
        clock,
        cancelled_retention_seconds=0,
    )

    first = await reaper.scan_once()

    assert contract_state.raced
    assert first.contracts_deleted == 0
    assert await contract_state.get_exact(contract.key) is not None

    second = await reaper.scan_once()

    assert second.contracts_deleted == 1
    assert await contract_state.get_exact(contract.key) is None


@pytest.mark.asyncio
async def test_observation_create_race_preserves_earliest_timestamp() -> None:
    clock = ManualClock()
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    maintenance_state = RacingObservationCreateStore()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="observation-create-race")
    reaper = _reaper(harness.maintenance, clock)

    result = await reaper.scan_once()
    observation = await _stale_observation(maintenance_state, contract)

    assert maintenance_state.raced
    assert result.stale_observations_created == 0
    assert observation.first_observed_stale_at == clock() - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_observation_delete_race_preserves_changed_observation() -> None:
    clock = ManualClock()
    contract_state = MemoryJsonKvBucket(bucket="contracts")
    token_state = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    maintenance_state = RacingObservationDeleteStore()
    harness = _harness(contract_state, token_state, maintenance_state)
    contract = await _contract(harness, contract_id="observation-delete-race")
    reaper = _reaper(harness.maintenance, clock)
    await reaper.scan_once()
    assert await harness.concord._cancel(
        contract,
        controller_address("controller-main"),
        reason="participant cancelled",
    )
    maintenance_state.race_on_delete = True

    result = await reaper.scan_once()
    observation = await _stale_observation(maintenance_state, contract)

    assert maintenance_state.raced
    assert result.stale_observations_cleared == 0
    assert observation.reason == "raced replacement"


def test_component_factory_wires_default_stores_and_config_overrides() -> None:
    deckr = mock_deckr()
    calls = []

    def kv_bucket_for(policy):
        calls.append((policy.bucket, policy))
        return deckr._message_bus.kv_bucket(policy)

    context = ComponentContext(
        component_id=CONCORD_REAPER_COMPONENT_ID,
        instance_id="main",
        runtime_name="dev.deckr.concord.reaper:main",
        manifest=component.manifest,
        config={
            "scan_interval_seconds": 5,
            "stale_grace_seconds": 10,
            "cancelled_retention_seconds": 20,
            "log_label": "ConfiguredReaper",
        },
        endpoints={},
        base_dir=Path.cwd(),
        lanes=deckr.lanes,
        kv_bucket_for=lambda _policy: pytest.fail(
            "reaper must not use generic component KV"
        ),
        _concord_maintenance_stores_for=lambda: concord_maintenance_stores(
            kv_bucket_for
        ),
    )

    created = component.factory(context)

    assert component.manifest.consumes == ()
    assert component.manifest.publishes == ()
    assert component.manifest.endpoint_slots == ()
    assert created.service.config.scan_interval_seconds == 5
    assert created.service.config.stale_grace_seconds == 10
    assert created.service.config.cancelled_retention_seconds == 20
    assert created.service.config.log_label == "ConfiguredReaper"
    assert calls == [
        (DEFAULT_CONCORD_CONTRACT_BUCKET_NAME, CONCORD_CONTRACT_BUCKET_POLICY),
        (DEFAULT_CONCORD_TOKEN_BUCKET_NAME, CONCORD_TOKEN_BUCKET_POLICY),
        (DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME, CONCORD_MAINTENANCE_BUCKET_POLICY),
    ]
