from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError

from deckr import beacon as beacon_module
from deckr.beacon import (
    AdvertisementRecord,
    Beacon,
    BeaconAdvertisementSpec,
    BeaconDirectory,
    BeaconProtocol,
    CandidateStatus,
    _beacon_refresh_interval,
    _beacon_ttl_seconds,
    _BeaconTaskGroup,
    _candidate_from_entry,
    _require_text,
    beacon_advertisement_key,
    parse_beacon_advertisement_key,
)
from deckr.contracts.messages import hardware_manager_address
from deckr.substrates.nats_kv import KvConflict, KvEntry, KvUnavailable
from deckr.testing import (
    LifecycleFaultHarness,
    LifecycleFaultPhase,
    MemoryJsonKvBucket,
)

MANAGER = hardware_manager_address("beacon-lifecycle-guards")
FEATURE = "dev.deckr.test.beacon-lifecycle-guards.v1"


@dataclass(frozen=True, slots=True)
class _Endpoint:
    address: object
    session_id: str


@asynccontextmanager
async def _running_beacon(
    bucket: MemoryJsonKvBucket | None = None,
) -> AsyncIterator[tuple[Beacon, MemoryJsonKvBucket]]:
    raw = bucket or MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30)
    beacon = Beacon(raw)
    async with anyio.create_task_group() as tasks:
        beacon.start(tasks)
        beacon.start(tasks)
        await beacon.wait_current()
        try:
            yield beacon, raw
        finally:
            await beacon.aclose()
            await beacon.aclose()
            tasks.cancel_scope.cancel()


def _spec() -> BeaconAdvertisementSpec:
    return BeaconAdvertisementSpec(
        feature_id=FEATURE,
        protocol=BeaconProtocol(namespace="dev.deckr.test", version="1"),
        operations=("observe",),
        labels={"site": "test"},
        hints={"priority": 1},
        payload={"ready": True},
    )


def _record(**changes) -> AdvertisementRecord:
    values = {
        "advertisementId": "advertisement",
        "featureId": FEATURE,
        "advertiser": MANAGER,
        "endpoint": MANAGER,
        "sessionId": "manager-session",
        "refreshSeq": 1,
        "ttlSeconds": 30,
        "operations": ("observe",),
        "labels": {"site": "test"},
        "hints": {"priority": 1},
        "payload": {"ready": True},
        "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
    }
    values.update(changes)
    return AdvertisementRecord.model_validate(values)


def test_beacon_text_ttl_key_and_model_guards() -> None:
    assert _require_text("value", field_name="field") == "value"
    with pytest.raises(TypeError, match="string"):
        _require_text(1, field_name="field")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="whitespace"):
        _require_text(" value", field_name="field")
    with pytest.raises(ValueError, match="empty"):
        _require_text("", field_name="field")

    with pytest.raises(KvUnavailable, match="TTL-bound"):
        _beacon_ttl_seconds(None, bucket="beacon")
    for invalid in (0, -1, 1.5):
        with pytest.raises(KvUnavailable, match="positive whole seconds"):
            _beacon_ttl_seconds(invalid, bucket="beacon")
    assert _beacon_ttl_seconds(30.0, bucket="beacon") == 30
    assert 15 <= _beacon_refresh_interval(ttl_seconds=30) <= 22.5

    key = beacon_advertisement_key(
        feature_id=FEATURE,
        advertisement_id="advertisement",
    )
    assert parse_beacon_advertisement_key(key) == (FEATURE, "advertisement")
    assert parse_beacon_advertisement_key("not.a.beacon.key") is None

    for changes in (
        {"refreshSeq": 0},
        {"ttlSeconds": 0},
        {"operations": ("",)},
        {"labels": {"": "value"}},
        {"labels": {"key": ""}},
    ):
        with pytest.raises(ValidationError):
            _record(**changes)
    assert _record(createdAt=None, updatedAt=None).to_dict()["refreshSeq"] == 1
    assert _record().to_dict()["createdAt"].endswith("Z")

    mapping_spec = BeaconAdvertisementSpec(
        feature_id=FEATURE,
        protocol={"namespace": "dev.deckr.test", "version": "1"},
    )
    assert mapping_spec.protocol is not None
    assert BeaconAdvertisementSpec(feature_id=FEATURE).protocol is None


@pytest.mark.asyncio
async def test_beacon_task_group_requires_a_started_parent() -> None:
    tasks = _BeaconTaskGroup(None)
    with pytest.raises(RuntimeError, match="must be started"):
        await tasks.start()
    await tasks.aclose()


@pytest.mark.asyncio
async def test_beacon_public_lifecycle_validation_and_updates() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon() as (beacon, _raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        with pytest.raises(RuntimeError, match="re-entered"):
            await lease.__aenter__()

        candidates = beacon.candidates(FEATURE)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert beacon.get(
            feature_id=FEATURE,
            advertisement_id=candidate.advertisement.advertisement_id,
        ) == candidate
        assert await beacon.validate(candidate) == CandidateStatus.CANDIDATE
        assert (
            await beacon.validate(
                candidate,
                current_sessions={str(MANAGER): "replacement-session"},
            )
            == CandidateStatus.SESSION_MISMATCH
        )
        assert (
            await beacon.validate(replace(candidate, revision=candidate.revision + 1))
            == CandidateStatus.MISSING
        )
        mismatched = replace(
            candidate,
            advertisement=candidate.advertisement.model_copy(
                update={"feature_id": "dev.deckr.test.other.v1"}
            ),
        )
        assert await beacon.validate(mismatched) == CandidateStatus.FEATURE_MISMATCH

        assert beacon.candidates(FEATURE, selector=lambda _record: True)

        class _Reject:
            def accepts(self, _advertisement) -> bool:
                return False

        assert beacon.candidates(FEATURE, selector=_Reject()) == ()

        await lease.update(
            payload=None,
            labels={"site": "updated"},
            hints={"priority": 2},
            operations=("observe", "inspect"),
        )
        updated = beacon.candidates(FEATURE)[0]
        assert updated.advertisement.payload is None
        assert updated.advertisement.labels == {"site": "updated"}
        with pytest.raises(TypeError, match="mapping"):
            await lease.update(payload="invalid")  # type: ignore[arg-type]

        async with beacon.watch() as changes:
            initial = await anext(changes)
            assert initial.current
            assert initial.candidates

        await lease.aclose()
        await lease.aclose()
        assert lease.closed
        with pytest.raises(KvConflict, match="closed"):
            await lease.update(labels={"site": "too-late"})
        assert await beacon.validate(candidate) == CandidateStatus.MISSING

    with pytest.raises(KvUnavailable, match="closed"):
        beacon.advertise(endpoint, _spec())


@pytest.mark.asyncio
async def test_beacon_prepared_update_and_delete_acknowledgement_recovery() -> None:
    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon(bucket) as (beacon, raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        bucket.fail_next("update", phase=LifecycleFaultPhase.AFTER)
        await lease.update(labels={"site": "post-commit"})
        assert beacon.candidates(FEATURE)[0].advertisement.labels == {
            "site": "post-commit"
        }

        bucket.fail_next("delete", phase=LifecycleFaultPhase.BEFORE)
        await lease.aclose()
        assert await raw.items()

        # A replacement owns a fresh lease identity but must remove the stale
        # same-endpoint advertisement before it publishes.
        replacement = beacon.advertise(endpoint, _spec())
        await replacement.__aenter__()
        entries = await raw.items()
        assert len(entries) == 1
        await replacement.aclose()


@pytest.mark.asyncio
async def test_beacon_conflicting_post_commit_create_is_explicit() -> None:
    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon(bucket) as (beacon, _raw):
        barrier = bucket.block_next(
            "create",
            phase=LifecycleFaultPhase.AFTER,
            error=KvUnavailable("create acknowledgement lost"),
        )
        lease = beacon.advertise(endpoint, _spec())
        errors: list[BaseException] = []

        async def enter() -> None:
            try:
                await lease.__aenter__()
            except BaseException as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(enter)
            await barrier.wait()
            entries = await bucket.items()
            assert len(entries) == 1
            entry = entries[0]
            conflicting = dict(entry.value)
            conflicting["sessionId"] = "conflicting-session"
            await MemoryJsonKvBucket.update(
                bucket,
                entry.key,
                conflicting,
                revision=entry.revision,
            )
            barrier.unblock()

        assert len(errors) == 1
        assert isinstance(errors[0], KvConflict)
        assert lease.closed


@pytest.mark.asyncio
async def test_beacon_close_owns_active_leases_and_validation_fails_closed() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30)
    beacon = Beacon(raw)
    async with anyio.create_task_group() as tasks:
        beacon.start(tasks)
        await beacon.wait_current()
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        assert lease._task_group is not None  # noqa: SLF001
        lease._task_group.cancel_scope.cancel()  # noqa: SLF001
        candidate = beacon.candidates(FEATURE)[0]

        async with beacon._view._broadcaster.lock:  # noqa: SLF001
            beacon._view._broadcaster.publish_locked((), current=False)  # noqa: SLF001
        assert await beacon.validate(candidate) == CandidateStatus.UNAVAILABLE
        with pytest.raises(KvUnavailable, match="not current"):
            beacon.candidates(FEATURE)
        async with beacon._view._broadcaster.lock:  # noqa: SLF001
            beacon._view._broadcaster.publish_locked((), current=True)  # noqa: SLF001

        entry = await raw.get(candidate.key)
        assert entry is not None
        invalid = await raw.update(
            candidate.key,
            {"schema": "invalid"},
            revision=entry.revision,
        )
        await beacon._view.wait_for_revision(invalid.revision)  # noqa: SLF001
        assert await beacon.validate(candidate) == CandidateStatus.SCHEMA_INVALID

        await beacon.aclose()
        assert lease.closed
        # The invalid exact write cannot be proven to belong to this lease, so
        # cleanup correctly retains it rather than deleting another owner.
        assert await raw.items()
        tasks.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_refresh_conflict_missing_and_delete_ack_recovery() -> None:
    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon(bucket) as (beacon, raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()

        # A pre-commit CAS conflict is retried from an exact same-owner read.
        bucket.fail_next(
            "update",
            phase=LifecycleFaultPhase.BEFORE,
            error=KvConflict("revision raced"),
        )
        await lease.update(labels={"site": "retried"})
        assert beacon.candidates(FEATURE)[0].advertisement.labels == {
            "site": "retried"
        }

        # Discovery may recreate a missing advertisement, but only with the
        # already-prepared lease identity.
        first = beacon.candidates(FEATURE)[0]
        exact = await raw.get(first.key)
        assert exact is not None
        marker = await raw.delete(first.key, revision=exact.revision)
        assert marker is not None
        await beacon._view.wait_for_revision(marker)  # noqa: SLF001
        await lease.update(labels={"site": "recreated"})
        recreated = beacon.candidates(FEATURE)[0]
        assert recreated.advertisement.advertisement_id == (
            first.advertisement.advertisement_id
        )

        bucket.fail_next("delete", phase=LifecycleFaultPhase.AFTER)
        await lease.aclose()
        assert not await raw.items()

        # Closing before entry resolves the prepared identity and is idempotent.
        never_entered = beacon.advertise(endpoint, _spec())
        await never_entered.aclose()
        await never_entered.aclose()


@pytest.mark.asyncio
async def test_beacon_refresh_rejects_invalid_or_changed_exact_owner() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    for replacement in (
        {"schema": "invalid"},
        None,
    ):
        async with _running_beacon() as (beacon, raw):
            lease = beacon.advertise(endpoint, _spec())
            await lease.__aenter__()
            candidate = beacon.candidates(FEATURE)[0]
            entry = await raw.get(candidate.key)
            assert entry is not None
            value = replacement
            if value is None:
                value = dict(entry.value)
                value["sessionId"] = "other-session"
            changed = await raw.update(
                entry.key,
                value,
                revision=entry.revision,
            )
            await beacon._view.wait_for_revision(changed.revision)  # noqa: SLF001
            with pytest.raises(KvConflict):
                await lease.update(labels={"site": "must-not-publish"})
            await lease.aclose()


@pytest.mark.asyncio
async def test_beacon_ttl_supports_sync_metadata_and_heartbeat_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30))
    # A tiny structural stand-in exercises substrates whose TTL accessor is
    # synchronous rather than awaitable.
    beacon._exact = type(  # type: ignore[assignment]  # noqa: SLF001
        "SyncTtl",
        (),
        {"ttl_seconds": lambda self: 30},
    )()
    assert await beacon._ttl_seconds() == 30  # noqa: SLF001

    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon(bucket) as (running, _raw):
        lease = running.advertise(endpoint, _spec())
        await lease.__aenter__()
        assert lease._task_group is not None  # noqa: SLF001
        lease._task_group.cancel_scope.cancel()  # noqa: SLF001
        lease._refresh_interval = 0  # noqa: SLF001
        lease._last_refresh_at = 0  # noqa: SLF001
        bucket.fail_next("update", phase=LifecycleFaultPhase.BEFORE)
        sleeps = 0

        async def controlled_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                lease._closed = True  # noqa: SLF001
            await anyio.lowlevel.checkpoint()

        monkeypatch.setattr(beacon_module.anyio, "sleep", controlled_sleep)
        await lease._heartbeat_loop()  # noqa: SLF001
        assert bucket.operation_counts["update"] == 2
        assert sleeps == 3


@pytest.mark.asyncio
async def test_beacon_watch_and_noop_refresh_cover_current_public_paths() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon() as (beacon, _raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        candidate = beacon.candidates(FEATURE)[0]

        beacon._started = False  # noqa: SLF001
        assert await beacon.validate(candidate) == CandidateStatus.CANDIDATE
        assert (
            await beacon.validate(candidate, current_sessions={})
            == CandidateStatus.CANDIDATE
        )
        async with beacon.watch() as snapshots:
            assert (await anext(snapshots)).candidates == (candidate,)
        assert await beacon.remove_stale_advertisements(
            _Endpoint(hardware_manager_address("other"), "other-session"),
            _spec(),
        ) == 0
        await beacon._observe_write(candidate.key, candidate.revision)  # noqa: SLF001
        beacon._started = True  # noqa: SLF001

        original = lease._handle  # noqa: SLF001
        await lease.update()
        assert lease._handle is original  # noqa: SLF001
        async with lease._lock:  # noqa: SLF001
            assert await lease._publish_locked(force_refresh=True) is original  # noqa: SLF001

        lease._refresh_interval = None  # noqa: SLF001
        lease._last_refresh_at = None  # noqa: SLF001
        assert await lease._next_heartbeat_delay() > 0  # noqa: SLF001
        await lease.aclose()


@pytest.mark.asyncio
async def test_beacon_watch_ignores_unrelated_changes_then_reports_relevant() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon() as (beacon, _raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        async with beacon.watch(FEATURE) as changes:
            await anext(changes)
            unrelated = beacon.advertise(
                _Endpoint(hardware_manager_address("unrelated"), "other-session"),
                replace(
                    _spec(),
                    feature_id="dev.deckr.test.beacon-unrelated.v1",
                ),
            )
            await unrelated.__aenter__()
            with anyio.move_on_after(0.02) as scope:
                await anext(changes)
            assert scope.cancel_called

        async with beacon.watch(FEATURE) as changes:
            await anext(changes)
            await lease.update(labels={"site": "relevant"})
            change = await anext(changes)
            assert change.changed_keys
        await unrelated.aclose()
        await lease.aclose()


@pytest.mark.asyncio
async def test_beacon_refresh_conflict_recovery_exact_classification() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")

    # The retry itself may commit and lose its acknowledgement; exact matching
    # state is the only recovery accepted.
    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    async with _running_beacon(bucket) as (beacon, _raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        bucket.fail_next(
            "update",
            phase=LifecycleFaultPhase.BEFORE,
            error=KvConflict("first CAS raced"),
        )
        bucket.fail_next("update", phase=LifecycleFaultPhase.AFTER)
        await lease.update(labels={"site": "retry-ack-lost"})
        assert beacon.candidates(FEATURE)[0].advertisement.labels == {
            "site": "retry-ack-lost"
        }
        await lease.aclose()

    for mutation in ("missing", "invalid", "owner"):
        bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
        async with _running_beacon(bucket) as (beacon, raw):
            lease = beacon.advertise(endpoint, _spec())
            await lease.__aenter__()
            candidate = beacon.candidates(FEATURE)[0]
            barrier = bucket.block_next(
                "update",
                phase=LifecycleFaultPhase.BEFORE,
                error=KvConflict("CAS raced"),
            )
            errors: list[BaseException] = []

            async def refresh(
                current_lease=lease,
                label=mutation,
                captured_errors=errors,
            ) -> None:
                try:
                    await current_lease.update(labels={"site": label})
                except BaseException as exc:
                    captured_errors.append(exc)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(refresh)
                await barrier.wait()
                exact = await raw.get(candidate.key)
                assert exact is not None
                if mutation == "missing":
                    revision = await MemoryJsonKvBucket.delete(
                        raw,
                        candidate.key,
                        revision=exact.revision,
                    )
                else:
                    value = (
                        {"schema": "invalid"}
                        if mutation == "invalid"
                        else {**exact.value, "sessionId": "other-session"}
                    )
                    changed = await MemoryJsonKvBucket.update(
                        raw,
                        candidate.key,
                        value,
                        revision=exact.revision,
                    )
                    revision = changed.revision
                assert revision is not None
                await beacon._view.wait_for_revision(revision)  # noqa: SLF001
                barrier.unblock()

            if mutation == "missing":
                assert not errors
                assert beacon.candidates(FEATURE)[0].advertisement.advertisement_id == (
                    candidate.advertisement.advertisement_id
                )
            else:
                assert len(errors) == 1
                assert isinstance(errors[0], KvConflict)
            await lease.aclose()


@pytest.mark.asyncio
async def test_beacon_exact_withdraw_and_stale_delete_failure_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(beacon_module.random, "uniform", lambda _low, _high: 0)
    endpoint = _Endpoint(MANAGER, "manager-session")
    bucket = LifecycleFaultHarness(bucket="beacon", ttl_seconds=30)
    async with _running_beacon(bucket) as (beacon, raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        assert lease._task_group is not None  # noqa: SLF001
        lease._task_group.cancel_scope.cancel()  # noqa: SLF001
        candidate = beacon.candidates(FEATURE)[0]

        bucket.fail_next("delete", phase=LifecycleFaultPhase.BEFORE)
        await beacon._delete_candidate(candidate)  # noqa: SLF001
        assert await raw.get(candidate.key) is None
        await beacon._delete_candidate(candidate)  # noqa: SLF001

        await lease.update(labels={"site": "recreated"})
        candidate = beacon.candidates(FEATURE)[0]
        bucket.fail_next("delete", phase=LifecycleFaultPhase.AFTER)
        await beacon._delete_candidate(candidate)  # noqa: SLF001
        assert await raw.get(candidate.key) is None

        record = _record(advertisementId="prepared-identity")
        key = beacon_advertisement_key(
            feature_id=FEATURE,
            advertisement_id="prepared-identity",
        )
        created = await raw.create(key, record, ttl=30)
        await beacon._view.wait_for_revision(created.revision)  # noqa: SLF001
        assert await beacon._withdraw_advertisement_identity(  # noqa: SLF001
            feature_id=FEATURE,
            advertisement_id="prepared-identity",
            endpoint=MANAGER,
            session_id="manager-session",
        )
        assert not await beacon._withdraw_advertisement_identity(  # noqa: SLF001
            feature_id=FEATURE,
            advertisement_id="prepared-identity",
            endpoint=MANAGER,
            session_id="manager-session",
        )
        await lease.aclose()


@pytest.mark.asyncio
async def test_beacon_exact_only_refresh_withdraw_and_owner_change_guards() -> None:
    endpoint = _Endpoint(MANAGER, "manager-session")
    async with _running_beacon() as (beacon, raw):
        lease = beacon.advertise(endpoint, _spec())
        await lease.__aenter__()
        candidate = beacon.candidates(FEATURE)[0]

        # Exact state remains authoritative if the materialized projection has
        # not yet installed the candidate.
        beacon._view._entries_by_key.pop(candidate.key)  # noqa: SLF001
        await lease.update(labels={"site": "exact-only"})
        candidate = beacon.candidates(FEATURE)[0]

        original_exact = beacon._exact  # noqa: SLF001
        exact_entry = await raw.get(candidate.key)
        assert exact_entry is not None
        beacon._exact = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
            get_exact=AsyncMock(return_value=exact_entry),
            delete=AsyncMock(return_value=None),
        )
        assert await beacon._withdraw_advertisement(lease._handle)  # type: ignore[arg-type]  # noqa: SLF001
        beacon._exact = original_exact  # noqa: SLF001

        changed_value = dict(exact_entry.value)
        changed_value["sessionId"] = "changed-owner"
        changed = await raw.update(
            candidate.key,
            changed_value,
            revision=exact_entry.revision,
        )
        await beacon._view.wait_for_revision(changed.revision)  # noqa: SLF001
        with pytest.raises(Exception, match="changed owner"):
            await beacon._delete_candidate(candidate)  # noqa: SLF001
        await lease.aclose()

    missing_ttl = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30))
    missing_ttl._exact = SimpleNamespace()  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(KvUnavailable, match="TTL-bound"):
        await missing_ttl._ttl_seconds()  # noqa: SLF001


@pytest.mark.asyncio
async def test_beacon_prepared_identity_withdrawal_classifies_every_ack() -> None:
    key = beacon_advertisement_key(
        feature_id=FEATURE,
        advertisement_id="prepared-identity",
    )
    valid = KvEntry(
        bucket="beacon",
        key=key,
        value=_record(advertisementId="prepared-identity").to_dict(),
        revision=1,
    )
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30))

    beacon._exact = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_exact=AsyncMock(
            return_value=KvEntry(
                bucket="beacon",
                key=key,
                value={"schema": "invalid"},
                revision=1,
            )
        )
    )
    with pytest.raises(KvConflict, match="invalid"):
        await beacon._withdraw_advertisement_identity(  # noqa: SLF001
            feature_id=FEATURE,
            advertisement_id="prepared-identity",
            endpoint=MANAGER,
            session_id="manager-session",
        )

    beacon._exact = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
        get_exact=AsyncMock(return_value=valid),
        delete=AsyncMock(return_value=None),
    )
    assert await beacon._withdraw_advertisement_identity(  # noqa: SLF001
        feature_id=FEATURE,
        advertisement_id="prepared-identity",
        endpoint=MANAGER,
        session_id="manager-session",
    )

    for recovered in (valid, None):
        beacon._exact = SimpleNamespace(  # type: ignore[assignment]  # noqa: SLF001
            get_exact=AsyncMock(side_effect=[valid, recovered]),
            delete=AsyncMock(side_effect=KvUnavailable("delete ack lost")),
        )
        if recovered is None:
            assert await beacon._withdraw_advertisement_identity(  # noqa: SLF001
                feature_id=FEATURE,
                advertisement_id="prepared-identity",
                endpoint=MANAGER,
                session_id="manager-session",
            )
        else:
            with pytest.raises(KvUnavailable):
                await beacon._withdraw_advertisement_identity(  # noqa: SLF001
                    feature_id=FEATURE,
                    advertisement_id="prepared-identity",
                    endpoint=MANAGER,
                    session_id="manager-session",
                )


def test_beacon_exact_entry_classification_and_nullable_serialization() -> None:
    dumped = _record(createdAt=None, updatedAt=None).model_dump(
        by_alias=True,
        exclude_none=False,
        mode="json",
    )
    assert dumped["createdAt"] is None
    invalid_key = KvEntry(
        bucket="beacon",
        key="not-a-beacon-key",
        value=_record().to_dict(),
        revision=1,
    )
    assert _candidate_from_entry(invalid_key) == (None, "invalid_key")
    mismatched = KvEntry(
        bucket="beacon",
        key=beacon_advertisement_key(
            feature_id=FEATURE,
            advertisement_id="other",
        ),
        value=_record().to_dict(),
        revision=1,
    )
    assert _candidate_from_entry(mismatched) == (None, "identity_mismatch")


@pytest.mark.asyncio
async def test_beacon_directory_closed_and_unstarted_guards() -> None:
    beacon = Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=30))
    directory = BeaconDirectory(
        beacon,
        FEATURE,
        lambda candidate: candidate.advertisement.advertisement_id,
    )
    directory.start_soon(None)
    assert directory._parse_candidate(None) == ()  # noqa: SLF001
    with pytest.raises(KvUnavailable, match="not current"):
        directory.records()
    await directory.aclose()
    directory.start_soon(lambda *_args: None)
    with pytest.raises(anyio.ClosedResourceError):
        await directory.wait_ready()
    with pytest.raises(anyio.ClosedResourceError):
        await directory.wait_for()


@pytest.mark.asyncio
async def test_beacon_directory_wait_closure_and_currentness_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Current:
        def __init__(self) -> None:
            self.current = True

        def is_current(self) -> bool:
            return self.current

        async def wait_current(self) -> None:
            self.current = True

    source = _Current()
    directory = BeaconDirectory(source, FEATURE, lambda _candidate: "record")
    directory._apply_snapshot((), current=True)  # noqa: SLF001
    assert await directory.wait_current() is None

    close_while_waiting = BeaconDirectory(
        source,
        FEATURE,
        lambda _candidate: "record",
    )
    errors: list[BaseException] = []

    async def wait_ready() -> None:
        try:
            await close_while_waiting.wait_ready()
        except BaseException as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(wait_ready)
        await anyio.sleep(0)
        await close_while_waiting.aclose()
    assert len(errors) == 1
    assert isinstance(errors[0], anyio.ClosedResourceError)

    closed_current = BeaconDirectory(source, FEATURE, lambda _candidate: "record")

    async def close_after_ready() -> None:
        closed_current._closed = True  # noqa: SLF001

    monkeypatch.setattr(closed_current, "wait_ready", close_after_ready)
    with pytest.raises(KvUnavailable, match="closed"):
        await closed_current.wait_current()

    closed_wait = BeaconDirectory(source, FEATURE, lambda _candidate: "record")

    async def close_wait_after_ready() -> None:
        closed_wait._closed = True  # noqa: SLF001

    monkeypatch.setattr(closed_wait, "wait_ready", close_wait_after_ready)
    with pytest.raises(anyio.ClosedResourceError):
        await closed_wait.wait_for()
