# deckr-beacon: Feature Advertisement Discovery Protocol Specification Note

**Status:** draft  
**Scope:** Deckr runtime discovery  
**Working name:** Beacon  
**Purpose:** define a minimal feature advertisement protocol for discovering candidate services, independent of Concord, independent of application state machines, and mostly independent of the storage substrate.

---

## 1. Summary

`deckr-beacon` is a small discovery protocol for **temporary feature advertisements**.

A Beacon advertisement is a fresh candidate record saying that an endpoint currently advertises a feature. Discovery consumers may use matching advertisements to decide which endpoint to try next.

Beacon does **not** prove that a service is alive, willing, healthy, authorised for a particular operation, or able to satisfy a later contract. It only defines when an advertisement is present and fresh enough to be considered a candidate.

Beacon is deliberately weaker than Concord.

```text
Beacon finds candidates.
Concord binds participants.
```

A Beacon advertisement is not an authority grant. It is not a reservation. It is not a contract. It is a temporary signal:

> I advertise feature F at endpoint E. You may try me.

If the endpoint is unavailable, refuses the request, cannot establish a Concord contract, or later disappears, that is handled by the next protocol layer.

---

## 2. Design goals

Beacon should be:

```text
minimal
feature-first
substrate-portable
current-state oriented
safe under stale advertisements
safe under missed watch notifications
compatible with NATS JetStream KV as the first backend
composable with Concord without depending on Concord
```

Beacon should not become:

```text
a contract protocol
a liveness authority
a scheduler
a load balancer
a service health model
a resource lock API
a workflow engine
an application state machine
a status dashboard
a metadata dumping ground
```

The protocol owns only the discovery envelope:

```text
which feature is being advertised
which endpoint should be tried
which session published the advertisement
whether the advertisement record is currently present
whether the advertisement record is fresh enough to return as a candidate
which weak hints may guide candidate selection
```

The application owns everything else.

---

## 3. Non-goals

Beacon does **not** define:

```text
whether a discovered endpoint is live
whether a discovered endpoint will accept a request
whether a discovered endpoint is currently healthy
which candidate must be selected
how load should be balanced
how work is scheduled
how resources are allocated
whether two advertisements are mutually exclusive
whether a Concord contract is valid
how a failed request is retried
how a selected service performs application work
```

Beacon also does not guarantee that an advertisement remains true after it has been read.

A consumer must treat discovery results as candidates, not commitments.

---

## 4. Core semantics

The core model is intentionally small.

There are only three meaningful observations:

```text
advertisement exists
advertisement is a usable candidate now
advertisement is absent / stale / unusable
```

`usable candidate now` does not need to be a stored state. It is a computed predicate.

An advertisement is a usable candidate iff:

```text
advertisement record exists
AND advertisement schema is valid
AND advertisement featureId matches the queried featureId
AND advertisement endpoint is syntactically valid
AND advertisement has not expired according to the backend/profile
AND advertisement session is acceptable if endpoint/session validation is configured
```

Beacon has no terminal cancellation state.

An advertisement may be withdrawn, deleted, replaced, refreshed, expire, or be recreated later.

---

## 5. Fundamental rule

The central rule is:

> A Beacon advertisement is only a fresh candidate record. It never proves service availability, contract authority, or operation success.

Everything else follows from this.

The three operational scenarios are:

```text
1. I want to advertise a feature.
   I publish and refresh an advertisement.

2. I no longer want to advertise a feature.
   I withdraw the advertisement or stop refreshing it.

3. I want a service for feature F.
   I read fresh advertisements for F and try one or more candidates.
```

The discovery layer does not care what happens after the candidate is tried.

For Beacon:

```text
service unavailable
request rejected
stale endpoint
contract refused
contract cancelled
resource exhausted
application failure
```

are downstream outcomes, not discovery states.

---

## 6. Advertisers and consumers

Beacon has two operational roles:

```text
advertiser
consumer
```

An **advertiser** publishes and refreshes an advertisement.

A **consumer** queries or watches advertisements and chooses candidates to try.

These roles are not authority roles. A service may be both advertiser and consumer. An advertiser is not a leader. A consumer is not required to use any returned candidate.

Beacon has no built-in concepts of:

```text
primary
secondary
leader
follower
owner
claimant
target
winner
selected service
```

There are only:

```text
advertised candidates
consumer selection policy
```

Selection policy belongs to the consumer or application.

---

## 7. Advertisement creation is non-authoritative

Creating an advertisement does not force any consumer to use it.

Example:

```text
Service A publishes advertisement X for feature F.
Consumer B queries F and sees X.
```

At this point, X is only a candidate.

B may:

```text
ignore X
try X
try another candidate first
filter X out using local policy
retry X later
```

This is the safety property that makes loose advertisement acceptable:

```text
advertisement is non-authoritative
selection is local policy
operation success is proven only by the next protocol layer
```

A feature advertisement can invite traffic, but it cannot obligate any consumer to use the endpoint.

---

## 8. Discovery terms

### 8.1 Feature

A **feature** is a canonical discoverable capability.

A feature is identified by a stable `featureId` string.

Examples:

```text
dev.deckr.concord
org.example.media.transcode
org.example.camera.discovery
```

A feature is not necessarily the same as a service namespace, API namespace, protocol version, or service instance ID.

A single endpoint may advertise multiple features by publishing multiple advertisements.

A single feature may be advertised by many endpoints.

### 8.2 Advertisement

An **advertisement** is a temporary record saying:

```text
advertiser A advertises feature F at endpoint E
```

An advertisement is current-state data. It is not an event log, not a contract, and not a reservation.

### 8.3 Advertiser

An **advertiser** is the endpoint/session-capable actor responsible for publishing and refreshing an advertisement.

The advertiser should usually be the same endpoint that will receive requests, but the protocol may allow proxy advertisements if the application explicitly permits them.

### 8.4 Endpoint

An **endpoint** is the Deckr address returned to consumers as the target to try.

Beacon does not prove endpoint liveness. Endpoint presence is a separate concern.

If Deckr endpoint/session presence is available, Beacon may use it as an optional filter or annotation, but Beacon validity must not be confused with endpoint liveness authority.

### 8.5 Session

A **session** is the runtime incarnation that published the advertisement.

If an advertiser restarts, it should normally publish advertisements under a new session.

A stale advertisement from an older session should expire or be withdrawn.

### 8.6 Candidate

A **candidate** is an advertisement returned by a discovery query after basic validation.

A candidate is not a selected service. It is only something the consumer may try.

### 8.7 Selector

A **selector** is a consumer-side filter over candidates.

Selectors may use:

```text
featureId
protocol version
labels
hints
locality
freshness
endpoint type
application policy
```

Selector semantics belong to the consumer, not to the advertisement itself.

### 8.8 Hint

A **hint** is weak selection metadata published by the advertiser.

Hints may influence candidate ordering or filtering, but must not be treated as authority.

Application profiles may promote common selection metadata into typed payload
fields when loose hints are too vague. Those fields are still Beacon discovery
signals unless the profile explicitly says another protocol makes them
authoritative.

Examples:

```text
priority
weight
load
capacity
region
zone
supported versions
```

### 8.9 Revision

A **revision** is the substrate’s version for a stored advertisement record.

Beacon should use revision-guarded writes for advertisement refresh and withdrawal where the backend supports them.

### 8.10 Freshness

**Freshness** is the computed property that an advertisement is recent enough to be returned as a candidate.

Freshness may be based on:

```text
record existence
backend TTL
backend revision
server/store write time
local observed-at time
configured refresh policy
endpoint/session validation if enabled
```

Client-authored wall-clock timestamps are diagnostic unless the backend profile explicitly promotes them.

---

## 9. Abstract records

### 9.1 Advertisement record

A minimal advertisement record:

```json
{
  "schema": "deckr.beacon.advertisement.v1",
  "advertisementId": "beacon-01J...",
  "featureId": "dev.deckr.concord",
  "advertiser": "endpoint:concord-a",
  "endpoint": "endpoint:concord-a",
  "sessionId": "session-a-01J...",
  "refreshSeq": 12,
  "ttlSeconds": 30
}
```

A richer advertisement record:

```json
{
  "schema": "deckr.beacon.advertisement.v1",
  "advertisementId": "beacon-01J...",
  "featureId": "dev.deckr.concord",
  "advertiser": "endpoint:concord-a",
  "endpoint": "endpoint:concord-a",
  "sessionId": "session-a-01J...",
  "refreshSeq": 12,
  "ttlSeconds": 30,
  "protocol": {
    "namespace": "dev.deckr.concord.service",
    "version": "1"
  },
  "operations": [
    "create_contract",
    "attach",
    "refresh",
    "cancel",
    "watch"
  ],
  "labels": {
    "region": "local",
    "zone": "living-room"
  },
  "hints": {
    "priority": 100,
    "weight": 10,
    "load": 0.25
  },
  "createdAt": "2026-05-20T10:00:00Z",
  "updatedAt": "2026-05-20T10:00:14Z"
}
```

`createdAt` and `updatedAt` are diagnostic timestamps unless supplied by the backend profile.

An extension service can advertise a package-owned feature with a
package-owned payload profile:

```json
{
  "schema": "dev.deckr.beacon.advertisement.v1",
  "advertisementId": "0e59d56a-bd75-4d2e-818e-227a90f4a6ad",
  "featureId": "org.example.sonos.service",
  "advertiser": "service:sonos-home",
  "endpoint": "service:sonos-home",
  "sessionId": "service-session",
  "refreshSeq": 12,
  "ttlSeconds": 30,
  "payload": {
    "profile": "org.example.sonos.beacon.v1",
    "serviceId": "sonos-home",
    "serviceNamespace": "org.example.sonos.service",
    "operations": [
      "play",
      "pause",
      "set_volume"
    ]
  }
}
```

The `payload` field is opaque to Beacon. Beacon validates the advertisement
envelope; the service package that owns `org.example.sonos.beacon.v1` validates
the Sonos-specific payload.

Required fields:

```text
schema
advertisementId
featureId
advertiser
endpoint
sessionId
refreshSeq
ttlSeconds
```

Optional fields:

```text
protocol
operations
labels
hints
payload
createdAt diagnostic
updatedAt diagnostic
```

### 9.2 Candidate result

A discovery API may return a candidate result that separates service-authored fields from backend-observed fields:

```json
{
  "advertisement": {
    "schema": "deckr.beacon.advertisement.v1",
    "advertisementId": "beacon-01J...",
    "featureId": "dev.deckr.concord",
    "advertiser": "endpoint:concord-a",
    "endpoint": "endpoint:concord-a",
    "sessionId": "session-a-01J...",
    "refreshSeq": 12,
    "ttlSeconds": 30
  },
  "backend": {
    "revision": 29481,
    "observedAt": "2026-05-20T10:00:15Z",
    "storedAt": "2026-05-20T10:00:14Z",
    "expiresAt": "2026-05-20T10:00:44Z"
  },
  "status": "candidate"
}
```

Backend fields are adapter-specific. The abstract protocol does not require every backend to expose all of them.

---

## 10. State model

The stored advertisement state should remain minimal.

Prefer no explicit state field.

```text
record exists and is fresh  -> candidate
record missing or expired   -> not a candidate
```

Beacon should not store advertisement states like:

```text
active
healthy
dead
selected
reserved
busy
claimed
accepted
rejected
```

Those are either computed, downstream observations, or application states.

A service that wants to stop advertising should withdraw the record or stop refreshing it.

---

## 11. Candidate validity function

The abstract candidate validity function:

```python
def is_candidate(advertisement, query, backend_meta=None, current_sessions=None) -> bool:
    if advertisement is None:
        return False

    if advertisement.schema != "deckr.beacon.advertisement.v1":
        return False

    if advertisement.featureId != query.featureId:
        return False

    if not is_valid_endpoint(advertisement.endpoint):
        return False

    if backend_meta is not None:
        if backend_meta.expired:
            return False

    if current_sessions is not None:
        current_session = current_sessions.get(advertisement.advertiser)
        if current_session is not None:
            if advertisement.sessionId != current_session.sessionId:
                return False

    if query.selector is not None:
        if not query.selector.accepts(advertisement):
            return False

    return True
```

The `current_sessions` check depends on Deckr’s endpoint/session model.

If endpoint presence is not available, Beacon should not pretend to know liveness. The candidate may still be returned, possibly annotated as `presence_unknown`.

---

## 12. Decision flow: advertise a feature

An advertiser may advertise a feature at any time if authorised by local policy.

Flow:

```text
1. Generate advertisementId.
2. Set featureId to the canonical feature being advertised.
3. Set advertiser to the current endpoint identity.
4. Set endpoint to the target endpoint consumers should try.
5. Bind the advertisement to the current sessionId.
6. Set refreshSeq = 1.
7. create(advertisementKey, advertisementRecord).
8. Begin refresh loop.
```

Important rule:

```text
creating an advertisement does not prove service availability
```

Failure cases:

```text
advertisement create conflict
  -> generate a new advertisementId or inspect existing record

store unavailable
  -> service is not discoverable through Beacon

session invalid
  -> do not advertise until a current session exists
```

---

## 13. Decision flow: refresh advertisement

Advertisement refresh is the advertiser’s statement that it still wants to be discoverable for the feature.

Flow:

```text
1. Read current advertisement handle.
2. Verify local session is still current.
3. Read current advertisement record.
4. If advertisement missing: republish according to local policy.
5. If advertisement sessionId mismatches local session: stop refreshing this advertisement.
6. Compute next refreshSeq = previous refreshSeq + 1.
7. Update hints/labels if desired.
8. update(advertisementKey, newAdvertisement, last=previousRevision).
9. If update succeeds: continue.
10. If update conflicts: reread; if stale/mismatched, publish a new advertisement or stop.
11. If update unavailable: treat service as not reliably discoverable until recovery.
```

Beacon may allow advertisement republishing after loss because discovery is not authority.

However, a stale session should not overwrite a newer session’s advertisement record.

Therefore refresh should use revision-guarded update where the backend supports it.

---

## 14. Decision flow: withdraw advertisement

Withdrawal means the advertiser no longer wants the advertisement to be returned as a candidate.

Flow:

```text
1. Read current advertisement handle.
2. Verify advertisementId/sessionId match local handle.
3. delete(advertisementKey, last=revision) where supported.
4. Stop refreshing the advertisement.
```

If delete fails due to conflict, the advertiser should reread.

If the advertisement is already missing, withdrawal is complete.

If the substrate is unavailable, the advertiser may stop refreshing and allow TTL expiry.

Withdrawal is not a contract cancellation. It is only removal of a discovery candidate.

---

## 15. Decision flow: discover candidates

A consumer discovers candidates by feature.

Flow:

```text
1. Canonicalise featureId.
2. Read advertisement records under the feature prefix.
3. Validate each record against the queried featureId.
4. Filter expired or malformed records.
5. Optionally check endpoint/session presence.
6. Apply local selector.
7. Order candidates by local selection policy.
8. Return zero or more candidates.
```

Important rule:

```text
discovery returning a candidate does not mean the candidate will work
```

If no candidates are returned, Beacon only says:

```text
no fresh advertisements for this feature were observed by this backend/profile
```

It does not prove that no service exists.

---

## 16. Decision flow: select candidate

Selection is local policy.

Beacon may provide utility functions for ordering, but the protocol does not define a globally correct candidate.

Possible ordering inputs:

```text
freshness
revision
locality
labels
protocol version
advertiser hints
consumer preferences
randomisation
round-robin state
```

A selected candidate should normally be tried by the next layer:

```text
request/reply
application protocol
Concord contract creation
endpoint presence validation
```

If the selected candidate fails, the consumer may retry another candidate.

---

## 17. Decision flow: observe stale or unreachable candidate

A consumer may discover that a candidate is stale or unreachable after selection.

Flow:

```text
1. Try candidate endpoint.
2. If request fails, times out, is rejected, or cannot establish a contract:
     mark candidate as locally failed for local retry policy.
3. Optionally revalidate the advertisement by exact read.
4. Try another candidate or report no usable candidate.
```

Beacon should not write global state merely because one consumer failed to use one candidate.

A failed candidate from one consumer’s perspective may still work for another consumer.

Local negative caching is allowed, but it is not part of the authoritative discovery record.

---

## 18. Failure recovery

### 18.1 Own refresh fails

If an advertiser cannot refresh its advertisement, it must assume it is not reliably discoverable through Beacon.

Immediate local behaviour:

```text
continue serving if appropriate
stop assuming discovery clients can find this service
retry refresh or publish a new advertisement when the store recovers
```

Unlike Concord, loss of an advertisement does not imply loss of authority to perform application work. It only affects discovery.

### 18.2 Advertisement disappears

If an advertiser observes that its own advertisement disappeared, it may publish a new advertisement if its session is still current and local policy allows.

This differs from Concord participant tokens. Concord token loss destroys authority for that contract generation. Beacon advertisement loss merely removes discoverability.

### 18.3 Advertisement stops advancing

If a consumer observes an advertisement that has not advanced according to its local policy, it may treat that advertisement as stale and avoid selecting it.

This does not require proving the advertiser is dead.

### 18.4 Endpoint unreachable

If the endpoint returned by an advertisement is unreachable, the consumer should treat the candidate as locally failed and try another candidate.

Beacon does not automatically delete the advertisement.

### 18.5 Missed watch events

Watch notifications are an optimisation, not the authority.

Every Beacon runtime must be able to repair by exact reads:

```text
read feature advertisement prefix
read candidate record exactly if needed
recompute candidate set
```

Missing a watch event must not permanently hide or preserve a candidate.

### 18.6 Network partition

If an advertiser is partitioned away from the Beacon substrate and cannot refresh, its advertisement may expire.

If a consumer is partitioned away from the Beacon substrate, it may have no current candidate set.

Beacon does not infer service death from the partition. It only exposes or withholds advertisements according to the visible state substrate.

### 18.7 Reconnect

Reconnect may allow the advertiser to refresh an existing advertisement if the record and session are still current.

If the advertisement expired or the session changed, the advertiser should publish a new advertisement.

A new advertisement may refer to the same feature and endpoint.

---

## 19. Clock and freshness semantics

Beacon should not depend on synchronised service clocks.

Client-side wall-clock timestamps are diagnostic.

Preferred freshness signals:

```text
record existence
backend TTL
backend revision
backend/server write time where available
refreshSeq
local observed-at time
endpoint/session validation if configured
```

Unlike Concord, Beacon may use backend/server time more freely for candidate expiry and ordering, because discovery is not authority.

However, participants should not compare service-authored wall-clock timestamps as the primary freshness mechanism.

Recommended policy shape:

```text
advertiser refreshes every local interval R
backend expires advertisements after TTL T
consumer filters missing/expired records
consumer may prefer recently revised records
consumer may locally avoid candidates that failed recently
```

If the backend supports TTL, backend expiry should be the primary stale-removal mechanism.

If the backend does not support TTL, Beacon must use an adapter-level sweeper or consumer-side freshness policy.

---

## 20. Metadata and hints

Beacon may carry more metadata than Concord because discovery needs selection hints.

Allowed metadata categories:

```text
protocol version
operation list
labels
locality hints
capacity/load hints
priority/weight hints
compatibility tags
```

Strict rule:

```text
Beacon hints may influence selection.
They must not be treated as reservations, commitments, or authority.
Typed profile selection fields follow the same rule unless another protocol
explicitly owns authority for that field.
```

Beacon should avoid storing application state.

Bad Beacon metadata:

```text
job progress
current task state
contract state
resource ownership
business workflow state
large payloads
application result data
```

If an application needs authoritative state, that state belongs in the application store, Concord, or another explicit protocol.

---

## 21. Duplicate and overlapping advertisements

Beacon allows duplicate or overlapping advertisements.

Examples:

```text
endpoint A advertises feature F with advertisement X
endpoint A also advertises feature F with advertisement Y
endpoint B advertises feature F
many endpoints advertise the same feature
one endpoint advertises many features
```

If the application wants uniqueness, that is application policy.

Beacon does not enforce:

```text
only one provider for a feature
only one advertisement per endpoint
only one version of a feature
only one selected provider
```

A backend or implementation may provide optional de-duplication helpers, but base Beacon semantics permit overlap.

---

## 22. Security and trust

The first Deckr implementation may assume a trusted, authenticated, authorised runtime network.

Under that assumption:

```text
endpoints may advertise authorised features
advertisements may contain weak hints
advertisement spam is an operational concern
consumers verify by trying the next protocol layer
```

A stricter implementation should enforce:

```text
only authorised endpoints may advertise feature F
only advertiser A may refresh advertisement A
only advertiser A may withdraw advertisement A
advertisement quotas
per-feature rate limits
payload size limits
label/hint validation
```

The most important security-sensitive rule is advertisement ownership:

```text
an advertisement should be refreshable only by the session that owns it
```

If the substrate cannot enforce this, advertisement values should include an unforgeable session-bound capability or token secret.

---

## 23. NATS / JetStream substrate mapping

### 23.1 Recommended buckets

A NATS-backed implementation should store advertisements in a TTL-bound KV bucket.

Recommended buckets or streams:

```text
deckr_beacon_advertisement_v1
  TTL-bound advertisement records

deckr_beacon_event_v1
  optional audit/event stream
```

Reason:

```text
advertisements are temporary discovery state
stale advertisements should disappear automatically
watchers should be able to wake up on create/update/delete where possible
```

### 23.2 Key shapes

Recommended feature-first key:

```text
advertisements.by_feature.<featureId>.<advertisementId>
```

Optional advertiser-oriented management key, if the implementation wants a secondary index:

```text
advertisements.by_advertiser.<advertiserId>.<advertisementId>
```

The base protocol should not require the secondary index.

Feature IDs, advertiser IDs, endpoint IDs, and advertisement IDs must be encoded into NATS/KV-safe key tokens.

### 23.3 KV consistency

NATS KV is a JetStream-backed abstraction over streams. The NATS documentation describes KV buckets as immediately consistent persistent maps, while noting that read-your-writes is not guaranteed for direct gets because reads may be served by followers or mirrors.

Beacon implication:

```text
use revisions and exact reads for repair
expect watch/requery loops
avoid treating a casual read immediately after write as universal truth
```

Because Beacon returns candidates rather than authority, this is usually less dangerous than it would be for Concord.

### 23.4 CAS operations

NATS KV supports:

```text
create: associate a value only if the key currently has no value
update: compare-and-set / compare-and-swap
```

The Python client exposes operations equivalent to:

```text
create(key, value) -> revision
update(key, value, last=revision) -> revision
delete(key, last=revision) -> bool
watch(keys, ...) -> KeyWatcher
```

Beacon implication:

```text
advertisement creation uses create where possible
advertisement refresh uses revision-guarded update
advertisement withdrawal uses revision-guarded delete
blind put is avoided for refresh of session-owned advertisements
```

Blind put may be acceptable for explicitly stateless, non-owned discovery records, but the recommended Deckr implementation should use revision guards.

### 23.5 Watch semantics

NATS KV supports watching keys and watching all keys. Watchers receive updates for matching keys when puts or deletes happen.

Beacon implication:

```text
watch is a wake-up path
exact get/prefix reread is the repair path
```

A consumer that sees a watch event should re-read the relevant feature prefix and recompute candidates.

A consumer should also periodically revalidate watched feature sets if missed watches matter to its local behaviour.

### 23.6 TTL and expiry

NATS KV buckets can be configured with TTL limits.

Beacon implication:

```text
use a TTL-bound advertisement bucket when all advertisements share one TTL profile
use underlying stream/per-message TTL features or adapter support if per-advertisement TTL is required
use periodic revalidation/sweeping if expiry events are not observable enough for the implementation
```

For Beacon, TTL expiry is normal. It simply removes a candidate from discovery.

### 23.7 Headers and timestamps

Beacon should not rely on ordinary Deckr/NATS user headers as authoritative protocol timestamps.

Server/stream timestamps may be useful for diagnostics and candidate ordering where exposed by the backend.

Freshness should primarily come from:

```text
record existence
backend TTL
revision
refreshSeq
backend/server write time where available
```

### 23.8 Clustering and replication

A serious NATS-backed Beacon deployment should use replicated JetStream storage appropriate to the desired discovery availability and durability.

Beacon is less authority-sensitive than Concord, but unreliable discovery still causes retries, degraded service selection, and stale candidate sets.

Recommended stance:

```text
development: single-node may be acceptable
production: 3 or 5 JetStream-enabled servers where possible
advertisement bucket replicas according to platform reliability needs
```

### 23.9 NATS Core is not discovery authority

NATS Core messages may be used for:

```text
notification
wake-up
request/reply hints
application traffic
```

They must not be the authority for the Beacon candidate set.

Beacon authority lives in the persisted state substrate:

```text
JetStream KV advertisement records
revision/CAS operations
TTL/expiry policy
```

---

## 24. NATS-backed decision table

| Operation                      | NATS primitive                                                       |
| ------------------------------ | -------------------------------------------------------------------- |
| Publish advertisement first time | KV `create(advertisementKey, value)`                                 |
| Refresh advertisement           | KV `update(advertisementKey, value, last=revision)`                  |
| Withdraw advertisement          | KV `delete(advertisementKey, last=revision)`                         |
| Discover feature candidates     | KV keys/prefix read under `advertisements.by_feature.<featureId>.*`  |
| Watch feature candidates        | KV watch over feature key pattern                                    |
| Detect expiry                   | Missing key, delete marker, watch event, or periodic revalidation    |
| Debug age                       | Server/stream timestamp or local observed-at timestamp, diagnostic   |
| Freshness authority             | key existence, TTL, revision, refreshSeq                             |
| Candidate selection             | local policy over candidate records and hints                        |

---

## 25. Discovery lifecycle examples

### 25.1 Successful advertisement and discovery

```text
A has endpoint session S1.
A advertises feature F with advertisement X.
A refreshes X periodically.
B queries feature F.
B receives X as a candidate.
B tries A's endpoint.
```

### 25.2 Advertiser withdraws

```text
A advertises feature F with advertisement X.
A no longer wants to receive traffic for F.
A deletes X with revision guard.
Consumers watching F wake up and recompute candidates.
Future queries no longer return X.
```

### 25.3 Advertiser becomes stale

```text
A advertises feature F with advertisement X.
A stops refreshing X.
X expires from the advertisement bucket.
Consumers no longer receive X as a fresh candidate.
If A later returns, it publishes a new advertisement.
```

### 25.4 Candidate fails after discovery

```text
B queries feature F and receives X.
B sends a request to X.endpoint.
The request fails.
B locally marks X as failed for retry policy.
B tries another candidate.
Beacon does not delete X merely because B failed to use it.
```

### 25.5 Concord service discovery

```text
Concord service A advertises feature dev.deckr.concord.
Client B queries Beacon for dev.deckr.concord.
B selects A as a candidate.
B calls A using the Concord service protocol.
Concord then applies its own contract semantics.
Beacon does not validate the Concord contract.
```

### 25.6 All advertisers disappear

```text
A and B both advertised feature F.
A disappears.
B disappears.
Their advertisements eventually expire.
Queries for F return no candidates.
This does not prove no provider exists; it only means no fresh advertisements were observed.
```

---

## 26. Advertisement expiry and garbage collection

Advertisements should not accumulate indefinitely.

Recommended rule:

```text
advertisement records are TTL-bound
expired advertisements are not returned as candidates
old delete markers or tombstones may be purged by backend policy
```

If the substrate does not support TTL, the Beacon adapter must provide equivalent cleanup:

```text
server-time sweeper
revision-age sweeper
consumer-side freshness filter
```

Garbage collection is operational. It does not carry application semantics.

---

## 27. Advertisement identity and resurrection

An advertisement identity is:

```text
(featureId, advertisementId)
```

Recommended practice:

```text
new session -> new advertisementId
same session -> refresh existing advertisementId
lost advertisement -> publish a new advertisementId unless exact recovery is safe
```

Unlike Concord, Beacon does not need a strict no-resurrection rule.

Reason:

```text
advertisements are candidates, not authority epochs
```

However, stale sessions must not overwrite newer sessions.

Therefore:

```text
refresh existing advertisements with revision guard
bind advertisements to sessionId
prefer new advertisementId after restart or lost handle
```

---

## 28. Relationship to Concord

Beacon and Concord are independent sibling protocols.

Beacon answers:

```text
Which endpoints currently advertise feature F?
```

Concord answers:

```text
Do these participants currently maintain a valid runtime contract?
```

Beacon may be used to find a Concord service endpoint.

Concord must not derive contract validity from Beacon.

Beacon must not derive discovery validity from Concord.

A typical composition:

```text
1. Consumer asks Beacon for feature dev.deckr.concord.
2. Beacon returns candidate Concord endpoints.
3. Consumer tries one candidate.
4. Concord negotiates or maintains a contract.
5. If the candidate fails, consumer retries discovery or another candidate.
```

Beacon discovers. Concord binds.

---

## 29. API sketch

A minimal Python-facing API might look like:

```python
class BeaconDiscovery:
    async def advertise(
        self,
        feature_id: str,
        endpoint: str,
        session_id: str,
        *,
        protocol: dict | None = None,
        operations: list[str] | None = None,
        labels: dict[str, str] | None = None,
        hints: dict[str, object] | None = None,
        ttl_seconds: int = 30,
    ) -> AdvertisementHandle:
        ...

    async def refresh(
        self,
        handle: AdvertisementHandle,
        *,
        hints: dict[str, object] | None = None,
    ) -> AdvertisementHandle:
        ...

    async def withdraw(
        self,
        handle: AdvertisementHandle,
    ) -> bool:
        ...

    async def find(
        self,
        feature_id: str,
        selector: Selector | None = None,
    ) -> list[Candidate]:
        ...

    async def watch(
        self,
        feature_id: str,
    ) -> AsyncIterator[BeaconEvent]:
        ...

    async def validate(
        self,
        candidate: Candidate,
    ) -> CandidateStatus:
        ...
```

`CandidateStatus` may distinguish:

```text
candidate
missing
expired
schema_invalid
feature_mismatch
session_mismatch
presence_unknown
unavailable
```

Only `candidate` versus `not candidate` is semantically authoritative.

---

## 30. Conformance tests

A backend implementation should pass at least these tests.

### Advertisement creation

```text
advertise feature succeeds with unique advertisementId
created advertisement is discoverable by featureId
advertisement featureId must match key featureId
malformed advertisement is not returned as candidate
```

### Refresh

```text
refresh increments refreshSeq
refresh uses revision guard where supported
stale revision refresh fails or repairs by reread
session-mismatched refresh does not overwrite current advertisement
```

### Withdrawal

```text
advertiser can withdraw own advertisement
withdraw uses revision guard where supported
withdrawn advertisement is not returned as candidate
withdraw of already-missing advertisement is harmless
```

### Discovery

```text
find(featureId) returns matching fresh advertisements
find(featureId) does not return other features
selector filters candidates
candidate ordering is local policy
no candidates does not imply no service exists
```

### Freshness

```text
expired advertisement is not returned
missing advertisement is not returned
unchanged advertisement may be locally deprioritised
backend TTL expiry can be repaired by exact reread
```

### Hints

```text
labels can be used by selectors
hints can influence ordering
hints are not treated as reservations or authority
invalid hint payload can be ignored or rejected by policy
```

### Watches

```text
watch observes create/update/delete where backend supports it
missed watch can be repaired by prefix/exact reads
watch event alone is not treated as final authority
```

### Duplicates

```text
multiple advertisements may exist for one feature
one endpoint may advertise multiple features
one endpoint may publish multiple advertisements unless policy forbids it
```

### NATS-specific

```text
KV create is used for first write where possible
KV update(last=revision) is used for refresh
KV delete(last=revision) is used for withdrawal
blind put is avoided for session-owned advertisements
TTL expiry is observed by exact get/revalidation
read-your-writes is not assumed from arbitrary direct get
```

---

## 31. Recommended implementation stance

The first Deckr implementation should be conservative:

```text
advertisement records: TTL-bound
state field: none
candidate status: computed
feature keying: feature-first
refresh: revision-guarded
withdrawal: revision-guarded delete
watch: notification only
exact/prefix reads: repair path
client timestamps: diagnostic only
NATS Core: notification only
JetStream KV: discovery state substrate
```

The protocol should be deliberately small enough that it can later be implemented on:

```text
NATS JetStream KV
etcd
ZooKeeper
Consul
PostgreSQL
Redis with caveats
in-memory test backend
```

without changing the abstract semantics.

---

## 32. One-sentence definition

> Beacon is a feature advertisement discovery protocol in which endpoints publish temporary, refreshable advertisements for features, and consumers treat matching advertisements as weak candidates rather than authority.

---

## References

[1]: https://docs.nats.io/nats-concepts/jetstream/key-value-store "Key/Value Store | NATS Docs"  
[2]: https://docs.nats.io/nats-concepts/jetstream/streams "Streams | NATS Docs"  
[3]: https://nats-io.github.io/nats.py/modules.html "Modules - nats.py documentation"
