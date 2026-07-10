from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from message_bus_mocks import mock_deckr

from deckr.components import ComponentContext
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_REAPER_STALE_CONTRACT_REASON,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_CONTRACT_BUCKET_NAME,
    DEFAULT_CONCORD_MAINTENANCE_BUCKET_NAME,
    DEFAULT_CONCORD_TOKEN_BUCKET_NAME,
    Concord,
    ConcordReaperConfig,
    ConcordReaperService,
    ConcordStaleObservationRecord,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractState,
    ContractValidityStatus,
    canonical_json_hash,
    concord_stale_observation_key,
)
from deckr.concord_reaper import CONCORD_REAPER_COMPONENT_ID, component
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvChange
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


def _stores():
    return (
        MemoryJsonKvBucket(bucket="contracts"),
        MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120),
        MemoryJsonKvBucket(bucket="maintenance"),
    )


def _reaper(
    concord: Concord,
    clock: ManualClock,
    *,
    stale_grace_seconds: float = 900,
    cancelled_retention_seconds: float = 3600,
) -> ConcordReaperService:
    return ConcordReaperService(
        concord,
        config=ConcordReaperConfig(
            staleGraceSeconds=stale_grace_seconds,
            cancelledRetentionSeconds=cancelled_retention_seconds,
            scanIntervalSeconds=60,
            logLabel="TestReaper",
        ),
        clock=clock,
    )


def _concord(contract_bucket, token_bucket, maintenance_bucket) -> Concord:
    return ConcordMaintenanceHarness(
        contract_store=contract_bucket,
        token_store=token_bucket,
        maintenance_store=maintenance_bucket,
    ).concord


async def _contract(concord: Concord, *, contract_id: str):
    return await concord._create_contract(
        (controller_address("controller-main"), hardware_manager_address("manager-main")),
        contract_id=contract_id,
        profile=PROFILE,
        terms={"profile": PROFILE, "contract": contract_id},
        created_by=controller_address("controller-main"),
    )


async def _delete_token(concord: Concord, token) -> None:
    bucket = concord._coordinator.token_scan  # noqa: SLF001
    await bucket.delete(token.key, revision=token.revision)
    revision = concord._coordinator.token_source.revision_cached(token.key) or (  # noqa: SLF001
        token.revision + 1
    )
    await concord._apply_token_change(  # noqa: SLF001
        KvChange(concord.token_bucket, token.key, revision, "delete")
    )


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
    coordinator = _concord(contract_state, token_state, maintenance_state)
    await _contract(coordinator, contract_id="raw-scan-contract")
    reaper = _reaper(coordinator, clock)

    async with anyio.create_task_group() as task_group:
        reaper.start(task_group)
        result = await reaper.scan_once()
        task_group.cancel_scope.cancel()

    assert result.scanned_contract_count == 1
    assert result.stale_observations_created == 1
    assert contract_state.items_prefixes == ["contracts."]
    assert maintenance_state.items_prefixes == ["stale.", "stale."]
    assert not contract_state.watch_called
    assert not token_state.watch_called
    assert not maintenance_state.watch_called


@pytest.mark.asyncio
async def test_pending_open_contract_with_valid_token_is_not_stale() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    coordinator = _concord(contract_state, token_state, maintenance_state)
    contract = await _contract(coordinator, contract_id="pending-with-owner-token")
    await coordinator._attach(contract, controller_address("controller-main"), "session")
    reaper = _reaper(coordinator, clock)

    validity = await coordinator.validate_exact(contract)
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
    assert (await coordinator.contract_record(contract)).state == ContractState.OPEN
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_missing_token_contract_cancelled_only_after_stale_grace() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    coordinator = _concord(contract_state, token_state, maintenance_state)
    contract = await _contract(coordinator, contract_id="missing-token-contract")
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    await coordinator._attach(contract, controller, "controller-session")
    manager_token = await coordinator._attach(contract, manager, "manager-session")
    await _delete_token(coordinator, manager_token)
    reaper = _reaper(coordinator, clock)

    assert (await coordinator.validate_exact(contract)).status == ContractValidityStatus.MISSING_TOKEN
    assert (await reaper.scan_once()).contracts_cancelled == 0
    clock.advance(900)
    assert (await reaper.scan_once()).contracts_cancelled == 1
    assert (await coordinator.contract_record(contract)).state == ContractState.CANCELLED


@pytest.mark.asyncio
async def test_unavailable_status_does_not_create_or_advance_stale_observation() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    coordinator = _concord(
        contract_state,
        UnavailableGetKvBucket(token_state),
        maintenance_state,
    )
    contract = await _contract(coordinator, contract_id="unavailable-contract")
    reaper = _reaper(coordinator, clock)

    assert (await coordinator.validate_exact(contract)).status == (
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
    assert (await coordinator.contract_record(contract)).state == ContractState.OPEN
    await _assert_no_stale_observation(maintenance_state, contract)


@pytest.mark.asyncio
async def test_stale_observation_removed_when_contract_cancelled() -> None:
    clock = ManualClock()
    contract_state, token_state, maintenance_state = _stores()
    coordinator = _concord(contract_state, token_state, maintenance_state)
    contract = await _contract(coordinator, contract_id="participant-cancelled")
    reaper = _reaper(coordinator, clock)
    await reaper.scan_once()

    assert await coordinator._cancel(
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
    coordinator = _concord(contract_state, token_state, maintenance_state)
    contract = await _contract(coordinator, contract_id="externally-deleted")
    reaper = _reaper(coordinator, clock)
    await reaper.scan_once()

    await coordinator._coordinator.contract_scan.delete(  # noqa: SLF001
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
    coordinator = _concord(contract_state, token_state, maintenance_state)
    concord = coordinator
    contract = await _contract(coordinator, contract_id="deleted-contract")
    controller = controller_address("controller-main")
    manager = hardware_manager_address("manager-main")
    controller_token = await coordinator._attach(
        contract,
        controller,
        "controller-session",
        token_id="controller-token",
    )
    manager_token = await coordinator._attach(
        contract,
        manager,
        "manager-session",
        token_id="manager-token",
    )
    assert await concord.maintenance_cancel_contract(contract, now=clock())
    reaper = _reaper(coordinator, clock)

    clock.advance(3599)
    result = await reaper.scan_once()
    assert result.contracts_deleted == 0
    assert await coordinator._coordinator.contract_scan.get_exact(contract.key) is not None  # noqa: SLF001

    clock.advance(1)
    result = await reaper.scan_once()

    assert result.contracts_deleted == 1
    assert result.token_keys_deleted == 2
    assert await coordinator._coordinator.contract_scan.get_exact(contract.key) is None  # noqa: SLF001
    assert await coordinator._coordinator.token_scan.get_exact(controller_token.key) is None  # noqa: SLF001
    assert await coordinator._coordinator.token_scan.get_exact(manager_token.key) is None  # noqa: SLF001
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
        kv_bucket_for=kv_bucket_for,
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
