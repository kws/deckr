# Agent instructions: Python direct JetStream Beacon/Concord/service-view refactor

## 1. Goal

Refactor the Python runtime so that protocol authority is implemented directly
on NATS/JetStream, with no generic `StateStore` abstraction layer.

The desired Python core model is:

```text
Beacon       = materialized JetStream KV view of the Beacon advertisement bucket
               + core-owned advertisement heartbeats
Concord      = materialized JetStream KV views of contract/token/maintenance
               buckets + core-owned participant token heartbeats
Service views = direct JetStream/KV protected view primitives, authorized by
               Concord service-use contracts
Lanes        = Core NATS message queues only
StateStore   = removed
```

The branch already documents the intended protocol boundary: Core NATS carries
lane messages, JetStream KV backs explicit protocol stores, Beacon provides weak
discovery, and Concord provides live agreements. The concrete Beacon/Concord
buckets are already specified as:

```text
deckr_beacon_advertisement_v1
deckr_concord_contract_v1
deckr_concord_token_v1
deckr_concord_maintenance_v1
```

Keep all language-neutral wire semantics: key shapes, schemas, CAS behavior,
TTL policy, canonical terms hashing, and Beacon/Concord separation. Do **not**
turn Beacon into live authority; Concord remains the authority for established
agreements.

### Document authority

For this refactor, this document supersedes all older README and documentation
guidance when they conflict. Treat stale references in other docs as historical
context until they are updated. Implement this plan over older descriptions of
`StateStore`, Beacon, Concord, service views, or service runtime helpers.

### Alpha breaking-change policy

This package is alpha. This refactor is intentionally breaking. Do not add or
preserve shims, aliases, deprecated wrappers, backwards-compatible constructors,
or compatibility functions for the removed APIs. Remove the old API surface
cleanly and update tests/callers to the new direct JetStream/KV model.

---

## 2. Remove `StateStore` completely

### Problem to fix

`StateStore` currently sits between protocol code and NATS. Its API explicitly
says prefix reads are not authoritative and watches are not authoritative event
logs. That is the wrong surface for Beacon, Concord, and protected service
views, because the materialized JetStream KV bucket state is the protocol
authority.

The current NATS `StateStore.items()` path is also expensive: it opens a KV
watch, consumes an initial snapshot, filters markers, then tears down the
ephemeral consumer. That makes every `find()` or reconciliation scan pay for a
new KV observation. The watch path also coalesces changes by key through
`_LatestStateWatchPump`, which is useful for generic current-state wakeups but
not precise enough as the primary runtime event source for Beacon, Concord, or
service views.

### Instruction

Remove `StateStore` as an abstraction from the Python core runtime. Do not keep
it as app-owned generic state, test infrastructure, or a transitional protocol
boundary.

Remove it from:

```text
Deckr.state()
LaneSubstrate.state()
NatsSubstrate.state()
SupervisedNatsSubstrate.state()
BeaconDiscovery
BeaconService / Beacon
ConcordCoordinator
ConcordService / Concord
ConcordParticipantManager / ConcordParticipant
ConcordReaperService
ServiceViewReader
ServiceViewWriter
tests and fakes that exist only to support StateStore
```

Remove or obsolete these public abstractions:

```text
StateStore
StateStorePolicy
StateEntry
StateChange
PrefixObservation
observe_prefix_current(...)
NatsStateStore
MemoryStateStore
```

If a future feature needs generic application state, it should define a concrete
JetStream-backed protocol for that feature. Do not reintroduce a shared generic
state API in this package.

After the refactor, Beacon, Concord, and service views should open their own
JetStream KV buckets directly and own their own materialized views.

---

## 3. Introduce internal JetStream KV helpers, not a new public state layer

Create Python-only internal implementation helpers, for example:

```text
src/deckr/substrates/nats_kv.py
```

These helpers are for NATS mechanics only. They must not become a public generic
state abstraction and must not be exposed as a replacement `StateStore`.

The module may expose a thin raw JSON KV helper for exact maintenance operations,
for example `NatsJsonKvBucket`. That helper may support `get`, prefix
`items(...)`, `create`, revision-guarded `update`, revision-guarded `delete`,
and `watch`. It is still an internal NATS adapter, not a public generic state
API.

Define a small internal entry/change shape for materialized buckets:

```python
@dataclass(frozen=True, slots=True)
class KvBucketPolicy:
    bucket: str
    ttl_seconds: float | None
    allow_write_ttl: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class KvEntry:
    bucket: str
    key: str
    value: Mapping[str, Any]
    revision: int


@dataclass(frozen=True, slots=True)
class KvChange:
    bucket: str
    key: str
    revision: int
    operation: Literal["put", "delete", "expire"]
    entry: KvEntry | None
    marker_reason: str | None = None
```

Define an internal materialized bucket primitive:

```python
class NatsKvMaterializedBucket:
    def __init__(
        self,
        *,
        js: Any,
        bucket: str,
        policy: KvBucketPolicy,
        key_prefix: str = "",
        buffer_size: int = 100,
    ) -> None: ...

    async def start(self, task_group: anyio.abc.TaskGroup) -> None: ...
    async def wait_ready(self) -> None: ...

    def get_cached(self, key: str) -> KvEntry | None: ...
    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]: ...
    def revision_cached(self, key: str) -> int | None: ...

    async def get_exact(self, key: str) -> KvEntry | None: ...

    async def create(self, key: str, value: Mapping[str, Any]) -> KvEntry: ...
    async def update(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        revision: int,
    ) -> KvEntry: ...
    async def delete(self, key: str, *, revision: int | None = None) -> None: ...

    def subscribe(self) -> AsyncContextManager[ObjectReceiveStream[KvChange]]: ...
```

### Bucket behavior

The primitive must:

1. Open or create the JetStream KV bucket with explicit policy rules.
2. Enforce `max_msgs_per_subject=1` and the configured bucket TTL, matching the
   current NATS stream configuration logic.
3. Hydrate a local in-memory map once at startup.
4. Subscribe to raw `$KV.<bucket>.<prefix>` changes and update the local map
   continuously.
5. Emit `KvChange` events to subscribers.
6. Avoid repeat full scans in normal hot-path operation.
7. Trigger a full resync only on startup, explicit recovery, or subscription
   failure.

This full-scan restriction is for long-lived runtime views. The Concord reaper
is a low-frequency maintenance service and is explicitly allowed to scan raw KV
keys as described below.

### Hydration algorithm

Use a no-gap startup sequence:

```text
1. Open raw JetStream subscription first and buffer changes.
2. Load current snapshot from KV keys/get, or one KV watch initial snapshot.
3. Apply buffered changes in revision order, ignoring stale changes whose
   revision <= cached revision.
4. Mark bucket ready.
5. Continue applying live changes.
```

When applying a change:

```text
put:
  if revision > cached_revision:
      cache[key] = entry

delete/expire:
  if revision > cached_revision:
      cache.pop(key, None)
      tombstone_revision[key] = revision
```

Keep `revision_by_key` or equivalent tombstone state so a late snapshot `put`
cannot resurrect a key after a delete/expiry marker.

Reuse the current NATS marker parsing behavior:

```text
KV-Operation: DEL/PURGE -> delete
Nats-Marker-Reason: MaxAge -> expire
```

---

## 4. Beacon: direct materialized advertisement runtime

### Current code to remove or collapse

`BeaconDiscovery` currently accepts a `StateStore`, advertises through
`state.create`, refreshes through exact read plus revision update, finds through
`state.items(prefix)`, and watches through `state.watch(prefix)`.

Remove `BeaconDiscovery` as a public construction layer. Do not preserve the old
constructor shape. The primary runtime class should be `Beacon`.

### New core API

Keep the API small:

```python
class Beacon:
    async def wait_ready(self) -> None: ...

    async def advertise(
        self,
        spec: BeaconAdvertisementSpec,
    ) -> BeaconAdvertisementLease: ...

    def candidates(
        self,
        feature_id: str,
        *,
        selector: AdvertisementFilter | None = None,
    ) -> tuple[Candidate, ...]: ...

    def get(
        self,
        *,
        feature_id: str,
        advertisement_id: str,
    ) -> Candidate | None: ...

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus: ...

    def watch(
        self,
        feature_id: str | None = None,
    ) -> AbstractAsyncContextManager[ObjectReceiveStream[BeaconFeatureEvent]]: ...
```

Use `Beacon`, not `BeaconService`, as the primary class. Do not keep a
`BeaconService` alias or compatibility constructor.

### Internal indexes

Maintain:

```python
_entries_by_key: dict[str, Candidate]
_keys_by_feature: dict[str, set[str]]
```

On `put`:

1. Parse key with `parse_beacon_advertisement_key`.
2. Validate `AdvertisementRecord`.
3. Confirm key identity equals record identity.
4. Replace any prior candidate.
5. Update feature index.
6. Emit `ADVERTISED` if key was previously absent.
7. Emit `UPDATED` if key existed.
8. Emit `INVALID` if key/record/schema validation fails.

On `delete` or `expire`:

1. Remove from `_entries_by_key`.
2. Remove from `_keys_by_feature`.
3. Emit `WITHDRAWN` for explicit delete.
4. Emit `EXPIRED` for MaxAge expiry.

The old per-watch `known` dictionary in `BeaconService.watch_feature()` should
go away. The Beacon runtime should maintain the authoritative local indexed view
for all watchers.

### Advertisement lease and heartbeats

Keep core-owned heartbeats. Applications must not manage refresh timing or
`refreshSeq`.

Expose:

```python
class BeaconAdvertisementLease:
    @property
    def handle(self) -> AdvertisementHandle: ...

    async def update(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        labels: Mapping[str, str] | None = None,
        hints: Mapping[str, Any] | None = None,
        operations: Sequence[str] | None = None,
    ) -> AdvertisementHandle: ...

    async def withdraw(self) -> bool: ...
    async def aclose(self) -> None: ...
```

`Beacon.advertise(spec)` should:

1. Validate the spec.
2. Create the record with `refreshSeq=1`, `ttlSeconds`, `createdAt`, and
   `updatedAt`.
3. Use direct KV create on the Beacon bucket.
4. Start a heartbeat task owned by the lease.
5. Refresh at `spec.refresh_interval`, defaulting to the existing 5 seconds.
6. On refresh, exact-read the current KV record or use cached revision plus
   conflict fallback.
7. Confirm ownership before update.
8. Increment `refreshSeq`.
9. Revision-guard the update.
10. Preserve the bucket TTL behavior.

---

## 5. Concord: direct materialized contract/token runtime

### Current code to remove or collapse

`ConcordCoordinator` is currently initialized with `contract_state: StateStore`
and `token_state: StateStore`. It lists contracts through `items(prefix)` and
watches contracts through generic state watches. Remove this as a public
construction layer.

Keep record models, key functions, terms hashing, handles, validity status
enums, and validation rules.

### New core API

Expose one primary class:

```python
class Concord:
    async def wait_ready(self) -> None: ...

    async def propose(self, spec: ConcordAgreementSpec) -> ConcordAgreementLease: ...

    async def attach(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        session_id: str,
    ) -> ConcordParticipantLease: ...

    async def cancel(
        self,
        contract: ContractHandle,
        *,
        participant: str | EndpointAddress,
        reason: str | None = None,
    ) -> bool: ...

    def get_contract(
        self,
        pointer: ContractPointer | Mapping[str, Any],
    ) -> ContractHandle | None: ...

    def contract_record(self, contract: ContractHandle) -> ContractRecord | None: ...

    def contracts(
        self,
        *,
        profile: str | None = None,
        participant: str | EndpointAddress | None = None,
        state: ContractState | None = None,
        contract_id: str | None = None,
    ) -> tuple[ContractHandle, ...]: ...

    def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity: ...

    def watch(
        self,
        *,
        profile: str | None = None,
        participant: str | EndpointAddress | None = None,
    ) -> AbstractAsyncContextManager[ObjectReceiveStream[ConcordEvent]]: ...
```

No normal read path should call a full KV prefix scan. `contracts(...)`,
`get_contract(...)`, and `validate(...)` should use materialized indexes. Exact
KV reads are allowed for write conflict recovery, audit, explicit
refresh/resync paths, and low-frequency Concord reaper maintenance.

### Internal indexes

Maintain at least:

```python
_contracts_by_key: dict[str, ContractEntry]
_contract_key_by_id_generation: dict[tuple[str, int], str]
_contract_keys_by_profile: dict[str | None, set[str]]
_contract_keys_by_participant: dict[str, set[str]]
_contract_keys_by_id: dict[str, set[str]]

_tokens_by_key: dict[str, TokenEntry]
_token_key_by_contract_participant: dict[tuple[str, int, str], str]

_validity_by_contract_key: dict[str, ContractValidity]
_last_event_status_by_contract_key: dict[str, ContractValidityStatus]
```

Where `ContractEntry` and `TokenEntry` contain record, key, revision, and parsed
identity.

When a contract bucket change arrives:

```text
put:
  parse contract key
  validate ContractRecord
  update contract indexes
  recompute validity for that contract
  emit proposed/updated/cancelled/valid/invalid as applicable

delete/expire:
  remove contract indexes
  drop related validity
  emit deleted/missing-contract event if previously known
```

When a token bucket change arrives:

```text
put/delete/expire:
  parse participant token key
  update token indexes
  recompute validity for affected contract
  emit token event and contract validity transition if status changed
```

### Concord semantic events

Use a single event model, not separate generic notification and managed event
types:

```python
class ConcordEventType(StrEnum):
    CONTRACT_PROPOSED = "contract_proposed"
    CONTRACT_UPDATED = "contract_updated"
    CONTRACT_VALID = "contract_valid"
    CONTRACT_PENDING = "contract_pending"
    CONTRACT_INVALID = "contract_invalid"
    CONTRACT_CANCELLED = "contract_cancelled"
    CONTRACT_DELETED = "contract_deleted"
    TOKEN_ATTACHED = "token_attached"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_WITHDRAWN = "token_withdrawn"


@dataclass(frozen=True, slots=True)
class ConcordEvent:
    event_type: ConcordEventType
    contract: ContractHandle | None
    record: ContractRecord | None = None
    validity: ContractValidity | None = None
    participant: EndpointAddress | None = None
    token: ParticipantHandle | None = None
    previous_validity: ContractValidityStatus | None = None
    reason: str | None = None
```

Emit `CONTRACT_PROPOSED` when an open contract appears and is not yet fulfilled.
Emit `CONTRACT_VALID` immediately when all required participant tokens are
present and valid. Emit `CONTRACT_CANCELLED` as soon as the contract record
moves to `cancelled`. Emit `TOKEN_EXPIRED` on MaxAge token marker.

Replace `watch_contracts()` and `watch_contract_notifications()` with the
materialized Concord event stream. Update indexes first and emit semantic events
from the core view.

### Validation

`validate(contract)` must remain Concord-only:

```text
valid iff:
  contract exists
  contract state is open
  every named participant is attached
  every named participant token exists
  each token names the same contract id and generation
  each token belongs to the participant whose key is checked
  terms hash matches when present
  current session evidence matches when supplied
```

Use cached materialized entries for this validation. If the runtime is not
ready, return or raise `UNAVAILABLE`. If strict exact validation is required for
a caller, add an explicit method:

```python
async def validate_exact(...) -> ContractValidity
```

Do not make exact reads the default hot path.

`ConcordReaperService` must not use cached `validate(...)` for cancellation or
deletion decisions. It should use `validate_exact(...)` or equivalent scan-time
raw KV reads so stale observations and maintenance cancellations are based on
current contract/token bucket contents.

### Agreement lease and participant lease

Keep the core-owned heartbeat behavior. Applications should not manually write
participant tokens.

Expose:

```python
class ConcordAgreementLease:
    @property
    def contract(self) -> ContractHandle: ...
    @property
    def validity(self) -> ContractValidity: ...
    @property
    def local_token(self) -> ParticipantHandle | None: ...

    async def refresh(self) -> ContractValidity: ...
    async def cancel(self, reason: str | None = None) -> bool: ...
    async def aclose(self) -> None: ...


class ConcordParticipantLease:
    @property
    def contract(self) -> ContractHandle: ...
    @property
    def token(self) -> ParticipantHandle | None: ...

    async def attach_or_refresh(self) -> ParticipantHandle: ...
    async def aclose(self) -> None: ...
```

Heartbeat rules:

1. Default participant-token TTL remains 30 seconds.
2. Default refresh interval remains 15 seconds.
3. A lease may validate/adopt a fresher same-session token without immediately
   writing.
4. A participant must not silently recreate a missing token for a generation
   after it had already attached.
5. Token writes must be create or revision-guarded update.
6. Contract cancellation must be revision-guarded update.
7. Participant attach must exact-read or cached-read the current contract,
   confirm the contract is open, confirm the participant is named, create its
   own token key, and add itself to `attachedParticipants` with a
   revision-guarded contract update.

---

## 6. Replace `ConcordParticipantManager` with a view-backed participant actor

The current `ConcordParticipantManager` already hints at the desired design: it
maintains `_contract_index`, `_managed`, `_leases`, and `_last_status`, and has
watch/reconcile loops. Replace it with a smaller participant actor driven by the
materialized Concord view:

```python
class ConcordParticipant:
    def __init__(
        self,
        concord: Concord,
        *,
        participant: str | EndpointAddress,
        session_id: str,
        profile: str | None = None,
        accept: Callable[[ContractHandle, ContractRecord], bool | Awaitable[bool]],
        current_sessions: Callable[
            [ContractHandle],
            Mapping[str, str] | Awaitable[Mapping[str, str]],
        ] | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    ) -> None: ...

    async def start(self, task_group: anyio.abc.TaskGroup) -> None: ...
    async def aclose(self) -> None: ...

    def managed(self) -> tuple[ConcordManagedContract, ...]: ...
    def watch(
        self,
    ) -> AbstractAsyncContextManager[ObjectReceiveStream[ConcordManagedContractEvent]]: ...
```

Behavior:

```text
on Concord CONTRACT_PROPOSED/UPDATED/PENDING for matching participant/profile:
    call accept(contract, record)
    if accepted:
        attach or refresh local token immediately
        start/maintain heartbeat lease

on TOKEN_EXPIRED/TOKEN_WITHDRAWN for local participant:
    release lease and emit invalid/released

on CONTRACT_CANCELLED/DELETED:
    release lease and emit cancelled/released

periodic loop:
    only refresh active leases
    do not rescan all contracts unless the Concord view reports resync/recovery
```

This ensures proposals are handled from materialized Concord events instead of
waiting for periodic full scans or coalesced notification reconciliation.

---

## 7. Rewrite service views to direct JetStream/KV

### Current code to replace

Service view helpers are currently contained in this `deckr` Python package
under `deckr.services.runtime`, though not in the `deckr.core` subpackage.

Replace these `StateStore`-backed helpers:

```text
ServiceViewReader
ServiceViewWriter
```

Do not remove service views from `deckr`. Rewrite them as direct JetStream/KV
primitives that take advantage of the non-abstracted API.

### Target service-view API shape

Keep service-view support service-specific. Do not create a generic state API.

Expose a direct JetStream-backed runtime such as:

```python
@dataclass(frozen=True, slots=True)
class ServiceViewEntry:
    bucket: str
    key: str
    value: Mapping[str, Any]
    revision: int
    service_id: str
    service_namespace: str
    session_id: str


class ServiceViewStore:
    async def wait_ready(self) -> None: ...

    async def get(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> ServiceViewEntry | None: ...

    async def put(
        self,
        *,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        service_id: str,
        service_namespace: str,
        session_id: str,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry: ...

    async def delete(
        self,
        *,
        view: ServiceViewRef,
        revision: int | None = None,
    ) -> None: ...

    def watch(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> AbstractAsyncContextManager[ObjectReceiveStream[ServiceViewChange]]: ...
```

The exact names can change during implementation, but the behavior must be
service-view-specific:

```text
- expose bucket, key, revision, CAS, exact-read, watch, delete, and TTL behavior
- validate access through an active Concord service-use lease before protected
  reads or watches
- fence entries by serviceId, serviceNamespace, and sessionId
- preserve view-family and key-prefix authorization
- use direct JetStream/KV operations, not StateStore
```

### Service runtime boundary

Remove the broad service runtime layer that combines discovery, Concord,
commands, and views into one helper stack:

```text
ServiceAdvertiser
ServiceUseLeaseManager
ServiceUseAuthorizer
ServiceCommandChannel
```

Keep only pure service profile/message/model helpers if still needed:

```text
ServiceDescriptor
ServiceUseTerms
ServiceAdvertisementPayload
ServiceViewFamily
ServiceViewRef
service_use_terms(...)
parse_service_descriptor(...)
service_view_key(...)
service_view_prefix(...)
services lane message schemas
```

Service-like behavior should be composed explicitly:

```text
Beacon advertises service descriptors
Concord authorizes service-use contracts and validates leases
Lanes carry ordinary service command/reply messages
JetStream/KV stores protected service views
```

Beacon does not authorize protected view access. The `services` lane does not
own discovery, authority, views, or state.

---

## 8. Runtime wiring

Update `Deckr` so the default NATS runtime wires lanes, Beacon, Concord, and
service-view JetStream access explicitly.

Remove `Deckr.state()` entirely. Remove `state()` from the lane substrate
protocol. Lanes should publish, request/reply, and subscribe only.

Target shape:

```python
async with Deckr(...) as deckr:
    beacon = deckr.beacon
    concord = deckr.concord
    service_views = deckr.service_views
    lane = deckr.lane("hardware_messages")
```

`Deckr.__aenter__` should:

```text
1. Connect NATS.
2. Create/start Beacon materialized bucket view.
3. Create/start Concord contract/token/maintenance materialized bucket views.
4. Create/start service-view JetStream/KV access as needed.
5. Wait for Beacon and Concord readiness, or expose wait_ready().
6. Start lane runtime.
```

NATS substrate should expose raw `nc`/`js` or small internal factories so
Beacon, Concord, and service views can create their own buckets without going
through any generic state layer.

---

## 9. File-level change plan

### `src/deckr/substrates/nats.py`

Do:

```text
- Keep lane publish/request/subscribe behavior.
- Remove `state()` from `LaneSubstrate` obligations.
- Delete `NatsStateStore`.
- Move reusable NATS KV marker parsing helpers into `nats_kv.py`.
- Expose internal access to `nc`/`js` or narrowly scoped factories for
  protocol runtimes.
```

### `src/deckr/substrates/supervised_nats.py`

Do:

```text
- Keep supervised local NATS startup.
- Delegate lane behavior to `NatsSubstrate`.
- Remove supervised `state()` passthrough.
```

### `src/deckr/state.py`

Do:

```text
- Delete this module, or leave only a temporary private migration stub if needed
  to sequence implementation.
- Do not keep any production import path that exposes StateStore APIs.
```

### `src/deckr/lanes.py`

Do:

```text
- Remove `StateStore` and `StateStorePolicy` imports.
- Remove `LaneSubstrate.state()`.
- Keep lane contracts, endpoint registration, publish, request/reply, and
  subscribe behavior unchanged.
```

### `src/deckr/runtime.py`

Do:

```text
- Remove `Deckr.state()`.
- Add explicit `beacon`, `concord`, and service-view accessors.
- Start protocol runtimes through direct JetStream/KV wiring.
```

### `src/deckr/beacon.py`

Do:

```text
- Keep record models, key helpers, statuses, and events.
- Remove public `BeaconDiscovery`.
- Replace `BeaconService` internals with direct JetStream/KV materialized bucket
  access.
- Rename primary API to `Beacon`.
- Do not keep compatibility aliases or old constructors.
- Make candidate reads memory-backed after readiness.
- Ensure heartbeat writes are owned by `BeaconAdvertisementLease`.
```

### `src/deckr/concord.py`

Do:

```text
- Keep record models, key helpers, terms hashing, handles, and validity enums.
- Collapse `ConcordCoordinator` into `Concord`.
- Replace StateStore-backed scans/watches with materialized contract/token bucket
  views.
- Replace `watch_contract_notifications()` and `watch_contracts()` with one
  semantic `watch()`.
- Replace `ConcordParticipantManager` with a view-backed
  `ConcordParticipant`.
- Ensure all token/contract heartbeats and CAS writes pass through Concord.
```

### `src/deckr/concord_reaper.py`

Do:

```text
- Remove StateStore constructor dependencies.
- Operate through Concord's direct raw maintenance/contract/token bucket access
  or a Concord-owned maintenance API.
- Do not maintain contract, token, or maintenance materialized views or watches
  for the standalone reaper.
- `scan_once()` may do a full `contracts.` key scan and a full `stale.` key scan
  on each run.
- Validate open contracts from exact scan-time contract/token KV reads, not from
  Concord's cached `validate(...)` hot path.
- Persist `firstObservedStaleAt` in `deckr_concord_maintenance_v1` only for
  stale open-contract statuses. `unavailable` is not stale.
- After the stale grace period, cancel stale open contracts with a
  revision-guarded contract update using
  `cancelReason=concord_reaper_stale_contract` and
  `cancelledBy=concord:maintenance`.
- Delete cancelled contracts only after `cancelledAt` plus retention. Before
  deleting, audit identity, profile, participants, lifecycle timestamps,
  cancellation metadata, supersession, terms hash, current validation status,
  and participant-token summaries without logging full `terms` by default.
- After deleting a contract record, delete remaining participant-token keys for
  that contract generation.
- If a revision-guarded maintenance write/delete conflicts, log and leave the
  entry for a later scan.
```

### `src/deckr/services/`

Do:

```text
- Remove StateStore-backed `ServiceViewReader` and `ServiceViewWriter`.
- Replace service views with direct JetStream/KV helpers.
- Remove broad service runtime helpers from public imports.
- Keep pure service profile and message contracts only where still useful.
```

### `tests/`

Do:

```text
- Delete `MemoryStateStore` and StateStore-specific tests.
- Replace state-store fakes with protocol-specific fakes or NATS/JetStream-backed
  tests.
- Rewrite Beacon, Concord, and service-view tests around direct JetStream/KV.
```

---

## 10. Tests to add or rewrite

### Materialized KV tests

Add tests for `NatsKvMaterializedBucket`:

```text
- hydrates existing keys on startup
- emits put/delete/expire changes
- does not resurrect stale snapshot values after a newer delete marker
- ignores stale revisions
- recovers by full resync after subscription failure
- does not do per-query KV scans
```

For "does not do per-query scans", instrument the materialized bucket with
counters or use a fake KV. Assert that these hot paths do not call `kv.keys()`,
`kv.watch()`, or equivalent scan operations after readiness:

```text
beacon.candidates(...)
concord.contracts(...)
concord.validate(...)
service_views.get(...)
service_views.watch(...) after initial subscription setup
```

Do not include `ConcordReaperService` in this no-scan assertion. The reaper is
the intentional full-scan maintenance path.

### Beacon tests

Add tests:

```text
- advertise creates correct key and schema
- heartbeat increments refreshSeq at core-controlled interval
- candidates(feature_id) returns from local index
- watch(feature_id) emits advertised/updated/withdrawn/expired
- withdraw deletes with revision guard
- invalid record emits INVALID and is not returned as candidate
```

Use short TTLs in tests.

### Concord tests

Add tests:

```text
- propose emits contract_proposed without polling
- participant actor attaches immediately on accepted proposed contract
- contract becomes valid as soon as required tokens exist
- token expiry emits token_expired and contract_invalid/missing_token
- cancel emits contract_cancelled immediately
- contracts(profile=..., participant=...) reads from indexes
- stable contract id generation selection uses indexes, not scans
- validation uses cached materialized view
- exact validation remains available for explicit strict checks
```

### Concord reaper tests

Add tests:

```text
- scan_once can list contracts. and stale. without starting materialized watches
- pending open contract creates stale observation and cancels only after grace
- missing token exact validation creates stale observation and cancels only after
  grace
- unavailable does not create or advance stale observation
- stale observation survives reaper restart
- stale observation clears when the contract is cancelled, deleted, or no longer
  stale
- cancelled contract is deleted only after retention and remaining participant
  tokens are removed
- delete/cancel conflicts leave the entry for a later scan
```

### Service-view tests

Add tests:

```text
- direct JetStream/KV put stores fenced serviceId/serviceNamespace/sessionId
- read requires Concord lease authorization
- read rejects entries fenced to a different service/session
- watch emits put/delete/expire changes for an authorized view
- revision-guarded update detects conflicts
- delete can be revision-guarded
- TTL expiry is surfaced distinctly from explicit delete
- view-family prefixes are enforced
- service-view helpers do not import or use StateStore
```

### Integration smoke rewrite

Rewrite the NATS smoke script so the controller/manager do not poll:

```text
manager:
  start beacon advertisement lease
  start ConcordParticipant actor for hardware claim profile
  write protected service views through direct JetStream/KV helpers if needed
  respond to lane messages only after Concord says claim is valid

controller:
  wait for Beacon advertised event or candidates from ready materialized view
  propose Concord agreement
  await Concord contract_valid event
  read/watch protected service views through Concord-authorized JetStream access
  send lane command
```

---

## 11. Wire compatibility constraints

These constraints preserve the language-neutral wire protocol. They are not
Python API backwards-compatibility requirements.

Do not change these unless explicitly required:

```text
Beacon key:
  advertisements.by_feature.<feature-id-token>.<advertisement-id-token>

Concord contract key:
  contracts.<contract-id-token>.<generation>.meta

Concord token key:
  contracts.<contract-id-token>.<generation>.participants.<participant-token>
```

These key shapes are documented as part of the NATS contract.

Do not change:

```text
Beacon advertisement schema id
Concord contract schema id
Concord participant-token schema id
termsHash algorithm
TTL defaults
refresh defaults
CAS create/update/delete semantics
```

The normative guide says state transitions use create, revision-guarded update,
and revision-guarded delete; keep that behavior through direct JetStream/KV
operations.

---

## 12. Definition of done

The refactor is complete when:

```text
1. StateStore is removed from the Python core runtime and public API.
2. No production Python module imports StateStore, StateStorePolicy, StateEntry,
   or StateChange.
3. Deckr and LaneSubstrate no longer expose state().
4. Beacon directly owns the Beacon advertisement KV bucket and materialized
   indexed view.
5. Concord directly owns the contract/token/maintenance KV buckets and
   materialized indexed views.
6. Service views remain in deckr but use direct JetStream/KV primitives, not
   StateStore.
7. Beacon candidates, Concord contract queries, and service-view reads are
   memory-backed or exact-key-backed after startup, not repeated full scans.
8. Contract proposal, cancellation, token attach, token expiry, advertisement
   changes, and service-view changes produce semantic events without waiting for
   periodic scans.
9. Heartbeats for Beacon advertisements and Concord participant tokens are owned
   by core Beacon/Concord leases.
10. Service-like behavior is expressible as Beacon + Concord + lanes +
    direct JetStream/KV service views.
11. `ConcordReaperService` uses exact raw KV scans, not materialized watches,
    and bounds the Concord archive by cancelling stale open contracts, deleting
    retained cancelled contracts, clearing orphaned stale observations, and
    deleting leftover participant-token keys.
12. Tests prove no repeated full key scans occur on normal hot paths and that
    the reaper full-scan exception is intentional.
13. Existing wire compatibility is preserved for non-Python implementations.
14. No shims, aliases, deprecated wrappers, backwards-compatible constructors,
    or compatibility functions remain for removed Python APIs.
```

The important implementation principle is: **events are notifications, but the
hydrated materialized KV view is the current authority.** Beacon, Concord, and
service views should not force callers to work around a generic,
non-authoritative state abstraction when the protocol already has explicit
authoritative JetStream/KV buckets.
