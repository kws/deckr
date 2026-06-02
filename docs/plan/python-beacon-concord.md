# Agent instructions: Python Beacon/Concord materialized-KV refactor

## 1. Goal

Refactor the Python runtime so that **Beacon** and **Concord** are first-class NATS/JetStream KV protocol runtimes, not clients of the generic `StateStore` abstraction.

The desired core model is:

```text
Beacon  = materialized KV view of Beacon advertisement bucket + core-owned advertisement heartbeats
Concord = materialized KV view of contract/token buckets + core-owned participant token heartbeats
Lanes   = Core NATS message queues
Service = not a core layer; remove current Python service runtime helpers
```

The branch already documents this as the intended protocol boundary: Core NATS carries lane messages, JetStream KV backs explicit protocol stores, Beacon provides weak discovery, and Concord provides live agreements. The concrete buckets are already specified as `deckr_beacon_advertisement_v1`, `deckr_concord_contract_v1`, `deckr_concord_token_v1`, and `deckr_concord_maintenance_v1`. 

Keep all language-neutral wire semantics: key shapes, schemas, CAS behavior, TTL policy, canonical terms hashing, and Beacon/Concord separation. Do **not** turn Beacon into live authority; Concord remains the authority for established agreements. 

---

## 2. Remove the wrong abstraction boundary

### Problem to fix

`StateStore` currently sits between Beacon/Concord and NATS. Its API explicitly says prefix reads are not authoritative and watches are not authoritative event logs.  That is the wrong surface for Beacon/Concord, because the current materialized NATS KV bucket state is exactly the protocol authority.

The current NATS `StateStore.items()` path is also expensive: it opens a KV watch, consumes an initial snapshot, filters markers, then tears down the ephemeral consumer.  That makes every `find()` or reconciliation scan pay for a new KV observation. The watch path also coalesces changes by key through `_LatestStateWatchPump`, which is useful for generic current-state wakeups but not precise enough as the primary Beacon/Concord runtime event source. 

### Instruction

Do **not** wire Beacon or Concord through `StateStore`.

Keep `StateStore` only for app-owned generic state if required, but remove it from:

```text
BeaconDiscovery
BeaconService
ConcordCoordinator
ConcordService
ConcordParticipantManager
ConcordReaperService, except possibly maintenance bucket access during transitional work
```

After the refactor, Beacon/Concord code should open their own NATS KV buckets directly and own their own materialized views.

---

## 3. Introduce a NATS KV materialized-view primitive

Create a Python-only internal module, for example:

```text
src/deckr/substrates/nats_kv.py
```

Define a small internal primitive:

```python
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

Define:

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
    async def update(self, key: str, value: Mapping[str, Any], *, revision: int) -> KvEntry: ...
    async def delete(self, key: str, *, revision: int | None = None) -> None: ...

    def subscribe(self) -> AsyncContextManager[ObjectReceiveStream[KvChange]]: ...
```

### Bucket behavior

The primitive must:

1. Open or create the JetStream KV bucket with the existing policy rules.
2. Enforce `max_msgs_per_subject=1` and the configured bucket TTL, matching the current NATS stream configuration logic. The existing implementation already updates stream config to set `max_age`, `max_msgs_per_subject`, and `allow_msg_ttl` when required. 
3. Hydrate a local in-memory map once at startup.
4. Subscribe to raw `$KV.<bucket>.<prefix>` changes and update the local map continuously.
5. Emit `KvChange` events to subscribers.
6. Avoid repeat full scans in normal operation.
7. Trigger a full resync only on startup, explicit recovery, or subscription failure.

### Hydration algorithm

Use a no-gap startup sequence:

```text
1. Open raw JetStream subscription first and buffer changes.
2. Load current snapshot from KV keys/get, or one KV watch initial snapshot.
3. Apply buffered changes in revision order, ignoring stale changes whose revision <= cached revision.
4. Mark bucket ready.
5. Continue applying live changes.
```

This prevents missing changes between snapshot hydration and watch start.

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

Keep `revision_by_key` or equivalent tombstone state so a late snapshot `put` cannot resurrect a key after a delete/expiry marker.

Reuse the existing marker parsing logic from `src/deckr/substrates/nats.py`, especially:

```text
KV-Operation: DEL/PURGE -> delete
Nats-Marker-Reason: MaxAge -> expire
```

The current helper already distinguishes delete from expiry using NATS marker headers. 

---

## 4. Beacon: replace `BeaconDiscovery` with a direct materialized Beacon runtime

### Current code to remove or collapse

`BeaconDiscovery` currently accepts a `StateStore`, advertises through `state.create`, refreshes through exact read plus revision update, finds through `state.items(prefix)`, and watches through `state.watch(prefix)`.   

Remove `BeaconDiscovery` as a public construction layer. Either delete it or make it a private compatibility shim during transition.

### New core API

Keep the API small:

```python
class Beacon:
    async def wait_ready(self) -> None: ...

    async def advertise(self, spec: BeaconAdvertisementSpec) -> BeaconAdvertisementLease: ...

    def candidates(
        self,
        feature_id: str,
        *,
        selector: AdvertisementFilter | None = None,
    ) -> tuple[Candidate, ...]: ...

    def get(self, *, feature_id: str, advertisement_id: str) -> Candidate | None: ...

    async def validate(
        self,
        candidate: Candidate,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> CandidateStatus: ...

    def watch(
        self,
        feature_id: str | None = None,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[BeaconFeatureEvent]]: ...
```

Use `Beacon`, not `BeaconService`, as the primary class. If backwards compatibility is needed, temporarily alias:

```python
BeaconService = Beacon
```

but do not preserve the old constructor shape.

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
6. Emit:

   * `ADVERTISED` if key was previously absent
   * `UPDATED` if key existed
   * `INVALID` if key/record/schema invalid

On `delete` or `expire`:

1. Remove from `_entries_by_key`.
2. Remove from `_keys_by_feature`.
3. Emit:

   * `WITHDRAWN` for explicit delete
   * `EXPIRED` for MaxAge expiry

The old per-watch `known` dictionary in `BeaconService.watch_feature()` should go away. It currently exists only inside a watch context.  The core Beacon runtime should maintain the authoritative local indexed view for all watchers.

### Advertisement lease and heartbeats

Keep core-owned heartbeats. Do **not** make applications manage refresh timing or `refreshSeq`.

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
2. Create the record with `refreshSeq=1`, `ttlSeconds`, `createdAt`, and `updatedAt`.
3. Use `kv.create(key, record)`.
4. Start a heartbeat task owned by the lease.
5. Refresh at `spec.refresh_interval`, defaulting to the existing 5 seconds.
6. On refresh, exact-read current KV record or use cached revision plus conflict fallback.
7. Confirm ownership before update.
8. Increment `refreshSeq`.
9. Revision-guard update.
10. Preserve the bucket TTL behavior.

The current Beacon model already has the correct create/refresh/withdraw semantics; keep those semantics, but route them through the Beacon-owned KV bucket rather than generic `StateStore`. 

---

## 5. Concord: replace `ConcordCoordinator` with a direct materialized Concord runtime

### Current code to remove or collapse

`ConcordCoordinator` is currently initialized with `contract_state: StateStore` and `token_state: StateStore`.  It lists contracts through `items(prefix)` and watches contracts through generic state watches.  Remove this as a public construction layer.

Keep the record models, key functions, terms hashing, handles, validity status enums, and validation rules. Those are valuable.

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

    def get_contract(self, pointer: ContractPointer | Mapping[str, Any]) -> ContractHandle | None: ...

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
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[ConcordEvent]]: ...
```

No normal read path should call a full KV prefix scan. `contracts(...)`, `get_contract(...)`, and `validate(...)` should use the materialized indexes. Exact KV reads are allowed for write conflict recovery, audit, and explicit refresh/resync paths.

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

Where `ContractEntry` and `TokenEntry` contain record, key, revision, and parsed identity.

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

Use a single event model, not separate generic notification and managed event types:

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
```

```python
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

Emit `CONTRACT_PROPOSED` when an open contract appears and is not yet fulfilled. Emit `CONTRACT_VALID` immediately when all required participant tokens are present and valid. Emit `CONTRACT_CANCELLED` as soon as the contract record moves to `cancelled`. Emit `TOKEN_EXPIRED` on MaxAge token marker.

The existing `watch_contracts()` and `watch_contract_notifications()` duplicate responsibilities and are backed by generic state watches. Replace them with the materialized Concord event stream. The current implementation runs one contract watch and one token watch, then validates on every notification.  The new implementation should update indexes first and emit semantic events from the core view.

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

This mirrors the current validation logic and the spec. 

Use cached materialized entries for this validation. If the runtime is not ready, return or raise `UNAVAILABLE`. If strict exact validation is required for a caller, add an explicit method:

```python
async def validate_exact(...) -> ContractValidity
```

Do not make exact reads the default hot path.

### Agreement lease and participant lease

Keep the core-owned heartbeat behavior. Applications should not manually write participant tokens.

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
```

Expose:

```python
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
3. A lease may validate/adopt a fresher same-session token without immediately writing.
4. A participant must not silently recreate a missing token for a generation after it had already attached.
5. Token writes must be create or revision-guarded update.
6. Contract cancellation must be revision-guarded update.
7. Participant attach must:

   * exact-read or cached-read current contract;
   * confirm contract is open;
   * confirm participant is named;
   * create own token key;
   * add self to `attachedParticipants` with revision-guarded contract update.

The current Concord code already has these semantics; preserve them while replacing the storage access path. 

---

## 6. Replace `ConcordParticipantManager` with a view-backed participant actor

The current `ConcordParticipantManager` already hints at the desired design: it maintains `_contract_index`, `_managed`, `_leases`, and `_last_status`, and has watch/reconcile loops.  But it still rebuilds from `ConcordService._find_contracts()` and reconciles via generic notifications.  

Replace it with a smaller core-owned participant actor:

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
        current_sessions: Callable[[ContractHandle], Mapping[str, str] | Awaitable[Mapping[str, str]]] | None = None,
        refresh_interval: float = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    ) -> None: ...

    async def start(self, task_group: anyio.abc.TaskGroup) -> None: ...
    async def aclose(self) -> None: ...

    def managed(self) -> tuple[ConcordManagedContract, ...]: ...
    def watch(self) -> AbstractAsyncContextManager[ObjectReceiveStream[ConcordManagedContractEvent]]: ...
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

This ensures proposals are handled from materialized Concord events instead of waiting for periodic full scans or coalesced notification reconciliation.

---

## 7. Remove the Python service runtime layer

### Delete or quarantine

Remove these high-level service runtime helpers from the core package:

```text
ServiceAdvertiser
ServiceUseLeaseManager
ServiceUseAuthorizer
ServiceCommandChannel
ServiceViewReader
ServiceViewWriter
```

They currently combine Beacon advertising, Concord service-use contracts, service-lane commands, and generic `StateStore` views into another abstraction layer.    

For now, keep only pure data/model helpers if still needed by tests or examples:

```text
ServiceDescriptor
ServiceUseTerms
ServiceAdvertisementPayload
service_use_terms(...)
parse_service_descriptor(...)
```

Prefer moving those into a profile module, for example:

```text
src/deckr/profiles/service.py
```

Do not keep them as a “service runtime.”

### Keep lanes separate

Service command messages, if still needed, should be treated as ordinary lane message schemas. Do not let them own discovery, authority, views, or state. The docs already say service command and protected-view authority follows Concord after a service-use contract is negotiated. 

---

## 8. Runtime wiring

Update `Deckr` so the default NATS runtime wires lanes, Beacon, and Concord explicitly.

Current `Deckr.state()` exposes generic state via the substrate.  Keep that only if application state still needs it, but remove `state()` from the lane substrate protocol. Lanes should publish/subscribe messages only.

Target shape:

```python
async with Deckr(...) as deckr:
    beacon = deckr.beacon
    concord = deckr.concord
    lane = deckr.lane("hardware_messages")
```

`Deckr.__aenter__` should:

```text
1. Connect NATS.
2. Create/start Beacon materialized bucket view.
3. Create/start Concord contract/token/maintenance materialized bucket views.
4. Wait for Beacon and Concord readiness or expose wait_ready().
5. Start lane runtime.
```

NATS substrate should expose the raw `nc`/`js` or a small internal factory so Beacon and Concord can create their own buckets without going through `StateStore`.

---

## 9. File-level change plan

### `src/deckr/substrates/nats.py`

Do:

```text
- Keep lane publish/request/subscribe behavior.
- Remove `state()` from `LaneSubstrate` obligations.
- Keep old `NatsStateStore` temporarily only for app state if needed.
- Move reusable NATS KV marker parsing helpers into `nats_kv.py`.
```

Do not let Beacon/Concord call `NatsStateStore`.

### `src/deckr/state.py`

Do:

```text
- Leave for app-owned generic current state if required.
- Remove Beacon/Concord imports of StateStore.
- Consider renaming in a follow-up to avoid conceptual confusion.
```

### `src/deckr/beacon.py`

Do:

```text
- Keep record models, key helpers, statuses, events.
- Remove public `BeaconDiscovery`.
- Replace `BeaconService` internals with direct NATS KV materialized bucket.
- Rename primary API to `Beacon`.
- Keep compatibility alias only if necessary.
- Make `find()` a cached view read or rename it to `candidates()`.
- Ensure heartbeat writes are owned by `BeaconAdvertisementLease`.
```

### `src/deckr/concord.py`

Do:

```text
- Keep record models, key helpers, terms hashing, handles, validity enums.
- Collapse `ConcordCoordinator` into `Concord`.
- Replace StateStore-backed scans/watches with materialized contract/token bucket views.
- Replace `watch_contract_notifications()` and `watch_contracts()` with one semantic `watch()`.
- Replace `ConcordParticipantManager` with a view-backed `ConcordParticipant`.
- Ensure all token/contract heartbeats and CAS writes pass through Concord.
```

### `src/deckr/services/`

Do:

```text
- Remove `runtime.py` from core imports.
- Remove runtime helpers from `src/deckr/services/__init__.py`.
- Keep or move pure service profile models only if still used.
- Delete tests/examples that require the service runtime, or rewrite them against Beacon + Concord + lanes.
```

### `docs/`

Update docs to state:

```text
- Python Beacon and Concord directly own NATS KV buckets.
- StateStore is not part of Beacon/Concord protocol authority.
- Service runtime helpers were removed from core Python.
- Service-like behavior is composed from Beacon descriptor advertisement, Concord agreement, and lanes.
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

For “does not do per-query scans,” instrument the materialized bucket with counters or use a fake KV. Assert that:

```text
beacon.candidates(...)
concord.contracts(...)
concord.validate(...)
```

do not call `kv.keys()`, `kv.watch()`, or equivalent scan operations after readiness.

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

### Integration smoke rewrite

Rewrite the NATS smoke script so the controller/manager do not poll:

Current smoke code waits for Beacon by polling `beacon.find()` and waits for Concord validity by repeatedly calling `agreement.refresh()`.  Replace with:

```text
manager:
  start beacon advertisement lease
  start ConcordParticipant actor for hardware claim profile
  respond to lane messages only after Concord says claim is valid

controller:
  wait for Beacon advertised event or candidates from ready materialized view
  propose Concord agreement
  await Concord contract_valid event
  send lane command
```

---

## 11. Compatibility constraints

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

The normative guide says state transitions use `create`, revision-guarded `update`, and revision-guarded `delete`; keep that. 

---

## 12. Definition of done

The refactor is complete when:

```text
1. Beacon and Concord no longer depend on StateStore.
2. Beacon and Concord directly own NATS KV buckets and materialized indexed views.
3. Beacon candidates and Concord contract queries are memory-backed after startup.
4. Contract proposal, cancellation, token attach, and token expiry produce semantic events without waiting for periodic scans.
5. Heartbeats for Beacon advertisements and Concord participant tokens are owned by core Beacon/Concord leases.
6. The Python service runtime helpers are removed from the core public API.
7. Service-like behavior is expressible as Beacon + Concord + lanes.
8. Tests prove no repeated full key scans occur on normal hot paths.
9. Existing wire compatibility is preserved for non-Python implementations.
```

The important implementation principle is: **events are notifications, but the hydrated materialized KV view is the current authority.** Beacon and Concord should not force callers to work around a generic, non-authoritative `StateStore` abstraction when the protocol already has explicit authoritative NATS KV buckets.
