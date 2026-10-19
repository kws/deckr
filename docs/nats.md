# NATS substrate v1 specification

## 1 Purpose and scope

The NATS substrate carries Deckr messages and stores shared state. Core NATS
carries lane messages. NATS JetStream KV stores records for Beacon, Concord,
and package-owned state.

This specification gives the requirements for message transport, broker
connections, shared storage, state watches, and broker resource cleanup.
The requirements apply to all implementations, in all programming languages.
An implementation API or source model does not change these requirements.

The [Beacon specification](beacon.md) defines discovery.
The [Concord specification](concord.md) defines agreement authority.
Those specifications own their generic record formats, TTL policy and
lifecycles. This binding assigns the Deckr store names. Application profiles are
specified in [Deckr profiles](deckr-profiles.md); schema consumption is described
in [schema guidance](schemas.md).

The word **must** identifies a requirement. **Must not** identifies a prohibition.
The word **can** identifies a possibility or permission. Text marked
**Recommendation** identifies a recommended action, not a requirement.

## 2 Technical terms

The table gives the meanings of technical nouns in this specification.
Field names, schema identifiers, and protocol values keep their exact spelling.
Shared terms have the same meanings in the Beacon and Concord specifications.
“Session” means “endpoint session”.

| Term | Meaning |
| --- | --- |
| Acceptance predicate | A function that accepts or rejects a reply. |
| Authority | Permission to do a protected operation through a Concord agreement. |
| Barrier | A watch item that identifies the revision boundary for a recovered snapshot. |
| Broker | The NATS server that carries messages and supplies JetStream storage. |
| Contract pointer | The pair `contractId` and `generation` that identifies one Concord contract instance. |
| Currentness | The source state that shows if its observations are up to date. |
| Delete marker | A broker record that tells a watch that a key was deleted or expired. |
| Endpoint | An addressable entity in Deckr. |
| Endpoint address | The endpoint family and endpoint ID serialized as `<endpoint-family>:<endpoint-id>`. |
| Endpoint session | One runtime instance of an endpoint. |
| Envelope | The canonical `DeckrMessage` fields, including its body. |
| Exact read | A read of a specified key directly from the store. This read does not use a materialized source or view. |
| High-water revision | The highest stream revision observed before a broker watch opens. |
| JetStream consumer | A broker resource that supplies stream observations to a client. |
| Key token | An encoded value that forms one part of a store key or NATS subject. |
| KV bucket | A NATS JetStream store that holds values at keys. KV means key-value. |
| Lane | A named channel for Deckr messages. |
| Lane contract | Rules for the message types, endpoint families, and recipients that a lane permits. |
| Materialized source | A local cache of store observations and revisions, maintained through snapshots and watch events. |
| Materialized view | A parsed, indexed representation of observations from one or more materialized sources. |
| NATS subject | The broker address used to publish or subscribe to a message. |
| Reply inbox | A temporary NATS subject that receives replies to one request. |
| Revision | A store version that identifies a write to a key. |
| Revision guard | A condition that permits an update or delete only if the stored revision is the expected revision. |
| Snapshot | An immutable observation of state at one version. |
| Tombstone | A cached revision for a key that is absent. |
| TTL | Time to live. The time interval after the last write before the store expires a record. |
| Watch | A source of state snapshots and change notifications. |
| Watch heartbeat | A liveness signal from a store watch. |

Computer operations use technical verbs such as **encode**, **parse**,
**publish**, **subscribe**, and **validate**. In this specification, to publish
is to send a message through NATS. To resnapshot is to read the complete current state again.

## 3 Protocol boundaries

The substrate must carry the canonical envelope. Subjects and headers are
delivery hints. They must not replace envelope validation or Concord validation.

Endpoint sessions are local runtime and envelope identities. Lane delivery must
not depend on an endpoint-presence KV record. Closing an endpoint session must
close its local lane subscriptions. That action alone does not withdraw Beacon
advertisements or cancel Concord agreements.

Use these specifications for protocol requirements:

| Responsibility | Specification |
| --- | --- |
| Advertisement records, discovery, selection, and advertisement lifecycle | [Beacon](beacon.md). |
| Agreements, participation, authority, termination, recovery, and maintenance | [Concord](concord.md). |
| Subject formats, delivery, connection lifecycle, and shared storage mechanics | This document. |
| Protected service views and service-use fences | [Concord service use](deckr-profiles.md#service-use). |

Message lanes carry commands, data, and replies. Beacon and Concord do not
replace lane payloads. Private buckets must not replace Beacon discovery or
Concord authority. Removed contracts must not return through aliases, fallback
parsing, or parallel authority paths.

## 4 Broker connection lifecycle

### 4.1 Initial connection and shutdown

Connection attempts must have no retry-count limit or overall outage timeout.
Each connection attempt must have a bounded dial timeout.
Initial connection must retry indefinitely. It uses
`reconnect_max_interval_seconds` between attempts.

The maximum interval must be a finite positive number. Its default is 60 seconds.
Cancellation or runtime shutdown must stop connection attempts.
Supervised broker startup deadlines and unexpected process exits are separate
from connection retry policy.

### 4.2 Reconnection

After an established connection is lost, retries must use exponential backoff
with jitter. Use this procedure:

1. Set the retry window to the smaller of one second and the configured maximum interval.
2. Select a delay between half the retry window and the full retry window.
3. Wait for that delay, then try to connect.
4. Double the window after a failed attempt, up to the configured maximum interval.
5. Reset the window after a successful reconnection.

Advertisement expiry and Concord authority loss must not stop reconnection.
After reconnection, materialized sources must rebuild before they report current state.
Reconnection must not restore terminated Concord authority.
The [Concord recovery rules](concord.md#94-recovery) define the necessary new agreement.

## 5 Message subjects and headers

### 5.1 Key tokens

Subject tokens use the shared key-token encoding. Use this procedure for each value:

1. Compare the full value with `[A-Za-z0-9][A-Za-z0-9_-]*`.
2. If it matches and does not start with `b64_`, use it unchanged.
3. Otherwise, encode the value as UTF-8 bytes.
4. Encode those bytes as URL-safe base64 without padding.
5. Add `b64_` at the start of the encoded value.

For example, `deck_1` stays unchanged. The value `b64_native` becomes
`b64_YjY0X25hdGl2ZQ`. The [key-token vectors](../contract/v1/vectors/key-tokens.v1.json)
give acceptance examples.

### 5.2 Direct and broadcast subjects

The publisher must select the subject from the lane and the envelope recipient:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>
deckr.msg.<lane-token>.broadcast.<scope-token>.<endpoint-family-token>
```

For each lane, an endpoint must subscribe only to its direct subject and its
matching broadcast-family subject. The broadcast subscription uses this pattern:

```text
deckr.msg.<lane-token>.broadcast.*.<endpoint-family-token>
```

The `*` matches one scope token. Envelope and lane validation still apply.
The envelope `subject` field identifies application data. It is different from
the NATS subject.

### 5.3 Header mapping

The publisher must derive these headers from the envelope:

| Header | Envelope value |
| --- | --- |
| `Deckr-Message-Id` | `messageId`. |
| `Deckr-Message-Type` | `messageType`. |
| `Deckr-Sender` | The `sender` endpoint address. |
| `Deckr-Sender-Session` | `senderSessionId`. |
| `Deckr-Recipient` | The direct endpoint address, or `broadcast:<scope>:<endpoint-family>`. |
| `Deckr-Recipient-Session` | `recipientSessionId`, when present. |
| `Deckr-Contract-Id` | `contract.contractId`, when present. |
| `Deckr-Contract-Generation` | `contract.generation` as a decimal string, when present. |
| `Deckr-In-Reply-To` | `inReplyTo`, when present. |

The two contract headers must occur together. The canonical contract pointer
stays in `DeckrMessage.contract`:

```json
{
  "contract": {
    "contractId": "opaque-contract-id",
    "generation": 1
  }
}
```

### 5.4 Received messages

The receiver must parse the payload as a UTF-8 JSON `DeckrMessage` envelope.
It must validate the envelope and its lane contract.
For a lane subject, it must compare the subject with the envelope lane and recipient.
It must reject invalid subject formats and conflicting subject or header values.

The receiver must reject a partial contract-header pair. It must also reject
contract headers when the envelope has no contract pointer.
Missing headers do not remove or change envelope fields.
Headers must not supply authority that is absent from the envelope.

A reply inbox does not use the lane subject format. Replies received through
that inbox must still pass envelope, header, and lane validation.

## 6 Lane delivery and request/reply

### 6.1 Sending and recipient filtering

The endpoint session must set `sender` and `senderSessionId` from its own identity.
Before publication, the message bus must validate the message against the
requested lane contract. This includes message type, body, endpoint families,
broadcast scope, and contract-pointer policy.

If the message type requires `contract`, a missing pointer must fail validation
before publication. If the message type forbids `contract`, the sender must not
include a pointer. The receiver must apply the
[Concord message rules](concord.md#14-contract-pointers-on-messages)
before it uses protected authority.

A subscriber must receive a message only if these conditions are true:

- The envelope and lane contract are valid.
- The message has not expired.
- The direct recipient is the local endpoint, or the broadcast recipient matches its family.
- If `recipientSessionId` is present, it matches the local session ID.

Only a direct recipient can have `recipientSessionId`.

### 6.2 Requests and replies

Request/reply uses ordinary Deckr envelopes. The requester must subscribe to a
reply inbox before it publishes the request with that inbox as the NATS reply subject.

The requester must accept the first reply that satisfies all these conditions:

1. The reply is deliverable to the requester endpoint and session under section 6.1.
2. Its `messageId` differs from the request `messageId`.
3. Its `inReplyTo` equals the request `messageId`.
4. Its contract pointer equals the request pointer, including absence of a pointer.
5. The optional acceptance predicate accepts it.

Invalid, rejected, or non-deliverable replies must not end the request wait.
The requester must continue until it receives an acceptable reply or the timeout expires.
It must close the inbox subscription when the request completes, fails, times out,
or is cancelled.

A reply targets the request sender and `senderSessionId`, sets `inReplyTo` to
the request `messageId`, and preserves the contract pointer.

## 7 Shared KV storage requirements

### 7.1 Store access

Protocol stores must open with explicit bucket policies. This binding selects
the bucket names; Beacon and Concord define their key formats, TTL policies and
ownership checks. Managed protocol interfaces supply normal access.
Generic application bucket access must reject reserved Beacon and Concord
stores, including the Concord maintenance store.

Package-owned private buckets must have owner-qualified, versioned names,
such as `com_example_media_cache_v1`.

Exact stores and materialized sources have different responsibilities.
Exact reads and revision guards support mutations and strict validation.
Materialized sources supply cached observations, revisions, readiness, currentness,
and source generations. Normal runtime queries use managed materialized views.
They must not perform full native KV scans for each query.

### 7.2 Bucket policy checks

Before an implementation supplies a bucket handle, it must compare these normalized fields:

| JetStream field | Required policy |
| --- | --- |
| `max_age` | The bucket TTL in nanoseconds, or `0` for a persistent bucket. |
| `max_msgs_per_subject` | `1`. |
| `allow_msg_ttl` | The configured permission for broker-owned TTL writes. |
| `subject_delete_marker_ttl` | The same as `max_age` for a TTL bucket. Not applicable to a persistent bucket. |

These four fields are the complete shared Deckr policy comparison.
If they disagree with the required policy, the implementation must reject the bucket.
It must not change the existing stream.

Other stream settings are outside this comparison. These include `allow_direct`,
`storage`, and creation `metadata`. An opener must not reject or change an
existing bucket because those settings differ. A creator can select different
values for those settings.

### 7.3 Expiry and delete markers

The broker controls the TTL of a bucket. TTL buckets must keep subject delete
markers for the same duration as the bucket TTL. A client can report a marker
as a delete event or an expire event. Both events must remove the key from the
materialized source and its view.

Persistent buckets must reject per-write TTL. A TTL write must obey the explicit
bucket policy. It must not establish an independent per-key lifetime.
Protocol records get their TTL from the observed bucket TTL.
The Beacon and Concord specifications define their renewal schedules and expiry effects.

## 8 Materialized sources and recovery

### 8.1 Snapshot barrier

The NATS adapter and materialized source must use this procedure at watch startup
and after watch recovery:

1. Capture the stream high-water revision before opening the broker watch.
2. Replay the current subjects.
3. Emit a distinguishable snapshot barrier after the replay, carrying the captured revision.
4. Separate observations newer than the barrier from the recovered snapshot.
5. Apply those newer observations in revision order.
6. Install the recovered state atomically.
7. Include removals for cached keys absent from the recovered state.

The source must not expose a partially rebuilt snapshot as current state.
The barrier is a local observation boundary; its language-specific representation
is an implementation choice.
Revision checks must prevent older observations from replacing newer state.
Post-barrier absent-key revisions have a limit of 2,000.
At that limit, the source must immediately reopen the watch.
The new high-water snapshot compacts the tombstones.

### 8.2 Currentness

The source must report stale state when any of these conditions occurs:

- The NATS connection is not current.
- A required watch heartbeat is missed.
- Consumer continuity is lost.
- Consumer recovery starts.

A connected broker alone does not prove that a source is current.
Recovery must not hide a transition to stale state.
The source can report current state again only after it installs the recovered state.

The owning protocol defines the effect of stale state. In particular,
[Concord authority loss](concord.md#9-permanent-loss-of-authority)
is permanent for each affected local agreement.
[Beacon discovery loss](beacon.md#3-discovery-boundary) does not, by itself,
terminate an existing agreement.

### 8.3 Exact writes and observations

Exact create, update, and delete operations must not directly change the
materialized source. The broker watch supplies the ordered observations.
If a started lifecycle operation needs read-your-write behavior, it must wait
until the watch observes the committed revision.
Slow view readers must not block exact writes.

Full exact scans are for startup, recovery, diagnostics, maintenance, and repair
at a low frequency. Renewal loops must not become discovery or full reconciliation loops.
The owning protocols define the permitted scans and lifecycle operations.

## 9 Public state watches

### 9.1 Initial snapshot

Public state watches supply current state. They do not supply an unbounded event history.
Each watch must capture registration and its first immutable snapshot under the
same state lock. The first item must contain one `version`, its `current` flag,
and the complete filtered membership.

### 9.2 Changes and limits

Later wakeups must carry a newer version and at most 256 changed identities.
If more identities change before a subscriber reads, `resnapshot_required` must
be true and the changed set must be empty.
The subscriber must then use a complete current snapshot.
Beacon and Concord change items already contain the full filtered membership.
Their subscribers use that membership, including when change detail overflows.

A healthy slow subscriber must not block ingestion. The broadcaster must not
remove that subscriber because it is slow.
Each broadcaster permits at most 256 simultaneous registrations.
It must explicitly reject the next registration.

These requirements apply to protocol views, protected service views, and
application state projections using this binding's state-watch contract.
Applications define the projection payloads and access rules. Lane messages use
the delivery rules in section 6.

## 10 Broker resource lifecycle

KV watches and list-style helpers use JetStream consumers.
The runtime must own and close its long-lived materialized watches.
Temporary watch and list consumers must be explicitly deleted after use,
or the implementation must avoid creating them.
Server-side inactive cleanup is a fallback. It must not be the normal cleanup method.

Concord maintenance uses exact raw scans at low frequency.
It does not start materialized watches. Each temporary list consumer must close
before its scan returns. Protocol cleanup decisions remain in
[Concord maintenance](concord.md#15-concord-maintenance).

**Recommendation:** Check that consumer counts stay bounded during repeated reads,
watch recovery, and maintenance scans. Check that temporary consumers disappear
after completion. Inspect JetStream consumer counts before and after repeated
reads, reconnects and maintenance scans using the broker's management interface.

## 11 Conformance and references

All implementations must obey the envelope, lane, storage, and recovery requirements.
Schema validation alone is not sufficient.

**Recommendation:** Use acceptance tests for these conditions:

- Direct and broadcast subjects, key-token encoding, and header mappings.
- Invalid subjects, conflicting headers, and partial contract-header pairs.
- Recipient sessions, required contract pointers, and reply acceptance.
- Invalid replies followed by an acceptable reply, and request timeout cleanup.
- Indefinite connection retries, bounded attempts, backoff, and cancellation.
- Matching and conflicting bucket policies, without changes to existing streams.
- Expiry markers, concurrent snapshot writes, missing-key removal, and tombstone limits.
- Watch continuity loss, stale-state notification, and recovery before currentness.
- Atomic watch registration, change overflow, slow readers, and registration limits.
- Consumer cleanup during reads, maintenance, recovery, failure, and shutdown.

| Reference | Content |
| --- | --- |
| [Beacon specification](beacon.md) | Discovery records, stores, lifecycle, and hardware advertisements. |
| [Concord specification](concord.md) | Agreements, authority, maintenance, profiles, and protected views. |
| [NATS lane vectors](../contract/v1/vectors/nats-lane.v1.json) | Expected subjects and headers for the supplied fixtures. |
| [Key-token vectors](../contract/v1/vectors/key-tokens.v1.json) | Expected token encodings. |
| [Contract artifacts](../contract/v1/) | Schemas, fixtures, and acceptance vectors. |
| [Schema consumption](schemas.md) | Offline schema resolution and validation. |
| [Deckr profiles](deckr-profiles.md) | Application advertisements, claims and protected views. |

Implementations must conform to this specification and its schema artifacts.
Resolve any inconsistency within the reference through a reviewed contract
change; implementation behavior does not override the published requirements.

## Protocol store bindings

Generic Beacon and Concord use logical stores. This Deckr NATS binding assigns
these concrete bucket names; they are not dependencies of the generic schemas.

| Logical store | Deckr NATS bucket |
| --- | --- |
| Advertisement store | `deckr_beacon_advertisement_v1` |
| Contract store | `deckr_concord_contract_v1` |
| Participant-token store | `deckr_concord_token_v1` |
| Maintenance store | `deckr_concord_maintenance_v1` |

Key layouts, revision guards and TTL behavior remain as specified by the generic
protocols. A different application binding can assign different store names,
provided all its participants agree on the same binding and preserve those semantics.
Action Runtime's Deckr-owned protected view bucket is
`deckr_action_runtime_service_view_v1`.
