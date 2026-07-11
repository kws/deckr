from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio

from deckr._concord._keys import (
    concord_contract_key,
    concord_participant_token_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr._concord._models import (
    ConcordContractState,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ParticipantHandle,
    ParticipantTokenRecord,
    contract_handle,
    participant_handle,
)
from deckr._concord._ports import ConcordMaterializedSourcePort
from deckr._concord._validation import (
    ConcordObservationState,
    ConcordSessionAssertions,
    ContractObservation,
    contract_observation_from_entry,
    evaluate_contract_validity,
    token_observation_from_entry,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress
from deckr.core.util.anyio import (
    CoalescedStateBroadcaster,
    CoalescedStateSubscription,
)
from deckr.substrates.nats_kv import (
    KvEntry,
    KvMaterializedChange,
    KvMaterializedSnapshot,
    KvUnavailable,
)


@dataclass(frozen=True, slots=True)
class ConcordViewSnapshot:
    version: int
    current: bool
    contracts: tuple[ConcordContractState, ...]


class ConcordView:
    """Indexed semantic projection over the contract and token KV sources."""

    def __init__(
        self,
        contract_source: ConcordMaterializedSourcePort,
        token_source: ConcordMaterializedSourcePort,
    ) -> None:
        self.contract_source = contract_source
        self.token_source = token_source
        self._contract_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_contracts_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._contract_records_by_key: dict[str, ContractRecord] = {}
        self._contract_handles_by_key: dict[str, ContractHandle] = {}
        self._contract_keys_by_pointer: dict[tuple[str, int], str] = {}
        self._contract_keys_by_contract_id: dict[str, set[str]] = {}
        self._contract_keys_by_profile: dict[str | None, set[str]] = {}
        self._contract_keys_by_participant: dict[str, set[str]] = {}
        self._contract_keys_by_state: dict[ContractState, set[str]] = {}
        self._token_entries_by_key: dict[str, KvEntry] = {}
        self._invalid_tokens_by_key: dict[str, tuple[KvEntry, str]] = {}
        self._token_records_by_key: dict[str, ParticipantTokenRecord] = {}
        self._tokens_by_key: dict[str, ParticipantHandle] = {}
        self._token_keys_by_contract: dict[tuple[str, int], set[str]] = {}
        self._token_key_by_contract_participant: dict[tuple[str, int, str], str] = {}
        self._contract_observed_revision: dict[str, int] = {}
        self._token_observed_revision: dict[str, int] = {}
        self._contract_revision_condition = anyio.Condition()
        self._token_revision_condition = anyio.Condition()
        self._contract_current = False
        self._token_current = False
        self._ready = anyio.Event()
        self._broadcaster = CoalescedStateBroadcaster[ContractPointer](current=False)
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
                raise KvUnavailable("Concord materialized view is closed")
            await self.contract_source.wait_current()
            await self.token_source.wait_current()
            if self.is_current():
                return
            await anyio.sleep(0)

    async def run(self) -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(self._contract_loop)
            task_group.start_soon(self._token_loop)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._broadcaster.aclose()
        await self._notify_contract_revisions()
        await self._notify_token_revisions()

    async def _contract_loop(self) -> None:
        try:
            async with self.contract_source.subscribe() as changes:
                async for item in changes:
                    if isinstance(item, KvMaterializedSnapshot):
                        await self._install_contract_snapshot(item)
                    else:
                        await self._consume_contract_change(item)
        except anyio.ClosedResourceError:
            return

    async def _token_loop(self) -> None:
        try:
            async with self.token_source.subscribe() as changes:
                async for item in changes:
                    if isinstance(item, KvMaterializedSnapshot):
                        await self._install_token_snapshot(item)
                    else:
                        await self._consume_token_change(item)
        except anyio.ClosedResourceError:
            return

    async def _consume_contract_change(self, change: KvMaterializedChange) -> None:
        if change.resnapshot_required:
            await self._install_contract_snapshot(await self.contract_source.snapshot())
            return
        parsed = {
            key: self._parse_contract_entry(self.contract_source.get_cached(key))
            for key in change.changed_keys
        }
        async with self._broadcaster.lock:
            changed: set[ContractPointer] = set()
            for key, observation in parsed.items():
                pointer = self._contract_pointer_for_key_locked(key)
                self._remove_contract_key_locked(key)
                if observation is not None:
                    self._index_contract_observation_locked(observation)
                    pointer = pointer or _pointer_from_contract_key(key)
                if pointer is not None:
                    changed.add(pointer)
                revision = self.contract_source.revision_cached(key)
                if revision is not None:
                    self._contract_observed_revision[key] = revision
            self._contract_current = change.current
            self._publish_locked(changed)
        await self._notify_contract_revisions()

    async def _consume_token_change(self, change: KvMaterializedChange) -> None:
        if change.resnapshot_required:
            await self._install_token_snapshot(await self.token_source.snapshot())
            return
        parsed = {
            key: self._parse_token_entry(self.token_source.get_cached(key))
            for key in change.changed_keys
        }
        async with self._broadcaster.lock:
            changed: set[ContractPointer] = set()
            for key, observation in parsed.items():
                pointer = self._token_pointer_for_key_locked(key)
                self._remove_token_key_locked(key)
                if observation is not None:
                    self._index_token_observation_locked(observation)
                    pointer = pointer or _pointer_from_token_key(key)
                if pointer is not None:
                    changed.add(pointer)
                revision = self.token_source.revision_cached(key)
                if revision is not None:
                    self._token_observed_revision[key] = revision
            self._token_current = change.current
            self._publish_locked(changed)
        await self._notify_token_revisions()

    async def _install_contract_snapshot(self, snapshot: KvMaterializedSnapshot) -> None:
        observations = tuple(
            observation
            for entry in snapshot.entries
            if (observation := self._parse_contract_entry(entry)) is not None
        )
        async with self._broadcaster.lock:
            previous_keys = set(self._contract_entries_by_key)
            previous = {
                pointer
                for key in previous_keys
                if (pointer := _pointer_from_contract_key(key)) is not None
            }
            self._clear_contract_indexes_locked()
            for observation in observations:
                self._index_contract_observation_locked(observation)
            self._contract_observed_revision = {
                entry.key: entry.revision for entry in snapshot.entries
            }
            for key in previous_keys - self._contract_observed_revision.keys():
                revision = self.contract_source.revision_cached(key)
                if revision is not None:
                    self._contract_observed_revision[key] = revision
            current = {
                pointer
                for key in self._contract_entries_by_key
                if (pointer := _pointer_from_contract_key(key)) is not None
            }
            self._contract_current = snapshot.current
            self._publish_locked(previous | current, resnapshot=len(previous | current) > 256)
        await self._notify_contract_revisions()

    async def _install_token_snapshot(self, snapshot: KvMaterializedSnapshot) -> None:
        observations = tuple(
            observation
            for entry in snapshot.entries
            if (observation := self._parse_token_entry(entry)) is not None
        )
        async with self._broadcaster.lock:
            previous_keys = set(self._token_entries_by_key)
            previous = {
                pointer
                for key in previous_keys
                if (pointer := _pointer_from_token_key(key)) is not None
            }
            self._clear_token_indexes_locked()
            for observation in observations:
                self._index_token_observation_locked(observation)
            self._token_observed_revision = {
                entry.key: entry.revision for entry in snapshot.entries
            }
            for key in previous_keys - self._token_observed_revision.keys():
                revision = self.token_source.revision_cached(key)
                if revision is not None:
                    self._token_observed_revision[key] = revision
            current = {
                pointer
                for key in self._token_entries_by_key
                if (pointer := _pointer_from_token_key(key)) is not None
            }
            self._token_current = snapshot.current
            self._publish_locked(previous | current, resnapshot=len(previous | current) > 256)
        await self._notify_token_revisions()

    async def wait_contract_revision(self, key: str, revision: int) -> None:
        while self._contract_observed_revision.get(key, 0) < revision:
            if self._closed:
                raise KvUnavailable(
                    "Concord materialized view closed before observing "
                    f"contract revision {revision} for {key!r}"
                )
            async with self._contract_revision_condition:
                if self._contract_observed_revision.get(key, 0) >= revision:
                    return
                await self._contract_revision_condition.wait()

    async def wait_token_revision(self, key: str, revision: int) -> None:
        while self._token_observed_revision.get(key, 0) < revision:
            if self._closed:
                raise KvUnavailable(
                    "Concord materialized view closed before observing "
                    f"token revision {revision} for {key!r}"
                )
            async with self._token_revision_condition:
                if self._token_observed_revision.get(key, 0) >= revision:
                    return
                await self._token_revision_condition.wait()

    async def _notify_contract_revisions(self) -> None:
        async with self._contract_revision_condition:
            self._contract_revision_condition.notify_all()

    async def _notify_token_revisions(self) -> None:
        async with self._token_revision_condition:
            self._token_revision_condition.notify_all()

    def _publish_locked(
        self,
        changed: set[ContractPointer],
        *,
        resnapshot: bool = False,
    ) -> None:
        current = self._contract_current and self._token_current
        current_changed = self._broadcaster.current != current
        if changed or current_changed or (current and not self._ready.is_set()):
            self._broadcaster.publish_locked(
                changed,
                current=current,
                resnapshot_required=resnapshot,
            )
        if current:
            self._ready.set()

    @staticmethod
    def _parse_contract_entry(
        entry: KvEntry | None,
    ) -> tuple[KvEntry, ContractRecord | None, str] | None:
        if entry is None:
            return None
        parsed = parse_concord_contract_key(entry.key)
        if parsed is None:
            return entry, None, "contract key is not a Concord contract key"
        try:
            record = ContractRecord.model_validate(entry.value)
        except (TypeError, ValueError) as exc:
            return entry, None, str(exc)
        if (record.contract_id, record.generation) != parsed:
            return entry, None, "contract key and record identity differ"
        return entry, record, ""

    @staticmethod
    def _parse_token_entry(
        entry: KvEntry | None,
    ) -> tuple[KvEntry, ParticipantTokenRecord | None, str] | None:
        if entry is None:
            return None
        parsed = parse_concord_participant_token_key(entry.key)
        if parsed is None:
            return entry, None, "token key is not a Concord participant token key"
        try:
            record = ParticipantTokenRecord.model_validate(entry.value)
        except (TypeError, ValueError) as exc:
            return entry, None, str(exc)
        if (
            record.contract_id != parsed[0]
            or record.generation != parsed[1]
            or record.participant != parsed[2]
        ):
            return entry, None, "token key and record identity differ"
        return entry, record, ""

    def _index_contract_observation_locked(
        self,
        observation: tuple[KvEntry, ContractRecord | None, str],
    ) -> None:
        entry, record, diagnostic = observation
        self._contract_entries_by_key[entry.key] = entry
        if record is None:
            self._invalid_contracts_by_key[entry.key] = (entry, diagnostic)
            return
        handle = contract_handle(entry.key, record, entry.revision)
        self._contract_records_by_key[entry.key] = record
        self._contract_handles_by_key[entry.key] = handle
        self._contract_keys_by_pointer[(record.contract_id, record.generation)] = entry.key
        self._contract_keys_by_contract_id.setdefault(record.contract_id, set()).add(
            entry.key
        )
        self._contract_keys_by_profile.setdefault(record.profile, set()).add(entry.key)
        self._contract_keys_by_state.setdefault(record.state, set()).add(entry.key)
        for participant in record.participants:
            self._contract_keys_by_participant.setdefault(str(participant), set()).add(
                entry.key
            )

    def _index_token_observation_locked(
        self,
        observation: tuple[KvEntry, ParticipantTokenRecord | None, str],
    ) -> None:
        entry, record, diagnostic = observation
        self._token_entries_by_key[entry.key] = entry
        if record is None:
            self._invalid_tokens_by_key[entry.key] = (entry, diagnostic)
            return
        token = participant_handle(entry.key, record, entry.revision)
        self._token_records_by_key[entry.key] = record
        self._tokens_by_key[entry.key] = token
        pointer = (record.contract_id, record.generation)
        self._token_keys_by_contract.setdefault(pointer, set()).add(entry.key)
        self._token_key_by_contract_participant[
            (record.contract_id, record.generation, str(record.participant))
        ] = entry.key

    def _remove_contract_key_locked(self, key: str) -> None:
        record = self._contract_records_by_key.pop(key, None)
        self._contract_entries_by_key.pop(key, None)
        self._invalid_contracts_by_key.pop(key, None)
        self._contract_handles_by_key.pop(key, None)
        if record is None:
            return
        self._contract_keys_by_pointer.pop((record.contract_id, record.generation), None)
        _discard_index_key(self._contract_keys_by_contract_id, record.contract_id, key)
        _discard_index_key(self._contract_keys_by_profile, record.profile, key)
        _discard_index_key(self._contract_keys_by_state, record.state, key)
        for participant in record.participants:
            _discard_index_key(self._contract_keys_by_participant, str(participant), key)

    def _remove_token_key_locked(self, key: str) -> None:
        record = self._token_records_by_key.pop(key, None)
        self._token_entries_by_key.pop(key, None)
        self._invalid_tokens_by_key.pop(key, None)
        self._tokens_by_key.pop(key, None)
        if record is None:
            return
        _discard_index_key(
            self._token_keys_by_contract,
            (record.contract_id, record.generation),
            key,
        )
        self._token_key_by_contract_participant.pop(
            (record.contract_id, record.generation, str(record.participant)),
            None,
        )

    def _clear_contract_indexes_locked(self) -> None:
        self._contract_entries_by_key.clear()
        self._invalid_contracts_by_key.clear()
        self._contract_records_by_key.clear()
        self._contract_handles_by_key.clear()
        self._contract_keys_by_pointer.clear()
        self._contract_keys_by_contract_id.clear()
        self._contract_keys_by_profile.clear()
        self._contract_keys_by_participant.clear()
        self._contract_keys_by_state.clear()

    def _clear_token_indexes_locked(self) -> None:
        self._token_entries_by_key.clear()
        self._invalid_tokens_by_key.clear()
        self._token_records_by_key.clear()
        self._tokens_by_key.clear()
        self._token_keys_by_contract.clear()
        self._token_key_by_contract_participant.clear()

    def _contract_pointer_for_key_locked(self, key: str) -> ContractPointer | None:
        handle = self._contract_handles_by_key.get(key)
        return handle.pointer if handle is not None else _pointer_from_contract_key(key)

    def _token_pointer_for_key_locked(self, key: str) -> ContractPointer | None:
        token = self._tokens_by_key.get(key)
        if token is not None:
            return ContractPointer(contractId=token.contract_id, generation=token.generation)
        return _pointer_from_token_key(key)

    def get_contract(self, pointer: ContractPointer) -> ContractHandle | None:
        key = self._contract_keys_by_pointer.get(
            (pointer.contract_id, pointer.generation)
        )
        return self._contract_handles_by_key.get(key) if key is not None else None

    def record(self, contract: ContractHandle) -> ContractRecord | None:
        record = self._contract_records_by_key.get(contract.key)
        if record is None:
            return None
        if (
            record.contract_id != contract.contract_id
            or record.generation != contract.generation
        ):
            return None
        return record

    def contracts(
        self,
        profile: str | None = None,
        *,
        contract_id: str | None = None,
        participant: EndpointAddress | None = None,
        state: ContractState | None = None,
    ) -> tuple[ContractHandle, ...]:
        keys = self._selected_keys_locked(
            profile=profile,
            contract_id=contract_id,
            participant=participant,
            state=state,
        )
        return tuple(
            self._contract_handles_by_key[key]
            for key in sorted(keys)
            if key in self._contract_handles_by_key
        )

    def _selected_keys_locked(
        self,
        *,
        profile: str | None,
        contract_id: str | None,
        participant: EndpointAddress | None,
        state: ContractState | None,
    ) -> set[str]:
        candidates: list[set[str]] = []
        if profile is not None:
            candidates.append(set(self._contract_keys_by_profile.get(profile, ())))
        if contract_id is not None:
            candidates.append(
                set(self._contract_keys_by_contract_id.get(contract_id, ()))
            )
        if participant is not None:
            candidates.append(
                set(self._contract_keys_by_participant.get(str(participant), ()))
            )
        if state is not None:
            candidates.append(set(self._contract_keys_by_state.get(state, ())))
        if not candidates:
            return set(self._contract_handles_by_key)
        keys = min(candidates, key=len).copy()
        for candidate in candidates:
            keys.intersection_update(candidate)
        return keys

    def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | ConcordSessionAssertions | None = None,
    ) -> ContractValidity:
        pointer = contract.pointer
        key = concord_contract_key(
            contract_id=pointer.contract_id,
            generation=pointer.generation,
        )
        observation = contract_observation_from_entry(
            key,
            self._contract_entries_by_key.get(key),
        )
        participants = (
            observation.record.participants
            if observation.record is not None
            else contract.participants
        )
        tokens = tuple(
            token_observation_from_entry(
                token_key,
                participant,
                self._token_entries_by_key.get(token_key),
            )
            for participant in sorted(participants, key=str)
            for token_key in (
                concord_participant_token_key(
                    contract_id=pointer.contract_id,
                    generation=pointer.generation,
                    participant=participant,
                ),
            )
        )
        if not self._broadcaster.current:
            observation = ContractObservation(
                observation.key,
                observation.revision,
                ConcordObservationState.UNAVAILABLE,
                record=observation.record,
                diagnostic="Concord materialized contract or token source is stale",
            )
        return evaluate_contract_validity(
            expected_key=contract.key,
            expected_pointer=pointer,
            contract=observation,
            tokens=tokens,
            session_assertions=(
                current_sessions
                if isinstance(current_sessions, ConcordSessionAssertions)
                else ConcordSessionAssertions.from_mapping(current_sessions)
            ),
        )

    def state_for(self, contract: ContractHandle) -> ConcordContractState | None:
        record = self.record(contract)
        if record is None:
            return None
        return ConcordContractState(
            contract=contract,
            record=record,
            validity=self.validate(contract),
        )

    def _snapshot_locked(
        self,
        version: int,
        current: bool,
        *,
        profile: str | None,
        participant: EndpointAddress | None,
        state: ContractState | None,
        contract_id: str | None,
    ) -> ConcordViewSnapshot:
        contracts = self.contracts(
            profile,
            contract_id=contract_id,
            participant=participant,
            state=state,
        )
        return ConcordViewSnapshot(
            version=version,
            current=current,
            contracts=tuple(
                contract_state
                for contract in contracts
                if (contract_state := self.state_for(contract)) is not None
            ),
        )

    async def snapshot(
        self,
        profile: str | None = None,
        *,
        participant: EndpointAddress | None = None,
        state: ContractState | None = None,
        contract_id: str | None = None,
    ) -> ConcordViewSnapshot:
        return await self._broadcaster.capture(
            lambda version, current: self._snapshot_locked(
                version,
                current,
                profile=profile,
                participant=participant,
                state=state,
                contract_id=contract_id,
            )
        )

    @asynccontextmanager
    async def subscribe(
        self,
        profile: str | None = None,
        *,
        participant: EndpointAddress | None = None,
        state: ContractState | None = None,
        contract_id: str | None = None,
    ) -> AsyncIterator[
        CoalescedStateSubscription[ContractPointer, ConcordViewSnapshot]
    ]:
        async with self._broadcaster.subscribe(
            lambda version, current: self._snapshot_locked(
                version,
                current,
                profile=profile,
                participant=participant,
                state=state,
                contract_id=contract_id,
            )
        ) as subscription:
            yield subscription


def _pointer_from_contract_key(key: str) -> ContractPointer | None:
    parsed = parse_concord_contract_key(key)
    if parsed is None:
        return None
    return ContractPointer(contractId=parsed[0], generation=parsed[1])


def _pointer_from_token_key(key: str) -> ContractPointer | None:
    parsed = parse_concord_participant_token_key(key)
    if parsed is None:
        return None
    return ContractPointer(contractId=parsed[0], generation=parsed[1])


def _discard_index_key(index: dict[object, set[str]], value: object, key: str) -> None:
    keys = index.get(value)
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        index.pop(value, None)
