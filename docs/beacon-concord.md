# Beacon And Concord

> Normative v1 implementor guide: this document defines the language-neutral
> Beacon and Concord protocol semantics implemented by `deckr`. If this guide,
> the generated `contract/v1` artifacts, and the implementation disagree, treat
> that as a contract bug and fix the docs, artifacts, or implementation together.

Beacon and Concord are Deckr's shared discovery and agreement protocols:

- Beacon provides weak feature discovery through temporary advertisements.
- Concord provides live multi-participant agreements through contracts and
  participant tokens.

Beacon finds candidates. Concord binds participants.

## Sources Of Truth

Use these references together:

- this guide: protocol semantics and conformance rules
- [`nats-bus.md`](nats-bus.md): NATS subjects, KV buckets, store policies, and
  broker operations
- [`../contract/v1`](../contract/v1): language-neutral schemas, fixtures, and
  acceptance vectors

The JSON Schemas are structural wire contracts. They intentionally do not carry
every semantic rule. Every implementation must enforce the semantic rules in
this guide, including endpoint parsing, positive counters and TTLs, canonical
participant ordering, terms-hash validation, and profile identity checks.

All requirements in this specification apply equally to every implementation.
Implementation APIs and source models do not qualify or override protocol rules.
Runtime API examples and tooling are documented separately in the
[runtime reference](protocol-runtime-reference.md).

Retired current-state authorities are not part of v1. Do not treat endpoint
presence, action catalogs, hardware inventory, unilateral device claims, lease
buckets, or discovery buckets as parallel authority for Beacon or Concord.

## Discovery And Agreement Boundary

Beacon is discovery only. A Beacon advertisement can make an endpoint a
candidate for a new Concord negotiation, but it is not part of any existing
Concord contract's validity after that contract is negotiated.

An advertiser may withdraw or stop refreshing Beacon advertisements when it is
not accepting new Concord negotiations. That affects only future discovery and
negotiation. It does not invalidate, withdraw, cancel, degrade, or pause any
already negotiated Concord contract.

After a Concord contract exists, live authority and withdrawal are Concord
concerns. A participant withdraws by cancelling the contract, stopping its own
participant token, or allowing its participant token to expire. Profile-specific
loss of authority, such as a disconnected claimed device or a stopped service,
must be expressed through Concord cancellation, token loss, or profile-owned
Concord validation failure. Missing, withdrawn, expired, replaced, or changed
Beacon advertisements must not by themselves invalidate an existing Concord
contract or stop live rendering, routing, commands, or view consumption.

## Shared Rules

Endpoint addresses serialize as:

```text
<endpoint-family>:<endpoint-id>
```

The core endpoint families are `controller`, `hardware_manager`,
`action_provider`, and `service`. Endpoint addresses must not contain leading or
trailing whitespace. The id must be non-empty and must not contain `:`. Action
provider endpoint ids must match `[A-Za-z0-9][A-Za-z0-9._-]*` and must not use
reserved provider identities such as `dev.deckr.controller.builtin`.

State keys use the shared key-token encoding:

- safe tokens matching `[A-Za-z0-9][A-Za-z0-9_-]*` stay unchanged, except values
  beginning with `b64_`
- all other tokens are UTF-8 bytes encoded as URL-safe base64 without padding,
  prefixed with `b64_`

Concord terms hashes use canonical JSON:

- dump the JSON value with object keys sorted
- use compact separators with no insignificant whitespace
- preserve Unicode as UTF-8 JSON text
- hash the resulting bytes with SHA-256
- serialize as `sha256:<hex-digest>`

State transitions use compare-and-set when the substrate supports it. Initial
creation uses `create`; refresh and cancellation use revision-guarded `update`;
withdrawal/deletion should use revision-guarded `delete`. Watches are wakeups.
For language-neutral protocol semantics, exact reads and recomputed validity are
the strict authority path.

## Beacon

A Beacon advertisement is a fresh candidate record:

```text
I advertise feature F at endpoint E. You may try me.
```

Beacon does not prove endpoint liveness, health, authorization, resource
availability, ownership, future success, or command safety. Consumers must treat
Beacon results as candidates and establish any required Concord agreement before
acting under live authority. Once that agreement exists, continued Beacon
advertisement presence is not live-use authority.

Beacon advertisements use the schema
`dev.deckr.beacon.advertisement.v1`. The NATS-backed store and key shape are
specified in [`nats-bus.md`](nats-bus.md#beacon-store).

An advertisement record must have:

- `advertisementId`, `featureId`, and `sessionId` as non-empty strings with no
  leading or trailing whitespace
- valid `advertiser` and `endpoint` endpoint addresses
- `refreshSeq > 0`
- `ttlSeconds > 0`
- optional `operations`, `labels`, `hints`, and `payload`

Create an advertisement with a unique advertisement id using `create`. Refresh
it by exact-read, owner-check, incrementing `refreshSeq`, and revision-guarded
`update`. Withdraw it by exact-read, owner-check, and revision-guarded delete.
Runtimes own advertisement contexts and all heartbeat scheduling. Beacon derives
`ttlSeconds` and cadence from the Beacon KV bucket TTL; neither is caller
configuration. The default Beacon bucket TTL is 300 seconds.
Managed heartbeat refresh writes are scheduled with jitter
between `ttlSeconds * 0.5` and `ttlSeconds * 0.75`, so the default cadence is
150-225 seconds. Real payload, label, hint, protocol, or operation changes
publish immediately, but unchanged heartbeat refreshes may be skipped while the
stored value is already fresh enough. If the advertisement is no longer
refreshed, the TTL-bound store removes it. Beacon advertisements are not reaped
by Concord maintenance. Their lifecycle is the advertisement owner plus the
store TTL. A managed advertiser that is still running and observes its own
advertisement key missing must treat that as recoverable TTL/store loss and
publish the advertisement again instead of retrying the missing key forever.

Managed advertisers should best-effort remove stale advertisements for the same
feature, advertiser, and endpoint during startup or replacement, using
revision-guarded deletes. This cleanup affects future discovery only and must
not be interpreted as Concord withdrawal.

A Beacon candidate is usable only if:

- the record exists
- the record validates against the Beacon envelope schema and semantic rules
- the record key and advertisement identity agree
- `featureId` matches the queried feature
- any configured selector accepts the record
- any configured current-session check accepts the advertiser session

Selectors, labels, hints, and capacity fields are policy hints, not authority.
Duplicate or overlapping advertisements are allowed. Consumer policy chooses
which candidate to try.

### Beacon Runtime View

The Beacon runtime owns a parsed, indexed current-state view. Feature
queries seed from the feature index; arbitrary selectors run outside the view
lock. Malformed or identity-inconsistent observations remain recorded as
invalid state and never become candidates.

`Beacon.watch()` always yields one `BeaconWatchSnapshot` first. It contains the
view `version`, `current` flag, and the complete filtered candidate tuple. Later
`BeaconWatchChange` values contain the same complete membership plus at most
256 changed keys. `resnapshot_required` means the changed-key detail overflowed;
the membership on that item is still the authoritative current result. There is
no replay option and no event-enum delivery model. Reconnect rebuilds the view
atomically, reports stale/current transitions, and includes disappeared keys in
the resulting diff. `BeaconDirectory.watch_records()` preserves its existing
domain values while using this current-first source; it does not poll for
recovery.

## Concord

A Concord contract is a temporary runtime agreement between named participants.
It is valid only while the contract is open and every named participant
maintains an acceptable live token for the same contract id, generation,
participant, session, and terms hash. Any named participant may cancel the
contract. Cancellation is terminal.

Concord contracts use schema `dev.deckr.concord.contract.v1`. Concord
participant tokens use schema `dev.deckr.concord.participant-token.v1`. The
NATS-backed stores and key shapes are specified in
[`nats-bus.md`](nats-bus.md#concord-stores).

A contract record must have:

- `contractId` as a non-empty string with no leading or trailing whitespace
- `generation > 0`
- a non-empty, unique, lexicographically sorted `participants` list
- a unique, lexicographically sorted `attachedParticipants` list whose entries
  are a subset of `participants`
- optional `participantSessions`, whose keys are named participants and whose
  values are non-empty endpoint-session ids
- `state` as `open` or `cancelled`
- optional `profile`, `terms`, `termsHash`, diagnostics, and `supersedes`

If `terms` is present, `termsHash` must be present and must equal the canonical
JSON hash of `terms`. If both `profile` and `terms.profile` are present, they
must match. `supersedes` is diagnostic and does not affect validity.
`participantSessions` is immutable addressing for this proposal, not a
persistent service identity. The proposal owner derives its own entry from the
supplied endpoint. Managed consumers include the selected peer session, and
managed service and hardware proposals include every known participant session.
A participant whose current session differs must reject and best-effort cancel
the contract before creating a token. Later Beacon changes never rewrite or
invalidate an accepted contract's frozen session fence.

A participant token record must have:

- `contractId`, `sessionId`, and `tokenId` as non-empty strings with no leading
  or trailing whitespace
- `generation > 0`
- a valid `participant` endpoint address
- `refreshSeq > 0`
- `ttlSeconds > 0`
- optional `termsHash`, `contractHash`, and `observed`

Create a candidate contract with `create(contractKey, contractRecord)` and an
empty `attachedParticipants` list. Creating a contract does not make it valid.
A participant attaches by exact-reading the contract, confirming it is open and
names that participant, creating its own participant-token key, then adding
itself to `attachedParticipants` with a revision-guarded contract update. A
participant must not write another participant's token.

The participant generates and retains the token identity before the token
create. Token creation followed by a failed contract update is an explicit
partial-attach state, not authority. The lifecycle owner makes at most
eight contract compare-and-set attempts; after the first conflict it retries
only after the filtered Concord view advances. Exhaustion exact-withdraws the
retained token and, if withdrawal cannot complete, best-effort cancels the
contract. Store unavailability fails local authority closed while retaining the
partial handle for bounded shutdown cleanup. No state may report successful
attachment while a partial token remains.

Refresh a token by exact-reading the contract and token, confirming both still
match the local handle, incrementing `refreshSeq`, and revision-guarded
`update(tokenKey, tokenRecord)`. Concord derives `ttlSeconds` from the token KV
bucket TTL instead of a separate participant setting; the default token bucket
TTL is 120 seconds. There is no caller-configured participant-token refresh
cadence.
Participant-token refresh writes are scheduled from the token TTL with jitter
between `ttlSeconds * 0.5` and `ttlSeconds * 0.75`; with the default 120-second
bucket TTL this produces 60-90 second refreshes. A heartbeat refreshes only its
retained exact handle. If
validation observes fresher details for the same contract, participant, session,
token id, and terms hash, the local lease may update that already-held handle
without immediately writing again. A blank local lease must not adopt a token
from KV using participant/session identity alone.

### Heartbeat Failure Is Terminal

A missed Concord heartbeat MUST terminate local authority for the affected
contract generation immediately. This is irreversible: there is no grace period,
pause, or resumption of that agreement after the failure.

A missed heartbeat is either:

- a required participant-token renewal that fails, is unavailable, times out,
  or has no confirmed success by its bounded renewal deadline; or
- failure to receive a required heartbeat from a contract or participant-token
  authority watch by that watch's finite liveness deadline.

Each renewal must have a finite deadline established before the operation and
no later than the held token's expiry. Retries must not extend that deadline.
Every authority watch must likewise have a finite liveness deadline; a connected
transport does not extend or replace that deadline.
Coalescing an unnecessary write is permitted only while confirmed fresh renewal
evidence for the same already-held token satisfies the renewal requirement; it
must never conceal or excuse a missed required heartbeat.

On heartbeat failure, the runtime MUST immediately stop protected operations
under the affected agreement, stop renewing its token for that generation, and
close local authority. It MUST attempt best-effort contract cancellation and
withdrawal of its owned token. Remote cleanup failure does not delay local
termination. Token TTL expiry is the fallback by which peers lose authority
when cancellation or withdrawal cannot reach the store; it is not permission
for the participant that detected failure to continue using the agreement.

Loss of an attached participant token, contract cancellation, or a change to
the held token's owner, session, token id, or terms hash is likewise terminal.
Loss of currentness in either authority source MUST terminate local authority
for every agreement that depends on that source, even if exact reads remain
available. A source must report missed watch heartbeats and consumer recovery
as loss of currentness; internal transport recovery must not hide that loss.

Restored connectivity, successful later reads, renewed watch delivery, or tokens
still present in the store MUST NOT revive terminated local authority. Recovery
requires a fresh opaque contract id and generation 1. The runtime may continue
reconnecting and rebuilding its views to negotiate new agreements.

These termination rules concern Concord authority. A missed Beacon heartbeat
or an expired advertisement affects discovery only and MUST NOT terminate an
existing Concord agreement.

### Withdrawal And Successor Agreements

When a participant lease or owner-side agreement closes cleanly, it should
best-effort withdraw its owned token by exact-reading the token, confirming
contract id, generation, participant, session, token id, and terms hash still
match the local handle, and then issuing a revision-guarded delete. Cleanup
failure or changed token ownership must not delete another participant's token.

Once a participant has successfully attached a token for a contract generation,
loss of that token means loss of authority for that generation. The participant
must not silently recreate a token for the same contract generation. It should
cancel when possible and negotiate a successor with a fresh opaque contract id
and generation 1. Contract ids are never reused; `supersedes` remains diagnostic.
If a participant knows it has lost local lease state for a contract it previously
owned while its same-session token still exists in KV, it must treat that contract
as stale local authority: best-effort cancel it when possible and negotiate a
successor rather than adopting the token. An empty local registry alone does not
establish that such a loss occurred.

Before a local endpoint creates another proposal, its owner-bootstrap gate MUST
distinguish abandoned local ownership from contracts maintained by another live
session. Absence from the local ownership registry, a different session id, or
missing Beacon advertisements MUST NOT by themselves justify cancellation.

Bootstrap cleanup may cancel contracts positively known to have lost local
authority in this runtime, or contracts whose exact Concord state establishes
terminal authority loss under the rules above. Another session's acceptable
participant token remains evidence of live participation even while the contract
is pending. A missing token for a participant that has never attached, or an
unavailable store, is not evidence that its process has failed. Pending, partial,
attached, same-session, and previous-session records all require this distinction.

Service admission, capacity, and deliberate replacement remain profile policy.
Bootstrap MUST NOT implicitly preempt a live session. Where a profile requires
exclusive ownership, unresolved contention MUST block the contender's proposal
commit, readiness, and Beacon publication. Any deliberate replacement MUST follow
explicit profile policy and prevent the displaced session from cancelling the
successor merely because it uses the same endpoint address. The contention
mechanism is an implementation choice; random backoff may assist retries but
does not itself establish authority or abandonment.

After terminal authority loss is established, a waiting process may perform the
required cleanup and negotiate fresh agreements. A previously displaced process
may likewise compete again when the role becomes available; it MUST NOT resume
its old authority. Proposal commit, provider readiness, hardware-manager readiness,
and Beacon publication remain blocked until required bootstrap cleanup is exactly
confirmed. Bootstrap never adopts an old token or contract.

A missing token for a participant that is not yet in `attachedParticipants`
means the contract is not yet fulfilled. A missing token for a participant that
is already in `attachedParticipants` means authority was lost and the contract is
invalid for that generation.

Implementations validate a contract by exact-reading the contract and every
named participant token. The result is valid only if:

- the contract exists and is `open`
- every named participant is in `attachedParticipants`
- every named participant token exists
- every token names the same `contractId` and `generation`
- every token belongs to the participant whose key is being checked
- every token has the expected `termsHash` when the contract has one
- every token session matches any configured current-session evidence

If no external current-session evidence is available, token existence and
internal consistency are the session evidence. A missing, invalid, stale, or
generation-mismatched token means the contract is not valid. A cancelled
contract is never resumed; recovery uses a successor contract.

Malformed external state always fails closed. A stored contract or token that
cannot be parsed is invalid authority; it must not be treated as absent,
pending, or recoverable local ownership, and a parsing exception must not
escape a lease boundary. Mutation paths normalize malformed contract and token
records to typed terminal conflicts before performing any write.

The runtime gathers immutable contract and token observations and sends
both validation paths through one pure evaluator. The evaluator performs no I/O
or mutation and checks participants in canonical order. `Concord.validate(...)`
is the normal hot path and gathers raw cached entries by their canonical exact
keys from Concord's ready, current materialized source.
`Concord.validate_exact(...)` gathers the same observations with exact KV reads
for maintenance, recovery, diagnostics, and code that must bypass the cache.
Given the same observations, both paths return the same status, parsed contract,
identity-consistent token mapping, and typed reason code. They do not depend on
an index that could discard a malformed or identity-mismatched record before it
is evaluated. The token mapping retains every successfully parsed, canonical,
identity-consistent named token even if a later participant determines the
terminal result. Supplied current-session assertions are copied, normalized,
sorted, and frozen before evaluation.

A started runtime whose contract or token materialized source is stale returns
`ContractValidityStatus.UNAVAILABLE` with
`ContractValidityReason.SOURCE_UNAVAILABLE` on the cached path and MUST terminate
local authority for affected agreements as specified in
[Heartbeat Failure Is Terminal](#heartbeat-failure-is-terminal). `UNAVAILABLE`
describes the observation failure; it does not permit an existing agreement to
pause and resume. Exact validation remains independently usable for diagnostics,
cleanup, and new negotiations when the exact store is available, but MUST NOT
restore terminated local authority. Maintenance store readiness or contents are
not part of contract validity.

### Concord Runtime View

The Concord runtime owns one parsed, indexed view of contract records,
participant tokens, invalid observations, and cached validity. Filters seed from
the smallest applicable profile, participant, contract-state, contract-id, or
pointer index. Cached validation gathers only the selected contract and its
named participant-token observations; unrelated contracts are not parsed or
validated for a filtered watch.

`Concord.watch()` always yields one `ConcordWatchSnapshot` first. Each snapshot
contains its `version`, `current` flag, and the complete filtered tuple of
`ConcordContractState` records. Later `ConcordWatchChange` values also carry at
most 256 changed contract pointers and `resnapshot_required`. The complete
membership remains authoritative when pointer detail overflows. Contract and
token reconnect rebuilds are installed atomically and include missing-pointer
removals.

`Concord.propose(endpoint, spec)` and `Concord.participant(endpoint, policy)` are
synchronous factories that return async context managers. Context entry owns
bootstrap and private workers; context exit cancels and joins those workers,
performs shielded best-effort remote cleanup within five seconds, publishes a
closed state, and wakes subscribers. A proposal creates exactly one fresh opaque
contract. It never loops to create a successor; the caller negotiates a new
proposal when recovery requires one. Higher-level managed contexts own that
successor policy so ordinary clients do not implement proposal loops.

`ConcordAgreementSpec` contains agreement intent only: profile, participants,
terms, supersedes, optional static current-session assertions, and creator. The
endpoint supplies local identity and session. `ConcordParticipantPolicy` is an
immutable batch policy whose selector receives an immutable candidate snapshot
and returns typed selected pointers. Selection may be asynchronous and runs
outside runtime state locks. Unknown pointers, malformed selections, and
selector failures fail explicitly.

Both contexts expose an immutable, monotonically versioned `state`, an atomic
`wait_for_state(predicate, timeout_seconds=None)`, and current-first
`watch_state()`. Lifecycle phases are `starting`, `ready`, `unavailable`,
`terminal`, `closing`, and `closed`. Agreement state contains currentness,
validity, the contract handle, and local-authority status. Participant state
contains currentness and the complete immutable managed-contract membership,
including each contract's validity and local-authority status. A timed-out wait
raises `TimeoutError`; closure before a match raises `ClosedResourceError`.

The participant context uses one filtered Concord-view subscription, a
coalesced selector worker, and pointer-scoped serialized workers. Startup,
filtered view changes, reconnect, overflow resnapshots, and explicit policy
wakeups are its only reconciliation inputs; there is no periodic repair loop.
A blocked selector, exact-store operation, or subscriber cannot block unrelated
pointer workers or bounded shutdown. These views are convergence machinery only.
Concord contract creation, cancellation, and participant-token validity remain
the sole lifecycle authority.

`ContractValidity.reason_code` is the behavioral discriminator. Its values are:

- source and contract observations: `SOURCE_UNAVAILABLE`, `CONTRACT_MISSING`,
  `CONTRACT_MALFORMED`, `CONTRACT_KEY_MISMATCH`,
  `CONTRACT_POINTER_MISMATCH`, and `CONTRACT_CANCELLED`
- fulfillment and token observations: `PARTICIPANT_NOT_ATTACHED`,
  `TOKEN_MISSING`, `TOKEN_MALFORMED`, `TOKEN_KEY_MISMATCH`,
  `TOKEN_CONTRACT_MISMATCH`, `TOKEN_GENERATION_MISMATCH`,
  `TOKEN_PARTICIPANT_MISMATCH`, `TOKEN_TERMS_HASH_MISMATCH`, and
  `TOKEN_SESSION_MISMATCH`
- local lease fencing: `TOKEN_LOCAL_AUTHORITY_LOST`

The status mapping is fixed:

| Status | Reason codes |
| --- | --- |
| `UNAVAILABLE` | `SOURCE_UNAVAILABLE` |
| `MISSING_CONTRACT` | `CONTRACT_MISSING` |
| `INVALID_CONTRACT` | `CONTRACT_MALFORMED`, `CONTRACT_KEY_MISMATCH`, `CONTRACT_POINTER_MISMATCH` |
| `CANCELLED` | `CONTRACT_CANCELLED` |
| `NOT_YET_FULFILLED` | `PARTICIPANT_NOT_ATTACHED` |
| `MISSING_TOKEN` | `TOKEN_MISSING` |
| `INVALID_TOKEN` | `TOKEN_MALFORMED`, `TOKEN_KEY_MISMATCH`, `TOKEN_CONTRACT_MISMATCH`, `TOKEN_PARTICIPANT_MISMATCH`, `TOKEN_LOCAL_AUTHORITY_LOST` |
| `GENERATION_MISMATCH` | `TOKEN_GENERATION_MISMATCH` |
| `TERMS_HASH_MISMATCH` | `TOKEN_TERMS_HASH_MISMATCH` |
| `SESSION_MISMATCH` | `TOKEN_SESSION_MISMATCH` |
| `VALID` | none |

`ContractValidity.reason` remains separate human-readable diagnostic text; code
must never select lifecycle behavior by comparing or parsing that text. Concord
write paths still exact-read current records, verify canonical keys and complete
record identity, and use revision-guarded updates or deletes.

Runtime renewal loops are not discovery mechanisms. Beacon advertisement
renewal and Concord participant-token renewal must only refresh already-owned
leases/tokens. They must not perform full Concord reconciliation, participant or
profile prefix discovery, broad KV scans, or successor-contract attachment.
Discovery belongs to materialized Beacon/Concord views, watch notifications,
startup/watch-reconnect cache rebuilds, and explicit policy wakeups.
Running full discovery on a fast renewal cadence, including the shared
five-second state-renewal cadence, is forbidden.

### Contract Pointers On Lane Messages

Any lane message whose legitimacy depends on an existing Concord agreement must
carry the authorizing contract instance in the `DeckrMessage` envelope:

```json
{
  "contract": {
    "contractId": "opaque-contract-id",
    "generation": 1
  }
}
```

The pointer identifies the live Concord contract generation under which the
message claims authority. Receivers must validate that exact contract id and
generation against Concord participant-token validity and the sender/receiver
endpoint sessions required by the profile. Receivers must not authorize
protected messages by scanning for any matching live contract, by trusting a
lane-local route id, or by treating Beacon, endpoint presence, current-state
records, catalogs, or service-use ids as substitute authority.

Lane-local identifiers still matter, but they answer different questions:

- `contract.contractId` plus `contract.generation`: which live Concord
  agreement authorizes this message
- `senderSessionId` and `recipientSessionId`: which runtime endpoint sessions
  sent and receive the message, checked against Concord participant tokens
- device refs, action instance ids, binding ids, page session ids, service view
  refs, route ids, and output generations: which object, route, or stale-state
  fence is targeted within the authorized agreement

Core lane policy determines whether `contract` is required, forbidden, or not
applicable for a message type. Hardware control/input/state messages and
protected service/action commands require it. Public discovery, advertisements,
availability probes, interest updates, and lifecycle candidates do not carry
Concord authority in the lane envelope.

### Concord Maintenance

Core Concord maintenance is optional and Concord-only. Its separate capability
receives the contract, participant-token, and maintenance exact/raw stores directly;
normal `Concord` has no maintenance store, watcher, cache, readiness dependency,
or mutation route. Constructing the maintenance capability starts no task or
watch.

The lane-less component `dev.deckr.concord.reaper` owns
`ConcordReaperService.run()`. Infrastructure may instead construct the service
from `ConcordMaintenance` and call `scan_once()` explicitly. Ordinary callers
do not pass a task group to the service. The reaper is a low-frequency
maintenance scanner, not a materialized-view runtime or immediate notification
service. It never consults Beacon advertisements, endpoint presence, catalogs,
lane subscriptions, or any other parallel authority.

The reaper records `firstObservedStaleAt` in the persistent
`deckr_concord_maintenance_v1` store for each `contractId:generation`. Open
contracts count as stale only when scan-time Concord validation over the current
contract and participant-token keys reports
`missing_token`, `invalid_token`, `session_mismatch`, `terms_hash_mismatch`,
`generation_mismatch`, or `invalid_contract`. `not_yet_fulfilled` is stale only
when exact validation finds no valid refresh tokens at all; a pending contract
with any valid participant token remains pending.
`unavailable` is not a stale signal.

After the stale grace period, 900 seconds by default, maintenance cancels the
open contract without acting as a named participant. The cancellation is a
terminal Concord cancellation with `cancelReason=concord_reaper_stale_contract`
and `cancelledBy=concord:maintenance`.

Cancelled contract records are retained until `cancelledAt` plus the retention
period, 3600 seconds by default. Before deleting a cancelled record, maintenance
logs contract identity, profile, participants, attached participants, lifecycle
timestamps, cancellation metadata, supersession, terms hash, current validation
status, and participant-token summaries. Full `terms` are not logged by default.
The pre-mutation audit calls this the listed token-key count; successful delete
counts are reported separately by the cleanup result and reaper scan.
Before the revision-guarded contract delete, maintenance validates every listed
participant-token entry against the exact requested prefix and persists a
`cleanup.*` marker containing the contract generation, deleted contract
revision, and terms hash. A listed key, token identity, or marker mismatch fails
closed without deleting the contract. If the contract record changes during a
delete attempt, maintenance logs the conflict and leaves the record for a later
scan.

After the contract record is deleted, maintenance makes at most eight immediate
revision-guarded cleanup passes over that generation. The generation is complete
only when an exact rescan finds no participant tokens and the cleanup marker is
revision-guarded deleted. Conflict-budget exhaustion, malformed or
identity-inconsistent rescan state, and token-store unavailability leave the
marker durable and report cleanup pending. Later low-frequency scans enumerate
`cleanup.*` independently of contract records and resume the bounded cleanup;
participant-token TTL remains only a fail-safe.

At the end of each scan, orphaned `stale.*` maintenance observations whose
contract record no longer exists are removed with revision-guarded deletes.

## Profiles

Generic Beacon validates only the advertisement envelope. Generic Concord
validates only contract and token mechanics. Profile owners validate payload and
terms meaning.

Deckr core owns these profiles:

| Profile | Protocol | Purpose |
| --- | --- | --- |
| `dev.deckr.profile.hardware.v1` | Beacon | hardware devices, controls, capabilities |
| `dev.deckr.profile.hardware_claim.v1` | Concord | controller ownership of hardware devices |
| `dev.deckr.action_runtime.provider` | Services/Concord | action runtime service advertisement, views, and service-use leases |

For `dev.deckr.profile.hardware.v1`, the Beacon payload `sessionId` must match
the advertisement `sessionId`, `managerEndpoint` must match the advertisement
`endpoint`, and `managerId` must be the hardware-manager endpoint id.

For `dev.deckr.action_runtime.provider`, the service id must be
`action-runtime.<providerInstanceId>`, the service endpoint must be
`service:<serviceId>`, and action availability is published through the
contract-fenced `action_availability` view.

For `dev.deckr.profile.hardware_claim.v1`, the claim terms bind a controller
endpoint, hardware-manager endpoint, and one or more claimed devices. Device
ids in one claim must be unique. `instanceCount` must be greater than zero.
Beacon may discover a candidate device before a claim is created, but Beacon is
not part of claim validity. Participants validate ownership through the Concord
contract, participant tokens, endpoint, session, and device refs. Hardware
single-owner and capacity enforcement are hardware-manager/profile policy over
valid Concord claims. If a claimed device disconnects, the hardware manager
must cancel/end the Concord claim or stop maintaining its participant token; a
missing hardware Beacon advertisement alone is not a claim withdrawal.

The hardware Beacon profile is the only public device-inventory publication
surface. Hardware lane messages can carry input, commands, capability state, and
replies, but they do not announce inventory lifecycle or override Beacon and
Concord authority.

Claimed device descriptors are expected to remain stable for v1. Material
descriptor changes are represented by claim cancellation or token loss plus a
new Beacon candidate, not by hardware lane lifecycle messages.

Action provider runtimes use the `dev.deckr.action_runtime.provider` service
protocol. A controller obtains authority through that service's Concord
service-use contract, then reads the fenced `action_availability` view and sends
runtime lifecycle messages on the `services` lane. Individual control bindings
are controller-owned routing state. Once the controller and provider have
negotiated the service-use contract, Beacon is no longer part of that
contract's lifecycle or validity.

Service packages may use generic Beacon and Concord with package-owned feature
ids, advertisement payload/use profiles, and direct KV-backed service views.
Deckr core does not define service-specific domain semantics
such as Sonos zones, OpenHAB items, or package-owned view schemas.

Ordinary service consumers use the managed `deckr.services.DeckrServices`
client. That client owns service candidate discovery through Beacon, descriptor
parsing, local predicate filtering, service-use candidate races and successor
negotiation through Concord, request authorization, and fenced service-view
access. A service
protocol feature watch is per feature id, not per service id; the service id and
concrete view key prefixes come from each validated descriptor. Consumers must
not raw-scan Beacon KV, scan all Beacon candidates and parse service descriptors
ad hoc, classify Concord terminal statuses, or query Concord to discover
available services. Discovery output is only a candidate for a new service-use
negotiation.

`DeckrServices.use_matching(...)` admits at most 64 unique matching
endpoint/session candidates and races all admitted candidates, including later
arrivals while negotiation remains pending. Each attempt has a fresh opaque
contract id. The first valid agreement wins only after losing attempts have
relinquished local authority. Winner loss publishes `reconnecting` and starts a
fresh race; published commands are never replayed across contracts. Beacon
withdrawal or replacement after acceptance has no lifecycle effect on the
winner. `use(descriptor)` remains the exact-candidate operation.

Service-use Concord contract ids are opaque runtime ids. Service-use contracts
carry the service-use profile but no service-specific `terms`; `terms` and
`termsHash` are absent. New or lost service-use authority is established by
proposing a fresh Concord contract. The Concord participants must be exactly the
service endpoint and the client endpoint.

After a service-use Concord contract is negotiated, service-message and view
authority follows that already-held lease and its participant tokens, not
continued Beacon advertisement presence. Consumer-to-service service messages
must carry the exact contract pointer, name a protocol operation, and be sent
by the peer named in the contract. Protected service views are authorized within
the advertised
view-family prefixes and fenced by the advertised service identity, session,
and contract pointer. Consumers may refresh an already-held service-use lease
while it remains valid, but new or lost lease state requires current Beacon discovery
and a new opaque Concord contract. Protected view watches deliver payloads only
while the stored entry matches the watcher's service-use fence. If a visible
same-key entry is replaced by another service identity or session, the watcher
observes only a removal-style event and must not receive the replacement
payload. Delete and expire events for entries that were never visible to that
watcher are not delivered. A service may withdraw its Beacon advertisement when
it cannot accept new service-use contracts; already-held service-use contracts
remain governed only by Concord validity and participant tokens.

Service providers use `Deckr.service_provider(...)`; hardware managers use
`Deckr.hardware_manager(...)`. These contexts own owner bootstrap, participant
selection, private workers, authorization, advertisement-after-bootstrap
ordering, and bounded cleanup. Application code supplies protocol intent,
backend/device behavior, and domain policy—not token refresh, reconciliation,
contract retry, task-group, or raw-pointer plumbing.

## Contract Artifacts And Conformance

`contract/v1` contains:

- JSON Schemas for Beacon, Concord, Deckr profiles, and lane messages
- valid and invalid fixtures
- key-token vectors
- Beacon and Concord key vectors
- canonical Concord terms-hash vectors
- NATS lane subject/header vectors

Every implementation should use those artifacts as acceptance inputs.
Passing schema validation alone is not sufficient; implementations must also
enforce this guide's semantic rules. In particular, verify endpoint parsing,
key-token encoding, Beacon/Concord key generation, terms hashing, positive
counters and TTLs, contract participant canonicalization, and profile identity
cross-checks.
