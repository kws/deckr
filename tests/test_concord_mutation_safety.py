from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from deckr.concord import (
    CONCORD_CONTRACT_SCHEMA_ID,
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    ConcordConflict,
    ConcordConflictCode,
    ContractHandle,
    ParticipantHandle,
    ParticipantTokenRecord,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr.concord_maintenance import (
    ConcordReaperConfig,
    ConcordReaperService,
    ConcordTokenCleanupRecord,
    concord_stale_observation_key,
    concord_token_cleanup_key,
)
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.substrates.nats_kv import KvConflict, KvEntry, KvUnavailable
from deckr.testing import (
    ConcordMaintenanceHarness,
    ConcordRuntimeHarness,
    MemoryJsonKvBucket,
)

CONTROLLER = controller_address("controller-main")
MANAGER = hardware_manager_address("manager-main")
PARTICIPANTS = tuple(sorted((CONTROLLER, MANAGER), key=str))
PROFILE = "dev.deckr.test.mutation-safety.v1"


class AlwaysConflictingContractStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="contracts")
        self.reject_updates = False
        self.get_attempts = 0
        self.update_attempts = 0

    async def get(self, key: str):
        if self.reject_updates:
            self.get_attempts += 1
        return await super().get(key)

    async def update(self, *args, **kwargs):
        if self.reject_updates:
            self.update_attempts += 1
            raise KvConflict("misleading create conflict diagnostic")
        return await super().update(*args, **kwargs)


class MisleadingCreateConflictStore(MemoryJsonKvBucket):
    async def create(self, *args, **kwargs):
        try:
            return await super().create(*args, **kwargs)
        except KvConflict as exc:
            raise KvConflict("misleading revision-changed diagnostic") from exc


class RacingTokenDeleteStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.race_on_delete = False
        self.raced = False
        self.replacement_token_id = "replacement-token"

    async def delete(self, key: str, *, revision: int | None = None):
        if self.race_on_delete and not self.raced:
            current = await self.get_exact(key)
            assert current is not None
            token = ParticipantTokenRecord.model_validate(current.value)
            replacement = token.model_copy(
                update={
                    "refresh_seq": token.refresh_seq + 1,
                    "session_id": "replacement-session",
                    "token_id": self.replacement_token_id,
                }
            )
            await super().update(key, replacement, revision=current.revision)
            self.raced = True
        return await super().delete(key, revision=revision)


class AlwaysRefreshingTokenDeleteStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.refresh_on_delete = False
        self.delete_attempts = 0

    async def delete(self, key: str, *, revision: int | None = None):
        if self.refresh_on_delete:
            self.delete_attempts += 1
            current = await self.get_exact(key)
            assert current is not None
            token = ParticipantTokenRecord.model_validate(current.value)
            await super().update(
                key,
                token.model_copy(update={"refresh_seq": token.refresh_seq + 1}),
                revision=current.revision,
            )
        return await super().delete(key, revision=revision)


class ToggleUnavailableTokenStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.unavailable = False

    def _check_available(self) -> None:
        if self.unavailable:
            raise KvUnavailable("token store unavailable after contract deletion")

    async def get(self, key: str):
        self._check_available()
        return await super().get(key)

    async def items(self, prefix: str = ""):
        self._check_available()
        return await super().items(prefix)

    async def delete(self, key: str, *, revision: int | None = None):
        self._check_available()
        return await super().delete(key, revision=revision)


class UnavailableAfterOneTokenDeleteStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.unavailable = False
        self.successful_delete_count = 0

    def _check_available(self) -> None:
        if self.unavailable:
            raise KvUnavailable("token store became unavailable during cleanup")

    async def get(self, key: str):
        self._check_available()
        return await super().get(key)

    async def items(self, prefix: str = ""):
        self._check_available()
        return await super().items(prefix)

    async def delete(self, key: str, *, revision: int | None = None):
        self._check_available()
        result = await super().delete(key, revision=revision)
        self.successful_delete_count += 1
        if self.successful_delete_count == 1:
            self.unavailable = True
        return result


class ContractDeleteMakesTokenStoreUnavailable(MemoryJsonKvBucket):
    def __init__(self, token_store: ToggleUnavailableTokenStore) -> None:
        super().__init__(bucket="contracts")
        self._token_store = token_store

    async def delete(self, key: str, *, revision: int | None = None):
        result = await super().delete(key, revision=revision)
        self._token_store.unavailable = True
        return result


class MismatchedTokenScanEntryStore(MemoryJsonKvBucket):
    def __init__(self) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.return_mismatched_key = False

    async def items(self, prefix: str = ""):
        entries = await super().items(prefix)
        if not self.return_mismatched_key or not entries:
            return entries
        entry = entries[0]
        return (
            KvEntry(
                entry.bucket,
                concord_participant_token_key(
                    contract_id="different-contract",
                    generation=1,
                    participant=CONTROLLER,
                ),
                entry.value,
                entry.revision,
            ),
        )


class FaultingTokenCleanupRescanStore(MemoryJsonKvBucket):
    def __init__(self, fault: str) -> None:
        super().__init__(bucket="tokens", ttl_seconds=120)
        self.fault: str | None = fault
        self.scan_count = 0
        self.injected = False

    async def items(self, prefix: str = ""):
        entries = await super().items(prefix)
        self.scan_count += 1
        if self.fault == "key" and self.injected and entries:
            entry = entries[0]
            return (
                KvEntry(
                    entry.bucket,
                    concord_participant_token_key(
                        contract_id="different-contract",
                        generation=1,
                        participant=CONTROLLER,
                    ),
                    entry.value,
                    entry.revision,
                ),
            )
        return entries

    async def delete(self, key: str, *, revision: int | None = None):
        if self.fault is not None and not self.injected and self.scan_count >= 1:
            self.injected = True
            if self.fault == "key":
                raise KvConflict("token refreshed while cleanup deleted it")
            current = await self.get_exact(key)
            assert current is not None
            replacement = dict(current.value)
            if self.fault == "malformed":
                replacement = {
                    "schema": CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
                    "contractId": replacement["contractId"],
                }
            else:
                replacement["contractId"] = "different-contract"
            await super().put(key, replacement)
        return await super().delete(key, revision=revision)


async def _create_contract(
    harness: ConcordRuntimeHarness,
    *,
    contract_id: str = "mutation-contract",
) -> ContractHandle:
    return await harness.concord._create_contract(  # noqa: SLF001
        PARTICIPANTS,
        contract_id=contract_id,
        profile=PROFILE,
        created_by=CONTROLLER,
    )


async def _attach(
    harness: ConcordRuntimeHarness,
    contract: ContractHandle,
) -> ParticipantHandle:
    return await harness.concord._attach(  # noqa: SLF001
        contract,
        CONTROLLER,
        "controller-session",
        token_id="controller-token",
    )


def _store_state(store: MemoryJsonKvBucket) -> tuple[int, int]:
    return store.revision, store.mutation_count


def _authority_state(
    harness: ConcordRuntimeHarness,
) -> tuple[tuple[int, int], ...]:
    stores = [harness.contract_store, harness.token_store]
    maintenance_store = getattr(harness, "maintenance_store", None)
    if maintenance_store is not None:
        stores.append(maintenance_store)
    return tuple(_store_state(store) for store in stores)


async def _contract_value(
    harness: ConcordRuntimeHarness,
    contract: ContractHandle,
) -> dict[str, Any]:
    entry = await harness.contract_entry(contract.key)
    assert entry is not None
    return dict(entry.value)


async def _token_value(
    harness: ConcordRuntimeHarness,
    token: ParticipantHandle,
) -> dict[str, Any]:
    entry = await harness.token_entry(token)
    assert entry is not None
    return dict(entry.value)


async def _corrupt_contract(
    harness: ConcordRuntimeHarness,
    contract: ContractHandle,
    corruption: str,
) -> None:
    if corruption == "malformed":
        value = {
            "schema": CONCORD_CONTRACT_SCHEMA_ID,
            "contractId": contract.contract_id,
        }
    else:
        value = await _contract_value(harness, contract)
        value["contractId"] = f"{contract.contract_id}-replacement"
    await harness.seed_raw_contract(contract.key, value)


async def _corrupt_token(
    harness: ConcordRuntimeHarness,
    token: ParticipantHandle,
    corruption: str,
    *,
    attach_path: bool = False,
) -> None:
    if corruption == "malformed":
        value = {
            "schema": CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
            "contractId": token.contract_id,
        }
    else:
        value = await _token_value(harness, token)
        if attach_path:
            value["contractId"] = f"{token.contract_id}-replacement"
        else:
            value["sessionId"] = "replacement-session"
    await harness.seed_raw_token(token.key, value)


async def _seed_unattached_token(
    harness: ConcordRuntimeHarness,
    contract: ContractHandle,
) -> ParticipantHandle:
    return await harness.seed_token(
        ParticipantTokenRecord(
            contractId=contract.contract_id,
            generation=contract.generation,
            participant=CONTROLLER,
            sessionId="controller-session",
            tokenId="controller-token",
            refreshSeq=1,
            ttlSeconds=120,
            termsHash=contract.terms_hash,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["attach", "cancel", "maintenance"])
async def test_noncanonical_contract_handle_performs_no_writes(operation: str) -> None:
    harness: ConcordRuntimeHarness
    if operation == "maintenance":
        harness = ConcordMaintenanceHarness()
    else:
        harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    noncanonical = replace(
        contract,
        key=concord_contract_key(
            contract_id=f"{contract.contract_id}-replacement",
            generation=contract.generation,
        ),
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        if operation == "attach":
            await harness.concord._attach(  # noqa: SLF001
                noncanonical,
                CONTROLLER,
                "controller-session",
            )
        elif operation == "cancel":
            await harness.concord._cancel(  # noqa: SLF001
                noncanonical,
                CONTROLLER,
            )
        else:
            assert isinstance(harness, ConcordMaintenanceHarness)
            await harness.maintenance.cancel_contract(noncanonical)

    assert raised.value.code == ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH
    assert _authority_state(harness) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["refresh", "withdraw"])
async def test_noncanonical_participant_handle_performs_no_writes(
    operation: str,
) -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    token = await _attach(harness, contract)
    noncanonical = replace(
        token,
        key=concord_participant_token_key(
            contract_id=token.contract_id,
            generation=token.generation,
            participant=MANAGER,
        ),
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        if operation == "refresh":
            await harness.concord._refresh_token(noncanonical)  # noqa: SLF001
        else:
            await harness.concord._withdraw_token(noncanonical)  # noqa: SLF001

    assert raised.value.code == ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
    assert _authority_state(harness) == before


@pytest.mark.asyncio
async def test_mismatched_contract_entry_key_performs_no_writes() -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    entry = await harness.contract_entry(contract.key)
    assert entry is not None
    harness.contract_store._entries[contract.key] = KvEntry(  # noqa: SLF001
        entry.bucket,
        concord_contract_key(contract_id="replacement-contract", generation=1),
        entry.value,
        entry.revision,
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await harness.concord._cancel(contract, CONTROLLER)  # noqa: SLF001

    assert raised.value.code == ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH
    assert _authority_state(harness) == before


@pytest.mark.asyncio
async def test_mismatched_token_entry_key_performs_no_writes() -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    token = await _attach(harness, contract)
    entry = await harness.token_entry(token)
    assert entry is not None
    harness.token_store._entries[token.key] = KvEntry(  # noqa: SLF001
        entry.bucket,
        concord_participant_token_key(
            contract_id=token.contract_id,
            generation=token.generation,
            participant=MANAGER,
        ),
        entry.value,
        entry.revision,
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await harness.concord._withdraw_token(token)  # noqa: SLF001

    assert raised.value.code == ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
    assert _authority_state(harness) == before


INVALID_RECORD_CASES = (
    ("attach", "contract", "malformed", ConcordConflictCode.CONTRACT_INVALID),
    (
        "attach",
        "contract",
        "identity",
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
    ),
    ("attach", "token", "malformed", ConcordConflictCode.TOKEN_INVALID),
    (
        "attach",
        "token",
        "identity",
        ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
    ),
    ("refresh", "contract", "malformed", ConcordConflictCode.CONTRACT_INVALID),
    (
        "refresh",
        "contract",
        "identity",
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
    ),
    ("refresh", "token", "malformed", ConcordConflictCode.TOKEN_INVALID),
    (
        "refresh",
        "token",
        "identity",
        ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
    ),
    ("withdraw", "token", "malformed", ConcordConflictCode.TOKEN_INVALID),
    (
        "withdraw",
        "token",
        "identity",
        ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
    ),
    ("cancel", "contract", "malformed", ConcordConflictCode.CONTRACT_INVALID),
    (
        "cancel",
        "contract",
        "identity",
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
    ),
    (
        "maintenance",
        "contract",
        "malformed",
        ConcordConflictCode.CONTRACT_INVALID,
    ),
    (
        "maintenance",
        "contract",
        "identity",
        ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "record_kind", "corruption", "expected_code"),
    INVALID_RECORD_CASES,
)
async def test_invalid_external_records_fail_closed_without_writes(
    operation: str,
    record_kind: str,
    corruption: str,
    expected_code: ConcordConflictCode,
) -> None:
    harness: ConcordRuntimeHarness
    if operation == "maintenance":
        harness = ConcordMaintenanceHarness()
    else:
        harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    token: ParticipantHandle | None = None

    if operation in {"refresh", "withdraw"}:
        token = await _attach(harness, contract)
    elif record_kind == "token":
        token = await _seed_unattached_token(harness, contract)

    if record_kind == "contract":
        await _corrupt_contract(harness, contract, corruption)
    else:
        assert token is not None
        await _corrupt_token(
            harness,
            token,
            corruption,
            attach_path=operation == "attach",
        )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        if operation == "attach":
            await harness.concord._attach(  # noqa: SLF001
                contract,
                CONTROLLER,
                "controller-session",
                token_id="controller-token",
            )
        elif operation == "refresh":
            assert token is not None
            await harness.concord._refresh_token(token)  # noqa: SLF001
        elif operation == "withdraw":
            assert token is not None
            await harness.concord._withdraw_token(token)  # noqa: SLF001
        elif operation == "cancel":
            await harness.concord._cancel(contract, CONTROLLER)  # noqa: SLF001
        else:
            assert isinstance(harness, ConcordMaintenanceHarness)
            await harness.maintenance.cancel_contract(contract)

    assert raised.value.code == expected_code
    assert _authority_state(harness) == before


@pytest.mark.asyncio
async def test_duplicate_contract_create_maps_conflict_by_operation() -> None:
    contract_store = MisleadingCreateConflictStore(bucket="contracts")
    harness = ConcordRuntimeHarness(contract_store=contract_store)
    await _create_contract(harness)
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await _create_contract(harness)

    assert raised.value.code == ConcordConflictCode.KEY_ALREADY_EXISTS
    assert "revision-changed" in str(raised.value)
    assert _authority_state(harness) == before


@pytest.mark.asyncio
async def test_attached_participant_cas_stops_after_eight_conflicts() -> None:
    contract_store = AlwaysConflictingContractStore()
    harness = ConcordRuntimeHarness(contract_store=contract_store)
    contract = await _create_contract(harness)
    contract_state = _store_state(contract_store)
    contract_store.reject_updates = True

    with pytest.raises(ConcordConflict) as raised:
        await _attach(harness, contract)

    assert raised.value.code == ConcordConflictCode.REVISION_CHANGED
    assert contract_store.update_attempts == 8
    assert contract_store.get_attempts == 9
    assert _store_state(contract_store) == contract_state
    token_key = concord_participant_token_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
        participant=CONTROLLER,
    )
    assert await harness.token_entry(token_key) is not None


@pytest.mark.asyncio
async def test_raced_maintenance_token_delete_removes_refreshed_token() -> None:
    token_store = RacingTokenDeleteStore()
    harness = ConcordMaintenanceHarness(token_store=token_store)
    contract = await _create_contract(harness)
    token = await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )
    token_store.race_on_delete = True

    result = await harness.maintenance.delete_cancelled_contract(
        contract,
        retention_seconds=0,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert result.contract_deleted
    assert result.cleanup_completed
    assert not result.cleanup_pending
    assert result.deleted_token_key_count == 1
    assert token_store.raced
    assert await harness.token_entry(token.key) is None


@pytest.mark.asyncio
async def test_token_cleanup_exhaustion_remains_durably_pending() -> None:
    token_store = AlwaysRefreshingTokenDeleteStore()
    harness = ConcordMaintenanceHarness(token_store=token_store)
    contract = await _create_contract(harness, contract_id="cleanup-exhaustion")
    token = await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )
    token_store.refresh_on_delete = True

    result = await harness.maintenance.delete_cancelled_contract(
        contract,
        retention_seconds=0,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    marker_key = concord_token_cleanup_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    marker_entry = await harness.maintenance_store.get_exact(marker_key)
    assert result.contract_deleted
    assert not result.cleanup_completed
    assert result.cleanup_pending
    assert result.deleted_token_key_count == 0
    assert token_store.delete_attempts == 8
    assert await harness.contract_entry(contract.key) is None
    assert await harness.token_entry(token.key) is not None
    assert marker_entry is not None
    assert ConcordTokenCleanupRecord.model_validate(marker_entry.value).contract_id == (
        contract.contract_id
    )

    token_store.refresh_on_delete = False
    reaper = ConcordReaperService(
        harness.maintenance,
        config=ConcordReaperConfig(cancelledRetentionSeconds=0),
    )
    later = await reaper.scan_once()

    assert later.contracts_deleted == 0
    assert later.token_keys_deleted == 1
    assert later.token_cleanups_completed == 1
    assert later.token_cleanups_pending == 0
    assert await harness.token_entry(token.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is None


@pytest.mark.asyncio
async def test_post_contract_delete_store_unavailability_remains_durably_pending() -> None:
    token_store = ToggleUnavailableTokenStore()
    contract_store = ContractDeleteMakesTokenStoreUnavailable(token_store)
    harness = ConcordMaintenanceHarness(
        contract_store=contract_store,
        token_store=token_store,
    )
    contract = await _create_contract(harness, contract_id="cleanup-unavailable")
    token = await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )

    result = await harness.maintenance.delete_cancelled_contract(
        contract,
        retention_seconds=0,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    marker_key = concord_token_cleanup_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    assert result.contract_deleted
    assert not result.cleanup_completed
    assert result.cleanup_pending
    assert await harness.contract_entry(contract.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is not None

    token_store.unavailable = False
    assert await harness.token_entry(token.key) is not None
    reaper = ConcordReaperService(
        harness.maintenance,
        config=ConcordReaperConfig(cancelledRetentionSeconds=0),
    )
    later = await reaper.scan_once()

    assert later.token_keys_deleted == 1
    assert later.token_cleanups_completed == 1
    assert later.token_cleanups_pending == 0
    assert await harness.token_entry(token.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is None


@pytest.mark.asyncio
async def test_partial_token_cleanup_count_is_preserved_and_later_completed() -> None:
    token_store = UnavailableAfterOneTokenDeleteStore()
    harness = ConcordMaintenanceHarness(token_store=token_store)
    contract = await _create_contract(harness, contract_id="cleanup-partial-count")
    controller_token = await _attach(harness, contract)
    manager_token = await harness.concord._attach(  # noqa: SLF001
        contract,
        MANAGER,
        "manager-session",
        token_id="manager-token",
    )
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )
    reaper = ConcordReaperService(
        harness.maintenance,
        config=ConcordReaperConfig(cancelledRetentionSeconds=0),
    )

    first = await reaper.scan_once()

    marker_key = concord_token_cleanup_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    assert first.contracts_deleted == 1
    assert first.token_keys_deleted == 1
    assert first.token_cleanups_completed == 0
    assert first.token_cleanups_pending == 1
    assert token_store.successful_delete_count == 1
    assert await harness.contract_entry(contract.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is not None

    token_store.unavailable = False
    remaining = [
        token
        for token in (
            await harness.token_entry(controller_token.key),
            await harness.token_entry(manager_token.key),
        )
        if token is not None
    ]
    assert len(remaining) == 1

    second = await reaper.scan_once()

    assert second.contracts_deleted == 0
    assert second.token_keys_deleted == 1
    assert second.token_cleanups_completed == 1
    assert second.token_cleanups_pending == 0
    assert token_store.successful_delete_count == 2
    assert await harness.token_entry(controller_token.key) is None
    assert await harness.token_entry(manager_token.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is None


@pytest.mark.asyncio
async def test_token_scan_entry_key_mismatch_prevents_contract_deletion() -> None:
    token_store = MismatchedTokenScanEntryStore()
    harness = ConcordMaintenanceHarness(token_store=token_store)
    contract = await _create_contract(harness, contract_id="cleanup-key-mismatch")
    token = await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )
    token_store.return_mismatched_key = True
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await harness.maintenance.delete_cancelled_contract(
            contract,
            retention_seconds=0,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )

    marker_key = concord_token_cleanup_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    assert raised.value.code == ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
    assert _authority_state(harness) == before
    assert await harness.contract_entry(contract.key) is not None
    assert await harness.token_entry(token.key) is not None
    assert await harness.maintenance_store.get_exact(marker_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["key", "malformed", "identity"])
async def test_post_delete_rescan_fault_retains_marker_until_later_completion(
    fault: str,
) -> None:
    token_store = FaultingTokenCleanupRescanStore(fault)
    harness = ConcordMaintenanceHarness(token_store=token_store)
    contract = await _create_contract(
        harness,
        contract_id=f"cleanup-rescan-{fault}",
    )
    token = await _attach(harness, contract)
    token_entry = await harness.token_entry(token.key)
    assert token_entry is not None
    original_token = token_entry.value
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="ready for retention cleanup",
    )

    result = await harness.maintenance.delete_cancelled_contract(
        contract,
        retention_seconds=0,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    marker_key = concord_token_cleanup_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    assert result.contract_deleted
    assert not result.cleanup_completed
    assert result.cleanup_pending
    assert await harness.contract_entry(contract.key) is None
    assert await harness.token_entry(token.key) is not None
    assert await harness.maintenance_store.get_exact(marker_key) is not None

    token_store.fault = None
    await token_store.put(token.key, original_token)
    reaper = ConcordReaperService(
        harness.maintenance,
        config=ConcordReaperConfig(cancelledRetentionSeconds=0),
    )
    later = await reaper.scan_once()

    assert later.token_keys_deleted == 1
    assert later.token_cleanups_completed == 1
    assert later.token_cleanups_pending == 0
    assert await harness.token_entry(token.key) is None
    assert await harness.maintenance_store.get_exact(marker_key) is None


@pytest.mark.asyncio
async def test_participant_lease_refresh_normalizes_malformed_token_conflict() -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    lease = harness.concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
        token_id="controller-token",
    )
    token = await lease.attach_or_refresh()
    await harness.seed_raw_token(
        token.key,
        {
            "schema": CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
            "contractId": token.contract_id,
        },
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await lease.attach_or_refresh()

    assert raised.value.code == ConcordConflictCode.TOKEN_INVALID
    assert lease.token is None
    assert _authority_state(harness) == before
    with pytest.raises(ConcordConflict) as closed:
        await lease.attach_or_refresh()
    assert closed.value.code == ConcordConflictCode.LEASE_CLOSED


@pytest.mark.asyncio
async def test_malformed_heartbeat_state_stays_within_lease_task_scope() -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    lease = harness.concord._participant_lease(  # noqa: SLF001
        contract=contract,
        participant=CONTROLLER,
        session_id="controller-session",
        token_id="controller-token",
        refresh_interval=0.01,
    )
    token = await lease.attach_or_refresh()
    await harness.seed_raw_token(
        token.key,
        {
            "schema": CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
            "contractId": token.contract_id,
        },
    )
    lease._refresh_interval = 0  # noqa: SLF001

    await lease.heartbeat_loop()

    assert lease.token is None
    with pytest.raises(ConcordConflict) as closed:
        await lease.attach_or_refresh()
    assert closed.value.code == ConcordConflictCode.LEASE_CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["refresh", "validate", "withdraw"])
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("contractId", "replacement-contract"),
        ("generation", 2),
        ("participant", str(MANAGER)),
        ("sessionId", "replacement-session"),
        ("tokenId", "replacement-token"),
        ("termsHash", "sha256:replacement"),
    ),
)
async def test_all_token_identity_fields_are_fenced_without_writes(
    operation: str,
    field: str,
    replacement: str | int,
) -> None:
    harness = ConcordRuntimeHarness()
    contract = await _create_contract(harness)
    token = await _attach(harness, contract)
    value = await _token_value(harness, token)
    value[field] = replacement
    await harness.seed_raw_token(token.key, value)
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        if operation == "refresh":
            await harness.concord._refresh_token(token)  # noqa: SLF001
        elif operation == "validate":
            await harness.concord._validate_participant_token(token)  # noqa: SLF001
        else:
            await harness.concord._withdraw_token(token)  # noqa: SLF001

    assert raised.value.code == ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
    assert _authority_state(harness) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["malformed", "identity"])
async def test_retained_contract_cleanup_fails_closed_before_any_write(
    corruption: str,
) -> None:
    harness = ConcordMaintenanceHarness()
    contract = await _create_contract(harness)
    await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="retained cleanup",
    )
    if corruption == "malformed":
        value = {
            "schema": CONCORD_CONTRACT_SCHEMA_ID,
            "contractId": contract.contract_id,
            "generation": contract.generation,
            "state": "cancelled",
        }
        expected = ConcordConflictCode.CONTRACT_INVALID
    else:
        value = await _contract_value(harness, contract)
        value["contractId"] = "replacement-contract"
        expected = ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH
    await harness.seed_raw_contract(contract.key, value)
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await harness.maintenance.delete_cancelled_contract(
            contract,
            retention_seconds=0,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )

    assert raised.value.code == expected
    assert _authority_state(harness) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["malformed", "identity"])
async def test_token_cleanup_fails_closed_before_contract_delete(
    corruption: str,
) -> None:
    harness = ConcordMaintenanceHarness()
    contract = await _create_contract(harness)
    token = await _attach(harness, contract)
    assert await harness.concord._cancel(  # noqa: SLF001
        contract,
        CONTROLLER,
        reason="retained cleanup",
    )
    await _corrupt_token(
        harness,
        token,
        corruption,
        attach_path=corruption == "identity",
    )
    before = _authority_state(harness)

    with pytest.raises(ConcordConflict) as raised:
        await harness.maintenance.delete_cancelled_contract(
            contract,
            retention_seconds=0,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )

    expected = (
        ConcordConflictCode.TOKEN_INVALID
        if corruption == "malformed"
        else ConcordConflictCode.TOKEN_IDENTITY_MISMATCH
    )
    assert raised.value.code == expected
    assert _authority_state(harness) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["malformed", "identity"])
async def test_maintenance_observation_identity_failure_performs_no_writes(
    corruption: str,
) -> None:
    harness = ConcordMaintenanceHarness()
    contract = await _create_contract(harness)
    key = concord_stale_observation_key(
        contract_id=contract.contract_id,
        generation=contract.generation,
    )
    if corruption == "malformed":
        value = {"contractId": contract.contract_id}
    else:
        value = {
            "schema": "dev.deckr.concord.stale-observation.v1",
            "contractId": "replacement-contract",
            "generation": contract.generation,
            "firstObservedStaleAt": datetime.now(UTC).isoformat(),
            "status": "missing_token",
        }
    await harness.maintenance_store.put(key, value)
    before = _authority_state(harness)
    reaper = ConcordReaperService(
        harness.maintenance,
        config=ConcordReaperConfig(
            staleGraceSeconds=0,
            cancelledRetentionSeconds=0,
            scanIntervalSeconds=60,
        ),
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
    )

    await reaper.scan_once()

    assert _authority_state(harness) == before
