# deckr-concord: Sessioned Contract Protocol Specification Note

**Status:** draft
**Scope:** Deckr runtime coordination
**Working name:** Concord
**Purpose:** define a minimal contract protocol for temporary runtime agreements between services, independent of application state machines and mostly independent of the storage substrate.

---

## 1. Summary

`deckr-concord` is a small coordination protocol for **sessioned runtime contracts**.

A Concord contract is a temporary agreement between two or more participants. The contract is valid only while every named participant continues to maintain its own participation token. Any participant may cancel the contract at any time. If a participant stops presenting acceptable participation evidence, any other participant may cancel the contract.

The protocol does **not** attempt to prove whether a participant is dead, alive, partitioned, overloaded, paused, or deliberately uncooperative. It only defines when another participant is entitled to say:

> I am no longer receiving acceptable participation from you, therefore I cancel this contract.

Cancellation is terminal. Recovery is always by creating a new contract, possibly attached to the same higher-level application session.

---

## 2. Design goals

Concord should be:

```text
minimal
contract-first
substrate-portable
symmetric between participants
independent of application state machines
safe under stale sessions
safe under missed watch notifications
compatible with NATS JetStream KV as the first backend
```

Concord should not become:

```text
a workflow engine
a service discovery protocol
a scheduler
a distributed transaction protocol
a resource lock API
an application state machine
a metadata dumping ground
```

The protocol owns only the contract envelope:

```text
who is party to the contract
which contract generation is being discussed
which participant sessions have attached
whether every participant is maintaining a token
whether the contract has been cancelled
```

The application owns everything else.

---

## 3. Non-goals

Concord does **not** define:

```text
what the contract means
which service should be selected
how services discover one another
how a failed contract is retried
whether work resumes, restarts, pauses, or fails
how resources are allocated
whether two contracts are mutually exclusive
business-level compensation or rollback
```

Concord also does not guarantee that an old process cannot perform stale side effects outside the protocol. If a downstream resource needs protection from stale actors, the application should include the Concord contract epoch, generation, or token-derived fencing value in its own side-effecting operations.

---

## 4. Core semantics

The core model is intentionally small.

There are only three meaningful outcomes:

```text
candidate / open
valid now
cancelled
```

`valid now` does not need to be a stored state. It is a computed predicate.

A contract is usable iff:

```text
contract exists
AND contract.state == "open"
AND every named participant has a live token
AND every token names the same contractId
AND every token names the same generation
AND every token belongs to the participant that owns it
AND every token names that participant's current session
```

A contract is cancelled iff:

```text
contract.state == "cancelled"
```

Cancellation is terminal.

A cancelled contract is never resumed. A replacement or resumed interaction uses a new contract generation.

---

## 5. Fundamental rule

The central rule is:

> A Concord contract is valid only while all named participants maintain their own live participation tokens.

Everything else follows from this.

The three operational scenarios are:

```text
1. I want out.
   I cancel the contract.

2. Another participant wants out.
   They cancel the contract. I observe cancellation.

3. Another participant stops maintaining acceptable participation.
   I cancel the contract.
```

The contract layer does not care why cancellation happened.

For the contract layer:

```text
withdrawal
timeout
restart
stale session
network partition
refusal
application disagreement
```

can all result in the same state:

```text
cancelled
```

The reason may be recorded for diagnostics, but it must not change the semantics.

---

## 6. Participants are symmetric

Concord has no built-in concepts of:

```text
left
right
owner
claimant
target
leader
follower
primary
secondary
initiator
responder
```

There is only:

```text
me
others
```

Any participant may create a candidate contract.

Any named participant may attach its own token.

Any participant may cancel.

Any participant may observe another participant as stale and cancel.

The creator of a contract has no special authority after creation.

---

## 7. Contract creation is non-authoritative

Creating a contract does not make it valid.

Example:

```text
A creates contract C naming A and B.
A attaches A's token.
B has not attached B's token.
```

At this point, C exists, but it is not usable.

B may:

```text
ignore C
cancel C
attach B's token
```

Only after every named participant has attached a live token does the contract become valid.

This is the safety property that makes loose creation acceptable:

```text
creation is non-authoritative
participant tokens are authoritative
```

A contract proposal can name another participant, but it cannot force that participant into the contract.

---

## 8. Contract terms

### 8.1 Contract

A **contract** is a record naming a set of participants and a generation.

It is not the application agreement itself. It is the runtime envelope that says:

```text
these participants are attempting to maintain this temporary agreement
```

### 8.2 Participant

A **participant** is a Deckr endpoint/session-capable actor named in the contract.

The participant identity should be stable for the duration of the contract but does not need to be permanent across process restarts or autoscaling events.

For auto-scaled services, participant identities should generally be transient actor/session identities, not permanent logical service IDs.

### 8.3 Session

A **session** is the current runtime incarnation of a participant.

A participant that crashes and restarts should get a new session unless the existing session is explicitly preserved by the surrounding Deckr endpoint model.

A contract token must bind to a session.

### 8.4 Participant token

A **participant token** is the participant’s live evidence that it is still maintaining the contract.

Each participant owns only its own token.

A token says:

```text
I am participant P.
I am session S.
I am still maintaining contract C generation G.
My local refresh sequence is N.
```

### 8.5 Generation

A **generation** is the authority epoch of a contract.

A successor contract must use a new generation or a new contract ID.

A stale actor must not be able to cancel or act under a newer generation using data from an older generation.

### 8.6 Refresh sequence

A **refresh sequence** is a participant-local monotonic counter.

It increases every time that participant refreshes its token.

It is not a clock.

It is useful for:

```text
stale observation policy
debugging
peer progress checks
optional acknowledgement/convergence checks
```

### 8.7 Revision

A **revision** is the substrate’s version for a stored record.

For JetStream KV, the Python client exposes an entry `revision`, and `create`, `update`, and `delete` methods return or accept revisions; `update(key, value, last=...)` updates only if the latest revision matches. ([nats-io.github.io][1])

Concord must use revision-guarded writes for state transitions.

### 8.8 Cancellation

Cancellation is the terminal act of closing a contract.

After cancellation:

```text
the contract is not usable
participant tokens are irrelevant
the contract cannot be resumed
any recovery requires a successor contract
```

### 8.9 Staleness

A participant is stale from another participant’s perspective when it fails the local participant’s observation policy.

The observation policy may be based on:

```text
missing token
token generation mismatch
token session mismatch
unchanged token revision for N local refreshes
missing acknowledgement for N local refreshes
substrate TTL expiry
```

Concord does not need to prove that the stale participant is dead.

It only needs to permit the observing participant to cancel.

---

## 9. Abstract records

### 9.1 Contract record

A minimal contract record:

```json
{
  "schema": "deckr.concord.contract.v1",
  "contractId": "concord-01J...",
  "generation": 1,
  "participants": [
    "endpoint:a",
    "endpoint:b"
  ],
  "state": "open",
  "createdAt": "2026-05-20T10:00:00Z",
  "createdBy": "endpoint:a"
}
```

After cancellation:

```json
{
  "schema": "deckr.concord.contract.v1",
  "contractId": "concord-01J...",
  "generation": 1,
  "participants": [
    "endpoint:a",
    "endpoint:b"
  ],
  "state": "cancelled",
  "createdAt": "2026-05-20T10:00:00Z",
  "createdBy": "endpoint:a",
  "cancelledAt": "2026-05-20T10:00:14Z",
  "cancelledBy": "endpoint:b",
  "cancelRevision": 29481
}
```

`createdAt` and `cancelledAt` are diagnostic timestamps. They are not the source of protocol authority.

Required fields:

```text
schema
contractId
generation
participants
state
```

Recommended fields:

```text
createdBy
createdAt
cancelledBy
cancelledAt
cancelRevision
```

The `participants` list must be unique and canonicalised, for example sorted lexicographically, so that equivalent contracts can be compared deterministically.

### 9.2 Participant token record

A minimal participant token:

```json
{
  "schema": "deckr.concord.participant-token.v1",
  "contractId": "concord-01J...",
  "generation": 1,
  "participant": "endpoint:a",
  "sessionId": "session-a-01J...",
  "tokenId": "token-a-01J...",
  "refreshSeq": 12,
  "ttlSeconds": 30
}
```

An enhanced participant token:

```json
{
  "schema": "deckr.concord.participant-token.v1",
  "contractId": "concord-01J...",
  "generation": 1,
  "participant": "endpoint:a",
  "sessionId": "session-a-01J...",
  "tokenId": "token-a-01J...",
  "refreshSeq": 12,
  "ttlSeconds": 30,
  "contractHash": "sha256:...",
  "observed": {
    "endpoint:b": {
      "generation": 1,
      "refreshSeq": 9,
      "revision": 10342,
      "tokenHash": "sha256:..."
    }
  }
}
```

Required fields:

```text
schema
contractId
generation
participant
sessionId
tokenId
refreshSeq
ttlSeconds
```

Optional fields:

```text
contractHash
observed
diagnostic timestamp
```

The participant token is the participant’s proof of continued participation.

---

## 10. State model

The stored contract state should remain minimal:

```text
open
cancelled
```

There should not be stored contract states like:

```text
active
broken
interrupted
degraded
recovering
resuming
failed
```

Those states either are computed or belong to the application.

`active` is computed:

```text
active(contract) =
    contract.state == "open"
    AND all named participant tokens exist
    AND all named participant tokens are valid
```

`broken` is an application interpretation.

`resuming` is an application interpretation.

`failed` is an application interpretation.

Concord only says whether the contract is currently valid or cancelled.

---

## 11. Validity function

The abstract validity function:

```python
def is_valid(contract, tokens, current_sessions) -> bool:
    if contract is None:
        return False

    if contract.state != "open":
        return False

    for participant in contract.participants:
        token = tokens.get(participant)

        if token is None:
            return False

        if token.contractId != contract.contractId:
            return False

        if token.generation != contract.generation:
            return False

        if token.participant != participant:
            return False

        current_session = current_sessions.get(participant)

        if current_session is not None:
            if token.sessionId != current_session.sessionId:
                return False

    return True
```

The `current_sessions` check depends on Deckr’s endpoint/session model. If the participant token itself is the only session evidence, the session check is simply that the token exists and is internally consistent.

---

## 12. Decision flow: create a candidate contract

A participant may create a candidate contract at any time.

Flow:

```text
1. Generate contractId.
2. Set generation = 1, unless using a stable contract ID with explicit generation.
3. Set participants to the canonical participant set.
4. Set state = open.
5. create(contractKey, contractRecord).
6. Attach own participant token.
```

Important rule:

```text
creating a contract does not make it valid
```

The creator should normally attach its own token immediately after creation.

Failure cases:

```text
contract create conflict
  → generate a new contractId or inspect existing record

own token attach fails
  → cancel contract if possible, or leave for sweeper

store unavailable
  → no contract has been established
```

---

## 13. Decision flow: attach participant token

A named participant attaches by writing its own token.

Flow:

```text
1. Read contract.
2. If contract missing: stop.
3. If contract.state == cancelled: stop.
4. If participant is not in contract.participants: cancel or ignore.
5. Verify local session is current.
6. Create own token if this participant has not previously attached.
7. Begin refresh loop.
8. Revalidate contract.
```

A participant must not write another participant’s token.

If the participant sees a token under its own participant key that does not match its current session/token identity, it should cancel the contract or ignore the contract according to local policy.

In a trusted Deckr network, self-detection may be enough. In a stricter implementation, the substrate should enforce write permissions so that only participant P can write token P.

---

## 14. Decision flow: refresh own token

Token refresh is the main liveness act.

Flow:

```text
1. Read current contract.
2. If contract missing: stop acting.
3. If contract cancelled: stop acting.
4. Read own token.
5. If own token missing after prior attachment: stop acting and cancel.
6. If own token session/tokenId mismatch: stop acting and cancel.
7. Compute next refreshSeq = previous refreshSeq + 1.
8. Optionally read peer tokens and update observed map.
9. update(tokenKey, newToken, last=previousTokenRevision).
10. If update succeeds: continue.
11. If update conflicts: reread; if stale/mismatched, stop and cancel.
12. If update unavailable: stop acting locally until recovery.
```

The critical point is that refresh must use revision-guarded update.

Do not use blind put for token refresh.

If a participant has previously attached a token and later discovers that its token is missing, it must not silently recreate the token for the same contract generation. It should treat this as loss of authority and cancel or create a successor contract.

This prevents accidental resurrection of a contract after a liveness gap.

---

## 15. Decision flow: observe peer stale

A participant may cancel when another participant fails the observation policy.

Flow:

```text
1. Read contract.
2. If contract missing or cancelled: stop.
3. Read named participant tokens.
4. For each other participant:
     if token missing:
         cancel
     if token generation mismatch:
         cancel
     if token session mismatch:
         cancel
     if token fails local freshness policy:
         cancel
5. If all tokens pass: contract remains valid.
```

The local freshness policy is configurable.

Examples:

```text
missing token
unchanged token revision across N local refresh cycles
peer has not acknowledged my token revision across N local refresh cycles
peer token hash does not match expected contract hash
peer token names a different generation
```

This is the “not picking up the phone” model.

Concord does not assert:

```text
B is dead
```

It asserts:

```text
B is no longer satisfying my participation policy
therefore I cancel
```

---

## 16. Decision flow: cancel contract

Any participant may cancel.

Flow:

```text
1. Read contract.
2. If contract missing: stop.
3. If contract.state == cancelled: stop.
4. Verify the contractId and generation match the local handle.
5. Write contract.state = cancelled using revision-guarded update.
6. Stop refreshing own token.
7. Optionally delete own token.
8. Notify local application.
```

Cancellation write must be revision-guarded.

If two participants cancel concurrently, one wins and the other observes the already-cancelled state. The final result is the same.

Reason codes may be recorded, but are diagnostic only.

Possible diagnostic reason codes:

```text
self_cancelled
peer_cancelled
peer_token_missing
peer_token_stale
session_mismatch
generation_mismatch
own_token_lost
invalid_token
sweeper_orphan
```

The application must not assign different contract-layer semantics to these reason codes.

---

## 17. Decision flow: observe cancellation

Flow:

```text
1. Watch or poll contract.
2. Observe state == cancelled.
3. Stop treating contract as valid.
4. Stop refreshing own token.
5. Tear down local contract runtime.
6. Notify application.
```

The application decides whether to retry, resume, fail, pause, or compensate.

Concord only reports cancellation.

---

## 18. Failure recovery

### 18.1 Own refresh fails

If a participant cannot refresh its own token, it must assume it no longer maintains the contract.

Immediate local behaviour:

```text
stop acting under the contract
stop sending contract-authorised operations
attempt to cancel when state store becomes available
```

If the substrate is unavailable, the participant may not be able to publish cancellation immediately. That is acceptable. Local authority is still lost.

### 18.2 Peer token disappears

If a peer token is absent, the observing participant may cancel.

This is true even if the peer is still alive somewhere.

The protocol is not detecting biological/process death. It is enforcing contract participation.

### 18.3 Peer token stops advancing

If the configured policy requires peer progress and the peer token does not advance, the observing participant may cancel after the configured threshold.

Example:

```text
A has completed 3 successful token refreshes.
B's token revision has not changed.
B has not acknowledged A's current token revision.
A cancels.
```

This is a logical-progress timeout, not a wall-clock timeout.

### 18.4 Both participants disappear

If all participants disappear, nobody remains to cancel.

This produces an orphan contract.

Orphan contracts are cleaned by a sweeper or substrate policy.

A sweeper may cancel an open contract when:

```text
no participant token exists
AND contract has exceeded candidate/orphan retention policy
```

The sweeper should use revision-guarded cancellation.

### 18.5 Missed watch events

Watch notifications are an optimisation, not the authority.

Every Concord runtime must be able to repair by exact reads:

```text
read contract
read participant tokens
compute validity
```

Missing a watch event must not leave a participant permanently believing in a contract that is no longer valid.

### 18.6 Network partition

If a participant is partitioned away from the Concord substrate and cannot refresh its token, it must stop acting locally.

If the other participants can still reach the substrate, they may observe the token as stale/missing and cancel.

If nobody can reach the substrate, the contract may remain open in storage, but no well-behaved participant should continue acting without being able to maintain its own token.

### 18.7 Reconnect

Reconnect does not resurrect a contract.

If the contract was cancelled, the participant must negotiate or accept a successor contract.

If the participant’s token was lost or expired, the participant must not recreate it for the same generation. It should cancel and negotiate a successor.

A successor contract may refer to the same application-level session if the application wants to resume work, but Concord treats the successor as a new authority epoch.

---

## 19. Clock and freshness semantics

Concord should not depend on synchronised service clocks.

Client-side wall-clock timestamps are diagnostic.

They must not be the basis for authority.

Preferred freshness signals:

```text
token existence
token revision
token refreshSeq
contract generation
observed peer revision
observed peer refreshSeq
token hash
local monotonic refresh rounds
```

A participant may use its own monotonic timer to schedule refreshes. That timer does not need to match any other participant’s clock.

Recommended policy shape:

```text
refresh own token every local interval R
cancel if peer token is missing
cancel if peer token has not advanced after N successful own refreshes
cancel if peer token does not acknowledge expected generation/revision after N successful own refreshes
```

This gives a clock-light or clock-free policy.

Substrate TTL may still use server time internally. That is acceptable. The important rule is that participants should not compare their own wall-clock timestamps to decide authority.

---

## 20. Application metadata

Concord should avoid arbitrary application metadata by default.

The more data the contract carries, the more likely it becomes an application state record.

Recommended minimal contract payload:

```text
contractId
generation
participants
state
createdBy
createdAt diagnostic
cancelledBy diagnostic
cancelledAt diagnostic
```

If an application correlation is needed, allow at most one opaque field:

```text
applicationRef
```

Rules for `applicationRef`:

```text
Concord must not interpret it.
Concord must not enforce uniqueness on it unless explicitly configured.
Concord must not derive validity from it.
```

Application-specific details should live in application state.

Profile terms are the protocol extension point for application-specific
agreements. Concord validates the contract envelope, participant tokens,
generation, sessions, and term hash. The package that owns the profile validates
the term payload.

Example service-owned terms:

```json
{
  "profile": "org.example.sonos.profile.zone_binding.v1",
  "bindingId": "91db6e42-5f42-4fa5-94f5-7e698f88dc96",
  "clientEndpoint": "controller:main",
  "serviceEndpoint": "service:sonos-home",
  "zoneId": "kitchen",
  "permissions": [
    "playback",
    "volume"
  ],
  "mode": "exclusive"
}
```

The generic Concord record may carry those terms, but Concord does not know what
a Sonos zone, permission, or mode means:

```json
{
  "schema": "dev.deckr.concord.contract.v1",
  "contractId": "c1cf4ce8-9f6f-49e2-a17b-88f407f19c90",
  "generation": 1,
  "profile": "org.example.sonos.profile.zone_binding.v1",
  "participants": [
    "controller:main",
    "service:sonos-home"
  ],
  "state": "open",
  "termsHash": "sha256:...",
  "terms": {
    "profile": "org.example.sonos.profile.zone_binding.v1",
    "bindingId": "91db6e42-5f42-4fa5-94f5-7e698f88dc96",
    "clientEndpoint": "controller:main",
    "serviceEndpoint": "service:sonos-home",
    "zoneId": "kitchen",
    "permissions": [
      "playback",
      "volume"
    ],
    "mode": "exclusive"
  }
}
```

---

## 21. Duplicate contracts

Concord allows duplicate or overlapping contracts.

Example:

```text
A creates C1 naming A+B.
B creates C2 naming A+B.
```

Both are valid candidates.

If the application wants only one contract for a resource, conversation, device, stream, or job, that exclusivity is application policy.

The application may enforce uniqueness by creating its own application-side index or by using a contract group key with compare-and-set. That is outside the base Concord semantics.

---

## 22. Security and trust

The first Deckr implementation may assume a trusted, authenticated, authorised runtime network.

Under that assumption:

```text
participants may create candidate contracts naming others
participants detect invalid self-tokens and cancel
proposal spam is an operational concern
```

A stricter implementation should enforce:

```text
only participant P may write token P
only authorised participants may create contracts naming P
contract creation quotas
candidate expiry
per-participant rate limits
```

The most important security-sensitive rule is token ownership:

```text
a participant token must be writable only by the participant it represents
```

If the substrate cannot enforce this, token values should be unforgeable, for example by using a random token secret or session-bound capability.

---

## 23. NATS / JetStream substrate mapping

### 23.1 Recommended buckets

A NATS-backed implementation should separate durable contract state from TTL-bound participant tokens.

Recommended buckets or streams:

```text
deckr_concord_contract_v1
  durable contract records

deckr_concord_token_v1
  TTL-bound participant tokens

deckr_concord_event_v1
  optional audit/event stream
```

Reason:

```text
contract records should survive long enough for cancellation/audit/repair
participant tokens should expire when not refreshed
```

### 23.2 Key shapes

Contract key:

```text
contracts.<contractId>.<generation>.meta
```

Participant token key:

```text
contracts.<contractId>.<generation>.participants.<participantId>
```

Optional event subject:

```text
contracts.<contractId>.<generation>.events
```

Participant IDs must be encoded into NATS/KV-safe key tokens.

NATS KV keys may contain alphanumeric characters plus `_`, `-`, `.`, `=`, and `/`, and may be structured hierarchically for wildcard watching. ([docs.nats.io][2])

### 23.3 KV consistency

NATS documents KV as a JetStream-backed abstraction over streams and says buckets behave as immediately consistent persistent maps. It also notes that NATS guarantees monotonic writes and monotonic reads for KV but does not currently guarantee read-your-writes for direct gets, since direct gets may be served by followers or mirrors; more consistent results can be obtained by sending gets to the underlying stream leader. ([docs.nats.io][2])

Concord implication:

```text
do not use a casual read as the sole proof of authority after a write
use CAS results, revisions, exact revalidation, and repair loops
```

### 23.4 CAS operations

NATS KV supports atomic `create` and `update` operations for locking/concurrency control: `create` associates a value only if no value currently exists, and `update` is compare-and-set/compare-and-swap. ([docs.nats.io][2])

The Python client similarly exposes:

```text
create(key, value) -> revision
update(key, value, last=revision) -> revision
delete(key, last=revision) -> bool
```

where `update` only succeeds if the latest revision matches. ([nats-io.github.io][1])

Concord implication:

```text
contract creation uses create
contract cancellation uses revision-guarded update
participant token initial attach uses create
participant token refresh uses revision-guarded update
participant token cleanup uses revision-guarded delete
```

Avoid blind `put` for protocol transitions.

### 23.5 Watch semantics

NATS KV supports watching a key, watching all keys, and retrieving key history; the docs describe watch as receiving updates pushed in real time when puts or deletes happen. ([docs.nats.io][2])

The Python client exposes `watchall()` and `watch(keys, ...)`, where watch fires when a matching key is updated. ([nats-io.github.io][1])

Concord implication:

```text
watch is a wake-up path
exact get/revalidation is the authority path
```

A participant that sees a watch event should reread the contract/token set and recompute validity.

A participant must also periodically revalidate current contracts in case watch delivery is delayed, missed, restarted, or filtered.

### 23.6 TTL and expiry

NATS KV buckets can be configured with TTL limits for how long values are kept. ([docs.nats.io][2])

NATS streams also support `AllowMsgTTL`, which allows header-initiated per-message TTL instead of relying only on `MaxAge`, and `SubjectDeleteMarkerTTL`, which leaves a subject delete marker after the last message for a subject ages out. ([docs.nats.io][3])

Concord implication:

```text
if all participant tokens share one TTL:
    use a TTL-bound KV bucket

if participant tokens require per-message TTL:
    use the underlying stream features or a Deckr state-store abstraction that can set per-message TTL

if watchers must observe expiry:
    configure subject delete markers or use periodic revalidation/sweeping
```

NATS documents a `Nats-Marker-Reason` header for KV removals where subject delete markers are supported; the header may indicate reasons such as `MaxAge`, `Remove`, or `Purge`. ([docs.nats.io][4])

### 23.7 Headers and timestamps

NATS reserves the `Nats-` header namespace and instructs users not to set server-reserved headers. ([docs.nats.io][4])

`Nats-Time-Stamp` is listed under headers added when messages are republished from a stream or retrieved with direct get, and the docs say those headers should not be set on client-published messages. ([docs.nats.io][4])

Concord implication:

```text
do not rely on ordinary Deckr/NATS user headers as protocol timestamps
do not set Nats-* headers
do not make timestamp deltas the core freshness mechanism
```

Server/stream timestamps may be useful for diagnostics.

Freshness authority should come from:

```text
token existence
token revision
refreshSeq
generation
observed peer revision
CAS success/failure
```

### 23.8 Clustering and quorum

JetStream clustering uses RAFT. The NATS docs describe quorum as `½ cluster size + 1`; for a cluster of 3, at least 2 JetStream-enabled servers are needed to store new messages, and for a cluster of 5, at least 3 are needed. ([docs.nats.io][5])

The stream RAFT group has an elected leader that handles ACKs; if there is no leader, the stream will not accept messages. ([docs.nats.io][5])

NATS generally recommends 3 or 5 JetStream-enabled servers to balance scalability and failure tolerance. ([docs.nats.io][5])

Concord implication:

```text
do not rely on a two-node JetStream cluster for Concord authority
use 3 or 5 JetStream-enabled servers for serious deployments
use replicas=3 or replicas=5 for Concord buckets/streams where possible
```

NATS streams allow setting the number of replicas, with a documented maximum of 5 for clustered JetStream. ([docs.nats.io][3])

### 23.9 NATS Core is not authority

NATS Core messages may be used for:

```text
notification
wake-up
request/reply hints
application traffic
```

They must not be the authority for contract validity.

Concord authority lives in the persisted state substrate:

```text
JetStream KV contract record
JetStream KV participant token records
revision/CAS operations
```

---

## 24. NATS-backed decision table

| Operation                   | NATS primitive                                                          |
| --------------------------- | ----------------------------------------------------------------------- |
| Create candidate contract   | KV `create(contractKey, value)`                                         |
| Attach own token first time | KV `create(tokenKey, value)`                                            |
| Refresh own token           | KV `update(tokenKey, value, last=revision)`                             |
| Cancel contract             | KV `update(contractKey, cancelledValue, last=revision)`                 |
| Observe cancellation        | KV watch + exact get                                                    |
| Observe peer token          | KV get / watch token key                                                |
| Detect token expiry         | Missing token, delete marker, watch event, or periodic revalidation     |
| Clean orphan                | Sweeper exact reads + revision-guarded cancellation                     |
| Debug age                   | Server/stream timestamp or local observed-at timestamp, diagnostic only |
| Freshness authority         | revision, generation, refreshSeq, token existence                       |

---

## 25. Contract lifecycle examples

### 25.1 Successful two-party contract

```text
A creates contract C generation 1 naming A and B.
A attaches token A.
B sees C.
B accepts by attaching token B.
A and B both observe all tokens.
C is valid.
A and B refresh their own tokens.
```

### 25.2 Participant cancels deliberately

```text
A and B have valid C.
A wants out.
A revision-updates C.state to cancelled.
A stops refreshing token A.
B observes cancellation.
B stops using C.
```

### 25.3 Participant becomes stale

```text
A and B have valid C.
B stops refreshing token B.
A observes token B missing or stale.
A revision-updates C.state to cancelled.
A stops using C.
If B later returns, it sees C cancelled and must negotiate a successor.
```

### 25.4 Broken connection but same session survives

```text
A and B have valid C generation 1.
Connection between A and B breaks.
A's policy fires and A cancels C.
A and B later reconnect.
Both sessions may still be alive.
They create C generation 2 or a new contract ID.
The application may treat this as a resume of the same application session.
Concord treats it as a new authority epoch.
```

### 25.5 Both participants disappear

```text
A and B have valid C.
A disappears.
B disappears.
No participant remains to cancel.
Tokens eventually expire.
Sweeper later sees C open with no live tokens.
Sweeper cancels C with reason sweeper_orphan.
```

---

## 26. Candidate expiry

Unfulfilled candidate contracts should not accumulate indefinitely.

Recommended candidate cleanup rule:

```text
if contract.state == open
AND not all participant tokens have ever appeared
AND candidate age exceeds candidateTtl
THEN sweeper may cancel or purge
```

Because age here is a cleanup concern rather than contract authority, it may use substrate/server time or sweeper policy.

Candidate expiry is not the same as contract cancellation by a participant. It is operational garbage collection.

---

## 27. Token resurrection rule

A participant token has two phases:

```text
initial attach
refresh
```

Initial attach may use `create`.

Refresh must use `update` with the last known revision.

Once a participant has attached a token to a contract generation, loss of that token means loss of authority for that generation.

Therefore:

```text
if my token disappears after I had attached it:
    I must not recreate it for the same contract generation
    I must cancel or negotiate a successor
```

This avoids accidental resurrection after a liveness gap.

For this rule to be robust, the implementation should keep enough local handle state to know whether it had previously attached. If a participant restarts and loses that handle, it should have a new session and should not reattach to an old generation.

---

## 28. Generation and successor contracts

A contract authority epoch is:

```text
(contractId, generation)
```

A successor may be represented as:

```text
same contractId, generation + 1
```

or:

```text
new contractId, generation = 1
```

The first form is useful when there is a stable application-level contract reference.

The second form is simpler and safer for most cases.

A successor contract may include a diagnostic pointer:

```json
{
  "supersedes": {
    "contractId": "concord-01J...",
    "generation": 1
  }
}
```

This pointer must not affect validity.

---

## 29. API sketch

A minimal Python-facing API might look like:

```python
class ConcordCoordinator:
    async def create_contract(
        self,
        participants: list[str],
    ) -> ContractHandle:
        ...

    async def attach(
        self,
        contract: ContractHandle,
        participant: str,
        session_id: str,
    ) -> ParticipantHandle:
        ...

    async def refresh(
        self,
        handle: ParticipantHandle,
    ) -> ParticipantHandle:
        ...

    async def validate(
        self,
        contract: ContractHandle,
    ) -> ContractValidity:
        ...

    async def cancel(
        self,
        contract: ContractHandle,
        participant: str,
        reason: str | None = None,
    ) -> bool:
        ...

    async def watch(
        self,
        contract: ContractHandle,
    ) -> AsyncIterator[ConcordEvent]:
        ...
```

`ContractValidity` should distinguish:

```text
valid
not_yet_fulfilled
cancelled
missing_token
stale_token
generation_mismatch
session_mismatch
unavailable
```

But only `valid` versus `not valid` is semantically authoritative.

---

## 30. Conformance tests

A backend implementation should pass at least these tests.

### Creation

```text
create contract succeeds with unique id
duplicate contract key conflicts
creating contract does not make it valid
contract with missing participant token is invalid
```

### Attachment

```text
named participant can attach own token
unnamed participant cannot make contract valid
participant cannot attach token for another participant
all tokens present makes contract valid
```

### Refresh

```text
refresh increments refreshSeq
refresh uses revision guard
stale revision refresh fails
missing own token after attachment causes local loss of authority
```

### Cancellation

```text
any participant can cancel
cancellation is terminal
cancel race produces one cancelled contract
cancel by stale generation cannot affect successor generation
```

### Staleness

```text
missing peer token permits cancellation
generation-mismatched peer token permits cancellation
session-mismatched peer token permits cancellation
unchanged peer token across configured refresh rounds permits cancellation
```

### Orphans

```text
open contract with no live tokens is sweeper-cancellable
sweeper uses revision guard
sweeper does not cancel already-cancelled contract as a conflict
```

### Watches

```text
watch observes create/update/delete where backend supports it
missed watch can be repaired by exact reads
watch event alone is not treated as final authority
```

### NATS-specific

```text
KV create is used for first write
KV update(last=revision) is used for refresh/cancel
blind put is not used for protocol transitions
token bucket TTL expiry is observed by exact get/revalidation
delete markers are handled if configured
read-your-writes is not assumed from arbitrary direct get
```

---

## 31. Recommended implementation stance

The first Deckr implementation should be conservative:

```text
contract state: open | cancelled
participant tokens: separate TTL-bound records
validity: computed
cancellation: terminal
watch: notification only
exact reads: source of truth
blind put: forbidden for protocol transitions
timestamps: diagnostic only
NATS Core: notification only
JetStream KV: authority
```

The protocol should be deliberately small enough that it can later be implemented on:

```text
NATS JetStream KV
etcd
ZooKeeper
Consul
PostgreSQL
in-memory test backend
```

without changing the abstract semantics.

---

## 32. One-sentence definition

> Concord is a sessioned contract protocol in which a runtime agreement is valid only while every named participant continues to maintain its own live token, and any participant may terminate the agreement by cancelling the contract.

[1]: https://nats-io.github.io/nats.py/modules.html "Modules - nats.py documentation"
[2]: https://docs.nats.io/nats-concepts/jetstream/key-value-store "Key/Value Store | NATS Docs"
[3]: https://docs.nats.io/nats-concepts/jetstream/streams "Streams | NATS Docs"
[4]: https://docs.nats.io/nats-concepts/jetstream/headers "Headers | NATS Docs"
[5]: https://docs.nats.io/running-a-nats-service/configuration/clustering/jetstream_clustering "JetStream Clustering | NATS Docs"
