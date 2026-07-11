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
- Python models in `deckr.beacon`, `deckr.concord`,
  `deckr.hardware.profiles`, and `deckr.action_runtime`: current reference
  implementation and source for generated artifacts

The JSON Schemas are structural wire contracts. They intentionally do not carry
every semantic rule enforced by the Python models. Non-Python implementations
must also enforce the semantic rules in this guide, including endpoint parsing,
positive counters and TTLs, canonical participant ordering, terms-hash
validation, and profile identity checks.

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
Runtimes own advertisement leases and treat `refreshInterval` as a requested
cadence. Beacon derives `ttlSeconds` from the Beacon KV bucket TTL instead of
per-advertisement configuration; the default Beacon bucket TTL is 300 seconds.
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

The Python `Beacon` facade owns a parsed, indexed current-state view. Feature
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
- `state` as `open` or `cancelled`
- optional `profile`, `terms`, `termsHash`, diagnostics, and `supersedes`

If `terms` is present, `termsHash` must be present and must equal the canonical
JSON hash of `terms`. If both `profile` and `terms.profile` are present, they
must match. `supersedes` is diagnostic and does not affect validity.

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

Refresh a token by exact-reading the contract and token, confirming both still
match the local handle, incrementing `refreshSeq`, and revision-guarded
`update(tokenKey, tokenRecord)`. Concord derives `ttlSeconds` from the token KV
bucket TTL instead of a separate participant setting; the default token bucket
TTL is 120 seconds. Lease implementations treat configured refresh intervals as
requested cadence, not guaranteed write cadence.
Participant-token refresh writes are scheduled from the token TTL with jitter
between `ttlSeconds * 0.5` and `ttlSeconds * 0.75`; with the default 120-second
bucket TTL this produces 60-90 second refreshes. Leases may validate an
already-held token handle more often, but they should not write a token refresh
until a token must be attached or the effective refresh interval is due. If
validation observes fresher details for the same contract, participant, session,
token id, and terms hash, the local lease may update that already-held handle
without immediately writing again. A blank local lease must not adopt a token
from KV using participant/session identity alone.

If the contract is cancelled, the token is missing, or the token has changed
owner/session/token id/terms hash, the participant no longer maintains authority
for that contract generation. In Python, token refresh unavailability is treated
as loss of authority: the local lease closes immediately, attempts best-effort
contract cancellation, and relies on token TTL expiry if the same NATS/KV failure
prevents writing the cancellation.

When a participant lease or owner-side agreement closes cleanly, it should
best-effort withdraw its owned token by exact-reading the token, confirming
contract id, generation, participant, session, token id, and terms hash still
match the local handle, and then issuing a revision-guarded delete. Cleanup
failure or changed token ownership must not delete another participant's token.

Once a participant has successfully attached a token for a contract generation,
loss of that token means loss of authority for that generation. The participant
must not silently recreate a token for the same contract generation. It should
cancel when possible or negotiate a successor contract using a new generation or
new contract id.
If local lease state is lost while a same-session token still exists in KV, the
participant must treat the old contract as stale local authority: best-effort
cancel it when possible and negotiate a successor rather than adopting the token.

A missing token for a participant that is not yet in `attachedParticipants`
means the contract is not yet fulfilled. A missing token for a participant that
is already in `attachedParticipants` means authority was lost and the contract is
invalid for that generation.

Language-neutral implementations validate a contract by exact-reading the
contract and every named participant token. The result is valid only if:

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

The Python runtime gathers immutable contract and token observations and sends
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
`ContractValidityReason.SOURCE_UNAVAILABLE` on the cached path. Exact validation
remains independently usable when the exact store is available. Maintenance
store readiness or contents are not part of contract validity.

### Concord Runtime View

The Python `Concord` facade owns one parsed, indexed view of contract records,
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

`ConcordParticipant.watch()` follows the same current-first contract with
`ConcordParticipantSnapshot` and `ConcordParticipantChange`. It exposes the full
managed-contract membership and changed pointers. The participant manager uses
one filtered Concord-view subscription per authority bucket pair and reconciles
only affected pointers during ordinary operation; startup, reconnect, and
overflow take a full resnapshot. This view is observation and convergence
machinery only. Concord contract creation, cancellation, and participant-token
validity remain the sole lifecycle authority.

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
startup/watch-reconnect cache rebuilds, and explicit low-frequency repair.
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

Core Concord maintenance is optional and Concord-only. Python exposes it from
the separate `deckr.concord_maintenance` facade. `ConcordMaintenance` receives
the contract, participant-token, and maintenance exact/raw stores directly;
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
parsing, local predicate/selector resolution, service-use negotiation through
Concord, request contract pointers, and fenced service-view access. A service
protocol feature watch is per feature id, not per service id; the service id and
concrete view key prefixes come from each validated descriptor. Consumers must
not raw-scan Beacon KV, scan all Beacon candidates and parse service descriptors
ad hoc, classify Concord terminal statuses, or query Concord to discover
available services. Discovery output is only a candidate for a new service-use
negotiation.

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

## Contract Artifacts And Conformance

`contract/v1` contains:

- JSON Schemas for Beacon, Concord, Deckr profiles, and lane messages
- valid and invalid fixtures
- key-token vectors
- Beacon and Concord key vectors
- canonical Concord terms-hash vectors
- NATS lane subject/header vectors

Non-Python implementations should use those artifacts as acceptance inputs.
Passing schema validation alone is not sufficient; implementations must also
enforce this guide's semantic rules. In particular, verify endpoint parsing,
key-token encoding, Beacon/Concord key generation, terms hashing, positive
counters and TTLs, contract participant canonicalization, and profile identity
cross-checks.
