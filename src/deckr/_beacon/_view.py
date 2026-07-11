from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import anyio

from deckr.core.util.anyio import (
    CoalescedStateBroadcaster,
    CoalescedStateSubscription,
)
from deckr.substrates.nats_kv import (
    KvEntry,
    KvMaterializedChange,
    KvMaterializedSnapshot,
    KvUnavailable,
    NatsKvMaterializedBucket,
)

CandidateT = TypeVar("CandidateT")


@dataclass(frozen=True, slots=True)
class BeaconViewSnapshot(Generic[CandidateT]):
    version: int
    current: bool
    candidates: tuple[CandidateT, ...]


class BeaconView(Generic[CandidateT]):
    """Indexed semantic projection over one materialized advertisement source."""

    def __init__(
        self,
        source: NatsKvMaterializedBucket,
        *,
        parse_entry: Callable[[KvEntry], tuple[CandidateT | None, str]],
        candidate_key: Callable[[CandidateT], str],
        candidate_feature: Callable[[CandidateT], str],
        candidate_endpoint_key: Callable[[CandidateT], tuple[str, str, str]],
        sort_key: Callable[[CandidateT], Any],
    ) -> None:
        self.source = source
        self._parse_entry = parse_entry
        self._candidate_key = candidate_key
        self._candidate_feature = candidate_feature
        self._candidate_endpoint_key = candidate_endpoint_key
        self._sort_key = sort_key
        self._entries_by_key: dict[str, CandidateT] = {}
        self._invalid_by_key: dict[str, tuple[int, str]] = {}
        self._keys_by_feature: dict[str, set[str]] = {}
        self._keys_by_feature_endpoint: dict[tuple[str, str, str], set[str]] = {}
        self._observed_high_water_revision = 0
        self._revision_condition = anyio.Condition()
        self._ready = anyio.Event()
        self._broadcaster = CoalescedStateBroadcaster[str](current=False)
        self._closed = False

    @property
    def version(self) -> int:
        return self._broadcaster.version

    def is_current(self) -> bool:
        return self._ready.is_set() and self._broadcaster.current

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def wait_current(self) -> None:
        while not self.is_current():
            if self._closed:
                raise KvUnavailable("Beacon materialized view is closed")
            await self.source.wait_current()
            if self.is_current():
                return
            await anyio.sleep(0)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._broadcaster.aclose()
        await self.source.aclose()
        await self._notify_revision_waiters()

    async def wait_for_revision(self, revision: int) -> None:
        """Wait until semantic projection has consumed a broker revision."""

        while self._observed_high_water_revision < revision:
            if self._closed:
                raise KvUnavailable(
                    "Beacon materialized view closed before observing "
                    f"revision {revision}"
                )
            async with self._revision_condition:
                if self._observed_high_water_revision >= revision:
                    return
                await self._revision_condition.wait()

    async def run(self) -> None:
        try:
            async with self.source.subscribe() as changes:
                async for item in changes:
                    if isinstance(item, KvMaterializedSnapshot):
                        await self._install_snapshot(item)
                        continue
                    await self._consume_change(item)
        except anyio.ClosedResourceError:
            # The owner closes the broadcaster to wake blocked readers before
            # cancelling the surrounding task group. A snapshot already taken
            # from the source may race with that close; it has no state left to
            # publish and is safe to discard during shutdown.
            return

    async def _consume_change(self, change: KvMaterializedChange) -> None:
        if change.resnapshot_required:
            await self._install_snapshot(await self.source.snapshot())
            return
        observations = self._parse_keys(change.changed_keys)
        async with self._broadcaster.lock:
            changed = self._apply_observations_locked(observations)
            current_changed = self._broadcaster.current != change.current
            if changed or current_changed:
                self._broadcaster.publish_locked(changed, current=change.current)
            if change.current:
                self._ready.set()
            self._observed_high_water_revision = self.source.high_water_revision
        await self._notify_revision_waiters()

    async def _install_snapshot(self, snapshot: KvMaterializedSnapshot) -> None:
        parsed = self._parse_entries(snapshot.entries)
        entries_by_key = {
            self._candidate_key(candidate): candidate
            for candidate, _revision, _reason in parsed.values()
            if candidate is not None
        }
        invalid_by_key = {
            key: (revision, reason)
            for key, (candidate, revision, reason) in parsed.items()
            if candidate is None
        }
        keys_by_feature: dict[str, set[str]] = {}
        keys_by_endpoint: dict[tuple[str, str, str], set[str]] = {}
        for key, candidate in entries_by_key.items():
            keys_by_feature.setdefault(self._candidate_feature(candidate), set()).add(key)
            keys_by_endpoint.setdefault(
                self._candidate_endpoint_key(candidate), set()
            ).add(key)
        async with self._broadcaster.lock:
            changed = frozenset(
                key
                for key in self._entries_by_key.keys() | entries_by_key.keys()
                if self._entries_by_key.get(key) != entries_by_key.get(key)
            )
            current_changed = self._broadcaster.current != snapshot.current
            self._entries_by_key = entries_by_key
            self._invalid_by_key = invalid_by_key
            self._keys_by_feature = keys_by_feature
            self._keys_by_feature_endpoint = keys_by_endpoint
            if changed or current_changed or not self._ready.is_set():
                self._broadcaster.publish_locked(
                    changed,
                    current=snapshot.current,
                    resnapshot_required=len(changed) > 256,
                )
            if snapshot.current:
                self._ready.set()
            self._observed_high_water_revision = self.source.high_water_revision
        await self._notify_revision_waiters()

    async def _notify_revision_waiters(self) -> None:
        async with self._revision_condition:
            self._revision_condition.notify_all()

    def _parse_entries(
        self,
        entries: tuple[KvEntry, ...],
    ) -> dict[str, tuple[CandidateT | None, int, str]]:
        parsed: dict[str, tuple[CandidateT | None, int, str]] = {}
        for entry in entries:
            candidate, reason = self._parse_entry(entry)
            parsed[entry.key] = (candidate, entry.revision, reason)
        return parsed

    def _parse_keys(
        self,
        keys: frozenset[str],
    ) -> dict[str, tuple[CandidateT | None, int | None, str]]:
        parsed: dict[str, tuple[CandidateT | None, int | None, str]] = {}
        for key in keys:
            entry = self.source.get_cached(key)
            if entry is None:
                parsed[key] = (None, self.source.revision_cached(key), "missing")
                continue
            candidate, reason = self._parse_entry(entry)
            parsed[key] = (candidate, entry.revision, reason)
        return parsed

    def _apply_observations_locked(
        self,
        observations: Mapping[
            str,
            tuple[CandidateT | None, int | None, str],
        ],
    ) -> frozenset[str]:
        changed: set[str] = set()
        for key, (candidate, revision, reason) in observations.items():
            previous = self._entries_by_key.get(key)
            if previous is not None:
                self._remove_candidate_locked(previous)
            if candidate is None:
                if revision is None or reason == "missing":
                    self._invalid_by_key.pop(key, None)
                else:
                    self._invalid_by_key[key] = (revision, reason)
            else:
                self._invalid_by_key.pop(key, None)
                self._entries_by_key[key] = candidate
                self._keys_by_feature.setdefault(
                    self._candidate_feature(candidate), set()
                ).add(key)
                self._keys_by_feature_endpoint.setdefault(
                    self._candidate_endpoint_key(candidate), set()
                ).add(key)
            if previous != candidate:
                changed.add(key)
        return frozenset(changed)

    def _remove_candidate_locked(self, candidate: CandidateT) -> None:
        key = self._candidate_key(candidate)
        self._entries_by_key.pop(key, None)
        feature = self._candidate_feature(candidate)
        feature_keys = self._keys_by_feature.get(feature)
        if feature_keys is not None:
            feature_keys.discard(key)
            if not feature_keys:
                self._keys_by_feature.pop(feature, None)
        endpoint_key = self._candidate_endpoint_key(candidate)
        endpoint_keys = self._keys_by_feature_endpoint.get(endpoint_key)
        if endpoint_keys is not None:
            endpoint_keys.discard(key)
            if not endpoint_keys:
                self._keys_by_feature_endpoint.pop(endpoint_key, None)

    def get(self, key: str) -> CandidateT | None:
        return self._entries_by_key.get(key)

    def is_invalid(self, key: str) -> bool:
        return key in self._invalid_by_key

    def candidates(self, feature_id: str | None = None) -> tuple[CandidateT, ...]:
        if feature_id is None:
            candidates = tuple(self._entries_by_key.values())
        else:
            candidates = tuple(
                candidate
                for key in self._keys_by_feature.get(feature_id, ())
                if (candidate := self._entries_by_key.get(key)) is not None
            )
        return tuple(sorted(candidates, key=self._sort_key))

    def candidates_for_endpoint(
        self,
        endpoint_key: tuple[str, str, str],
    ) -> tuple[CandidateT, ...]:
        return tuple(
            candidate
            for key in self._keys_by_feature_endpoint.get(endpoint_key, ())
            if (candidate := self._entries_by_key.get(key)) is not None
        )

    def _snapshot_locked(
        self,
        feature_id: str | None,
        version: int,
        current: bool,
    ) -> BeaconViewSnapshot[CandidateT]:
        return BeaconViewSnapshot(
            version=version,
            current=current,
            candidates=self.candidates(feature_id),
        )

    async def snapshot(
        self,
        feature_id: str | None = None,
    ) -> BeaconViewSnapshot[CandidateT]:
        return await self._broadcaster.capture(
            lambda version, current: self._snapshot_locked(
                feature_id,
                version,
                current,
            )
        )

    @asynccontextmanager
    async def subscribe(
        self,
        feature_id: str | None = None,
    ) -> AsyncIterator[
        CoalescedStateSubscription[str, BeaconViewSnapshot[CandidateT]]
    ]:
        async with self._broadcaster.subscribe(
            lambda version, current: self._snapshot_locked(
                feature_id,
                version,
                current,
            )
        ) as subscription:
            yield subscription
