# Beacon v1 specification in ASD-STE100

## 1 Purpose and scope

Beacon finds endpoints that advertise a feature. It uses temporary records called
advertisements. A consumer can use an advertisement to select a candidate endpoint.

This specification gives the requirements for Beacon v1. It includes the
advertisement format, the store, the advertisement lifecycle, candidate selection,
and the runtime view. It also includes the Beacon hardware profile.

The requirements apply to all implementations, in all programming languages.
An implementation API or source model does not change these requirements.
This document defines Beacon protocol requirements. The [Concord specification](concord.md)
defines agreement requirements. Section 10 gives the related references.

The word **must** identifies a requirement. **Must not** identifies a prohibition.
The word **can** identifies a possibility or permission. Text marked
**Recommendation** identifies a recommended action, not a requirement.

## 2 Technical terms

The table gives the meanings of technical nouns in this specification. Field
names, API names, schema identifiers, and code values keep their exact spelling.

Shared terms have the same meanings in the [Concord specification](concord.md#2-technical-terms).
In Beacon sections, “candidate” means “Beacon candidate” and “selector” means
“Beacon selector”. “Session” means “endpoint session”.

| Term | Meaning |
| --- | --- |
| Advertisement | A temporary record that identifies a feature and an endpoint. |
| Advertisement heartbeat | A scheduled refresh that keeps a Beacon advertisement in the store. |
| Advertiser | The endpoint that owns an advertisement. |
| Authority | Permission to do a protected operation through a Concord agreement. |
| Beacon candidate | An advertisement that agrees with the selection conditions in section 7. |
| Beacon selector | A function that accepts or rejects one advertisement record. |
| Compare and set | A mutation that succeeds only if the stored revision is the expected revision. |
| Consumer | A component that uses Beacon to find a candidate. |
| Currentness | The source state that shows if its observations are up to date. |
| Endpoint | An addressable entity in Deckr. |
| Endpoint address | The endpoint family and endpoint ID serialized as `<endpoint-family>:<endpoint-id>`. |
| Endpoint session | One runtime instance of an endpoint. |
| Envelope | The advertisement fields that generic Beacon validates. |
| Exact read | A read of a specified key directly from the store. This read does not use a materialized source or view. |
| Feature | A function that an endpoint advertises. |
| Heartbeat | A scheduled record refresh or watch signal. |
| Key token | An encoded value that forms one part of a store key. |
| KV bucket | A NATS JetStream store that holds values at keys. KV means key-value. |
| Materialized source | A local cache of store observations and revisions, maintained through snapshots and watch events. |
| Materialized view | A parsed, indexed representation of observations from one or more materialized sources. |
| Owner check | A check that compares record ownership with the identity that the local advertiser holds. |
| Participant | An endpoint that a Concord contract names in `participants`. |
| Profile | A specification that gives the meaning of protocol data and the application rules for its use. |
| Revision | A store version that identifies a write to a key. |
| Revision guard | A condition that permits an update or delete only if the stored revision is the expected revision. |
| Session ID | The identifier of one endpoint session. |
| TTL | Time to live. The time interval after the last write before the store expires a record. |
| Watch | A source of state snapshots and change notifications. |
| Watch heartbeat | A liveness signal from a store watch. |

Computer operations use technical verbs such as **encode**, **parse**,
**publish**, **refresh**, **validate**, and **withdraw**. To publish is to write an
advertisement to the store. To refresh an advertisement is to extend its life
in the store. To withdraw an advertisement is to remove it from the store after
the necessary owner check.

## 3 Discovery boundary

Beacon gives discovery information. It does not prove any of these conditions:

- The endpoint is live or healthy.
- The consumer has authorization to use the endpoint.
- A resource is available or reserved.
- An endpoint owns a resource.
- A future operation will succeed.
- A command is safe.

Consumers must use Beacon results as candidates. Before a consumer uses live
authority, it must make any necessary Concord agreement.

An advertiser can withdraw advertisements when it does not accept new
negotiations. It can also stop advertisement refreshes. These actions have an
effect on future discovery and negotiation only.

A missing or changed advertisement must not, by itself, invalidate an existing
Concord agreement. It must not cancel, withdraw, pause, or degrade the agreement.
This rule also applies to advertisement withdrawal, expiry, replacement, missed
advertisement heartbeats, and missed Beacon watch heartbeats. These conditions
must not, by themselves, stop live rendering, routing, commands, or view consumption.

The advertisement owner and the store TTL control the advertisement lifecycle.
Concord maintenance does not remove Beacon advertisements. Concord has its
own rules for live authority and agreement withdrawal.

## 4 Advertisement format

### 4.1 Schema and required fields

Beacon uses the JSON Schema `dev.deckr.beacon.advertisement.v1`.
The envelope must agree with the schema and the semantic rules in this
specification. Schema validation alone is not sufficient.

The record must contain these fields:

| Field | JSON type | Requirement |
| --- | --- | --- |
| `advertisementId` | String | The value must not be empty. It must not have whitespace at the start or end. |
| `featureId` | String | The value must not be empty. It must not have whitespace at the start or end. |
| `advertiser` | String | The value must be a valid endpoint address. |
| `endpoint` | String | The value must be a valid endpoint address. |
| `sessionId` | String | The value identifies the advertiser's endpoint session. It must not be empty. It must not have whitespace at the start or end. |
| `refreshSeq` | Integer | The value must be more than zero. |
| `ttlSeconds` | Integer | The value must be more than zero. Section 6.4 gives its source. |

### 4.2 Optional fields

The record can also contain these fields:

| Field | Value |
| --- | --- |
| `schema` | The string `dev.deckr.beacon.advertisement.v1`. This is also the schema default. |
| `protocol` | An object with the string fields `namespace` and `version`, or `null`. |
| `operations` | An array of strings. |
| `labels` | An object with string values. |
| `hints` | An object with JSON values. |
| `payload` | An object with JSON values, or `null`. |
| `createdAt` | A string with JSON Schema format `date-time`, or `null`. |
| `updatedAt` | A string with JSON Schema format `date-time`, or `null`. |

The schema does not let the record contain other envelope fields. If `protocol`
is an object, it must contain `namespace` and `version`. The `protocol` object
must not contain other fields.

Generic Beacon validates the envelope only. The profile owner validates the
meaning of the payload. Section 9 gives the Beacon hardware profile requirements.

### 4.3 Endpoint addresses

An endpoint address has this format:

```text
<endpoint-family>:<endpoint-id>
```

The core endpoint families are `controller`, `hardware_manager`,
`action_provider`, and `service`.

An endpoint address must not have whitespace at the start or end. Its endpoint
ID must not be empty. The endpoint ID must not contain `:`.

An `action_provider` endpoint ID must match `[A-Za-z0-9][A-Za-z0-9._-]*`.
It must not use a reserved provider identity, such as
`dev.deckr.controller.builtin`.

## 5 Advertisement store

### 5.1 Bucket and key

The NATS store uses this bucket and key format:

```text
bucket: deckr_beacon_advertisement_v1
key:    advertisements.by_feature.<feature-id-token>.<advertisement-id-token>
```

The two key tokens represent `featureId` and `advertisementId`.
The two tokens must agree with the related fields in the record.

Compare the full value with `[A-Za-z0-9][A-Za-z0-9_-]*`.
If the full value matches and does not start with `b64_`, use it without a change.

For all other values, use this procedure:

1. Encode the value as UTF-8 bytes.
2. Encode those bytes as URL-safe base64 without padding.
3. Add `b64_` at the start of the encoded value.

These examples come from the acceptance vectors:

| Initial value | Key token |
| --- | --- |
| `deck_1` | `deck_1` |
| `b64_native` | `b64_YjY0X25hdGl2ZQ` |
| `deck:one` | `b64_ZGVjazpvbmU` |
| `dev.deckr.hardware` | `b64_ZGV2LmRlY2tyLmhhcmR3YXJl` |

### 5.2 Store access

The managed runtime opens Beacon as `deckr.beacon`.

**Recommendation:** Use this managed runtime. Do not manually construct a Beacon
store.

Core bucket names are reserved. Generic `Deckr.kv_bucket(...)` and
`ComponentContext.kv_bucket(...)` access rejects Beacon bucket policies.
Private buckets must not replace the specified Beacon discovery store.

Use compare-and-set operations when the store supports them. Use `create` for
initial publication. Use a revision guard with `update` for a refresh. Use a
revision guard with `delete` for withdrawal.

A watch notification signals a state change. For strict protocol validation,
use exact reads and calculate validity again.

### 5.3 TTL policy

The broker controls the bucket TTL. The default Beacon bucket TTL is 300 seconds.
The bucket must obey the [shared KV storage requirements](nats.md#7-shared-kv-storage-requirements).
Those requirements define policy comparison, retained messages, delete markers,
and removal of expired keys from the source and view.

## 6 Advertisement lifecycle

### 6.1 Create an advertisement

The runtime owns the advertisement context and the advertisement heartbeat
schedule. It also owns advertisement lifecycle logging.
`Beacon.advertise(endpoint, spec)` is a synchronous factory for an asynchronous
context manager. The endpoint supplies the advertiser identity and the current
session. The `spec` argument supplies only the advertisement intent.

Use this procedure for initial publication:

1. Select a unique advertisement ID.
2. Make the record with the required fields.
3. Make the key from `featureId` and `advertisementId`.
4. Create the record with `create`.

**Recommendation:** During startup or replacement, try to remove stale
advertisements for the same feature, advertiser, and endpoint. Use revision guards
for these deletions. This cleanup has an effect on future discovery only. It does
not withdraw a Concord agreement.

### 6.2 Refresh an advertisement

Use this procedure for a refresh write:

1. Do an exact read of the advertisement key.
2. Do an owner check on the record.
3. Increase `refreshSeq` by one.
4. Write the record with `update` and a revision guard.

The owner check compares `advertisementId`, `featureId`, `advertiser`, `endpoint`,
and `sessionId` with the locally held identity. If ownership does not match, do
not change the record.

### 6.3 Withdraw an advertisement

Use this procedure for withdrawal:

1. Do an exact read of the advertisement key.
2. Do the owner check from section 6.2.
3. Delete the record with `delete` and a revision guard.

If ownership does not match, do not delete the record. Exit from the advertisement
context controls withdrawal and worker cleanup with bounded limits.
Closure of an endpoint session alone does not withdraw its Beacon advertisements.

### 6.4 Schedule refreshes

Beacon gets `ttlSeconds` and the refresh interval from the observed bucket TTL.
The caller does not configure either value.

The managed runtime schedules advertisement heartbeat writes with jitter in
this interval:

```text
minimum interval = ttlSeconds * 0.5
maximum interval = ttlSeconds * 0.75
```

For the default TTL of 300 seconds, the interval is 150 to 225 seconds.
The runtime publishes real changes to `payload`, `labels`, `hints`, `protocol`,
or `operations` immediately. An unchanged advertisement heartbeat write is not
necessary while the stored value is sufficiently fresh.

Refresh loops must not become discovery loops. Usual discovery uses the
materialized view and its watches. Runtime code must not do full discovery or
participant or profile scans at the advertisement refresh interval. It must not
do full claim or provider reconciliation at this interval.
Renewal loops must refresh only advertisements that the runtime already owns.
They must not attach to successor contracts. Full discovery at the shared
five-second state-renewal interval is also prohibited.

Exact full scans are for startup, watch recovery, diagnostics, maintenance, and
repair at a low frequency. They are not part of usual feature queries.

### 6.5 Expiry and recovery

If the advertiser stops refreshes, the store removes the record when its TTL
expires. This removal has an effect on discovery only.

If a managed advertiser still operates, it must recover when its own
advertisement key is missing. It must publish the advertisement again with its own opaque
advertisement identity. It must not continually try to refresh the missing key
without recovery.

Advertisement expiry does not stop broker reconnection. After reconnection,
the materialized source must rebuild before it reports current state.

## 7 Candidate selection

A consumer can use a candidate only when all these conditions are true:

1. The record is in the store.
2. The record agrees with the envelope schema and the semantic rules.
3. The key agrees with the advertisement identity.
4. `featureId` is the same as the requested feature ID.
5. Each configured selector accepts the record.
6. Each configured current-session check accepts the advertiser session.

Selectors, labels, hints, and capacity fields supply policy hints. They do not
give authority. Advertisers can publish duplicate or overlapping advertisements.
The consumer policy selects the candidate to try.

Endpoint presence and retired coordination stores must not replace Beacon
discovery records. Action catalogs, hardware inventory, and unilateral device
claims must not replace those records either.

## 8 Runtime view and watches

### 8.1 Current state

The Beacon runtime owns one materialized view of advertisement records.
The view uses one materialized source for store observations and revisions.
The view contains parsed records and a feature index. Feature queries start
with this index. The runtime does not hold the view lock when it executes
arbitrary selectors.

The view keeps malformed observations as invalid state. It also keeps
observations with inconsistent identities as invalid state. These observations
must not become candidates.

`candidates(...)`, `get(...)`, and feature watches use the materialized view.
They do not do a full key scan for each query. Production runtime consumers
use the managed Beacon view. They do not subscribe directly to its raw store.

### 8.2 Watch results

`Beacon.watch()` always supplies one `BeaconWatchSnapshot` first.
This snapshot contains the view `version`, the `current` flag, and the full
filtered candidate tuple. The watch must obey the
[public state watch requirements](nats.md#9-public-state-watches).
Those requirements define atomic registration, change limits, overflow,
slow subscribers, and registration limits.

Subsequent results use `BeaconWatchChange`. Each result contains the full filtered
membership and bounded changed keys. The full membership is the current
result for that item. This applies also when the change detail overflows.

If `resnapshot_required` is true, use the full membership on that
item. Do not try to reconstruct membership from a partial set of changed keys.

The watch has no replay option. It does not supply an event enumeration.

### 8.3 View recovery

After reconnection, the runtime rebuilds the view as one atomic change.
It reports transitions between stale and current state. The resulting difference
includes keys that disappeared during the outage.

The source must obey the [shared recovery requirements](nats.md#8-materialized-sources-and-recovery).
Those requirements define snapshot barriers, revision ordering, tombstone limits,
currentness, and observation of exact writes. Loss of Beacon currentness affects
discovery only, as specified in section 3.

`BeaconDirectory.watch_records()` uses this source and keeps its existing
domain values. It supplies current state first. It does not poll for recovery.

## 9 Beacon hardware profile

Deckr core owns the Beacon profile `dev.deckr.profile.hardware.v1`.
This profile gives information about hardware devices, controls, and capabilities.
Its payload must agree with these identity conditions:

| Payload field | Required relationship |
| --- | --- |
| `sessionId` | The value must be the same as the advertisement `sessionId`. |
| `managerEndpoint` | The value must be the same as the advertisement `endpoint`. |
| `managerId` | The value must be the same as the hardware-manager endpoint ID. |

The hardware Beacon profile is the only public source for device inventory.
The hardware lane is named `hardware_messages`.
Hardware lane messages do not announce inventory lifecycle changes.
Hardware lane messages do not replace the device inventory published through
Beacon.
They can carry input, commands, capability state, and replies. These messages
must not replace Beacon discovery or Concord authority.

Beacon can identify a device before an endpoint makes a claim. Beacon does not
give device ownership. A missing hardware advertisement alone does not withdraw
an existing device claim. The [Concord specification](concord.md#161-hardware-claims)
gives the claim requirements.

## 10 Conformance and references

All implementations must obey the schema rules and the semantic rules.
An implementation must validate endpoint addresses, positive counters, positive
TTLs, key identity, and applicable profile identities.

**Recommendation:** Use the existing Beacon acceptance artifacts to test
implementations. Include key-token encoding and advertisement key construction.
The artifact locations are below.

| Reference | Beacon content |
| --- | --- |
| [Discovery boundary](#3-discovery-boundary) | The effect of Beacon changes on discovery and existing agreements. |
| [Endpoint addresses](#43-endpoint-addresses) | Endpoint identity requirements. |
| [Advertisement store](#5-advertisement-store) | Key tokens and store operations. |
| [Advertisement lifecycle](#6-advertisement-lifecycle) | Publication, refresh, withdrawal, and recovery. |
| [Concord specification](concord.md) | Agreement authority, participant tokens, and recovery. |
| [Managed service consumers](concord.md#165-managed-service-consumers) | Service discovery, candidate selection, and agreement negotiation. |
| [NATS substrate specification](nats.md) | Transport, shared KV policy, state watches, and source recovery. |
| [Advertisement schema](../contract/v1/schemas/beacon/advertisement.v1.schema.json) | The full envelope structure. |
| [Hardware profile schema](../contract/v1/schemas/profiles/hardware.v1.schema.json) | The Beacon hardware payload structure. |
| [Valid Beacon fixtures](../contract/v1/fixtures/valid/beacon/) | Valid advertisement examples. |
| [Invalid Beacon fixtures](../contract/v1/fixtures/invalid/beacon/) | Invalid advertisement examples. |
| [Key-token vectors](../contract/v1/vectors/key-tokens.v1.json) | Expected token encodings. |
| [Advertisement key vector](../contract/v1/vectors/beacon-concord-keys.v1.json) | The `beacon_advertisement_key` case only. |
| [Runtime reference](protocol-runtime-reference.md#33-publish-a-beacon-advertisement) | Beacon API examples. These examples do not change protocol requirements. |

If the specification, artifacts, and implementation disagree, identify the
difference as a contract bug. Correct each affected item so they agree.

Language reference:
[ASD-STE100 Simplified Technical English, Issue 9](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf).
