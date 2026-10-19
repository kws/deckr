# Concord v1 specification in ASD-STE100

## Reference status

This is an application-independent protocol specification. Historic wire schema
labels are retained. The optional [Deckr NATS binding](nats.md#protocol-store-bindings)
assigns concrete buckets to the logical stores below. Beacon is a possible source
of candidates, not a schema dependency or validity requirement.

## 1 Purpose and scope

Concord gives temporary runtime agreements between named participants. A contract gives authority only while it is open and all named participants maintain the necessary tokens. Each named participant can cancel the contract. Cancellation is permanent.

This document gives the Concord v1 requirements in ASD-STE100 Simplified Technical English. The requirements apply to all implementations, in each programming language. An implementation API or source model cannot change a protocol requirement.

This document defines requirements for Concord records, authority, lifecycle, runtime behavior, maintenance, and Concord profiles.
It includes the shared identity and storage rules necessary for Concord.
Application profiles and implementation APIs are specified separately.
Beacon advertisement rules and general lane transport rules are outside this scope.

Use these references with this document:

- [NATS substrate specification](nats.md), for the substrate requirements.
- [Contract artifacts](../contract/v1), for schemas, fixtures, and acceptance vectors.
- [Schema consumption](schemas.md), for validation and reference resolution.
- [Application profiles](deckr-profiles.md), for Deckr-specific requirements.
- [Beacon specification](beacon.md), for discovery requirements.

The JSON Schemas specify record structure. They do not specify all protocol requirements. Implementations must also obey the requirements in this document. Differences between the specification, contract artifacts, and implementation are contract errors.
Correct each affected item so they agree.

The word **must** identifies a requirement. **Must not** identifies a prohibition.
The word **can** identifies a possibility or permission. Text marked
**Recommendation** identifies a recommended action, not a requirement.

## 2 Technical terms

Use the terms below with the specified meanings. Code identifiers, field names, status values, and schema names keep their exact spelling.

Shared terms have the same meanings in the [Beacon specification](beacon.md#2-technical-terms).
In Concord participant-policy sections, “candidate” means “Concord participant
candidate” and “selector” means “Concord participant selector”. “Session” means
“endpoint session”.

| Term | Meaning |
| --- | --- |
| Advertisement heartbeat | A scheduled refresh that keeps a Beacon advertisement in the store. |
| Authority | Permission to do a protected operation through a Concord agreement. |
| Bootstrap | The owner checks and cleanup that occur before a local endpoint creates a proposal. |
| Compare and set | A mutation that succeeds only if the stored revision is the expected revision. |
| Concord heartbeat | A token renewal or a watch heartbeat from a contract or token source. |
| Concord participant candidate | Contract information supplied to a Concord participant selector for a participation decision. |
| Concord participant selector | A function that selects contract pointers from an immutable batch of Concord participant candidates. |
| Contract | The stored record of an agreement between named participants. |
| Contract pointer | The pair `contractId` and `generation` that identifies one contract instance. |
| Currentness | The source state that shows if its observations are up to date. |
| Endpoint | An addressable entity in an application. |
| Endpoint address | The endpoint family and endpoint ID serialized as `<endpoint-family>:<endpoint-id>`. |
| Endpoint session | One runtime instance of an endpoint. |
| Exact read | A read of a specified key directly from the store. This read does not use a materialized source or view. |
| Fence | Identity information that prevents use of authority from a different agreement or session. |
| Handle | The local identity information for a contract or participant token. |
| Heartbeat | A scheduled record refresh or watch signal. |
| Key token | An encoded value that forms one part of a store key. |
| Lease | Local runtime state for participation in an agreement. |
| Local authority | Authority that a runtime holds for one contract generation. |
| Materialized source | A local cache of store observations and revisions, maintained through snapshots and watch events. |
| Materialized view | A parsed, indexed representation of observations from one or more materialized sources. |
| Negotiation race | Attempts to make a service-use agreement with different service candidates at the same time. |
| Opaque contract id | A contract id with no application-defined identity structure. |
| Participant | An endpoint that a Concord contract names in `participants`. |
| Participant token | A record that a participant maintains to show its participation in one contract generation. |
| Partial attachment | A token creation that succeeds before the related contract update succeeds. |
| Profile | A specification that gives the meaning of protocol data and the application rules for its use. |
| Reaper | The optional Concord service that cancels stale contracts and removes retained records. |
| Revision | A store version that identifies a write to a key. |
| Revision guard | A condition that permits an update or delete only if the stored revision is the expected revision. |
| Service candidate | A service endpoint and endpoint session that the client can try in a service-use negotiation. |
| Service descriptor | Discovery data that identifies a service, its endpoint session, and its view key prefixes. |
| Session ID | The identifier of one endpoint session. |
| Successor | A new agreement that replaces a previous agreement. |
| Token renewal | A refresh that keeps an owned Concord participant token in the store. |
| TTL | Time to live. The time interval after the last write before the store expires a record. |
| Watch | A source of state snapshots and change notifications. |
| Watch heartbeat | A liveness signal from a store watch. |
| Winner | The first valid agreement in a negotiation race. The client accepts it only after other attempts give up local authority. |

Computer operations use their technical meanings. These include `create`, `update`, `delete`, parse, encode, hash, refresh, renew, validate, reconcile, and serialize. “Attach” means to create the owned participant token and register the participant in the contract. To refresh or renew a participant token is to extend its life in the store. To withdraw a participant token is to remove it from the store after the necessary identity checks.

## 3 Authority and discovery

A Beacon candidate can identify an endpoint for a new Concord agreement. After negotiation, the advertisement is not part of contract validity.

A missing, expired, withdrawn, replaced, or changed advertisement must not invalidate an existing Concord contract. These changes must not stop protected operations through an agreement that has authority. This rule includes rendering, routing, commands, and view access.

A participant withdraws from an agreement through contract cancellation, token withdrawal, or token expiry. A profile must express loss of authority through Concord cancellation, token loss, or failure of its Concord validation. Examples include a disconnected claimed device and a stopped service.

Do not use this information to replace Concord authority:

- Endpoint presence.
- Action catalogs or hardware inventory.
- Unilateral device claims.
- Lease buckets or discovery buckets.
- Lane subscriptions or current-state records.

## 4 Identity and common rules

### 4.1 Endpoint addresses

An endpoint address has the form `<family>:<id>`. The family matches
`[a-z][a-z0-9_-]*`. The ID is nonempty, has no leading or trailing whitespace,
and contains no colon. The whole address has no leading or trailing whitespace.

Beacon and Concord do not enumerate application families or reserve application
IDs. For example, `worker:example` and `scheduler:main` are valid protocol
endpoints. [Deckr addressing policy](runtime-architecture.md#endpoint-identity)
adds its four families and action-provider restrictions in application schemas.

The [neutral endpoint schema](../contract/v1/schemas/protocols/common/endpoint-address.v1.schema.json)
is shared by both protocols. Applications must validate their own restrictions
at their composition boundary; they must not add those restrictions to this schema.

### 4.2 Key tokens

Use the shared key-token encoding for state keys.

1. Check if the value agrees with `[A-Za-z0-9][A-Za-z0-9_-]*`.
2. If it agrees and does not start with `b64_`, keep the value unchanged.
3. For all other values, encode the UTF-8 bytes as URL-safe base64 without padding.
4. Add `b64_` at the start of each base64 result.

### 4.3 Terms hashes

Use canonical JSON to calculate a Concord terms hash.

The full [canonical encoding rules](canonical-json.md) preserve the v1 byte
format, including Unicode ordering and integer/decimal distinctions. Use its
acceptance vectors when implementing the rules in another language.

1. Serialize the JSON value with sorted object keys.
2. Use compact separators without insignificant whitespace.
3. Keep Unicode as UTF-8 JSON text.
4. Calculate the SHA-256 hash of the resulting bytes.
5. Serialize the hash as `sha256:<hex-digest>`.

### 4.4 Store mutations

Use compare and set when the substrate permits it. Use `create` for initial creation. Use `update` with a revision guard for refresh and cancellation.

Recommendation: Use `delete` with a revision guard for withdrawal and deletion. The withdrawal and maintenance procedures below also specify the necessary revision guards.

Watch events tell the runtime when to check state. Exact reads and a new validity calculation give the strict authority check. A watch event alone does not give authority.

## 5 Concord stores

### 5.1 Buckets and keys

The NATS substrate uses these Concord buckets and keys:

| Record | Bucket | Key |
| --- | --- | --- |
| Contract | `<contract-store>` | `contracts.<contract-id-token>.<generation>.meta` |
| Participant token | `<participant-token-store>` | `contracts.<contract-id-token>.<generation>.participants.<participant-token>` |
| Stale observation | `<maintenance-store>` | `stale.<contract-id-token>.<generation>` |
| Token cleanup marker | `<maintenance-store>` | `cleanup.<contract-id-token>.<generation>` |

The contract and maintenance buckets are persistent. The participant-token bucket uses a TTL. Encode the contract id and participant endpoint with the key-token rules in section 4.2.

The record schemas are:

| Record | Schema |
| --- | --- |
| Contract | `dev.deckr.concord.contract.v1` |
| Participant token | `dev.deckr.concord.participant-token.v1` |
| Stale observation | `dev.deckr.concord.stale-observation.v1` |
| Token cleanup marker | `dev.deckr.concord.token-cleanup.v1` |

### 5.2 Store responsibilities

Use different interfaces for these store responsibilities:

| Store interface | Responsibility |
| --- | --- |
| Exact Concord KV | Exact reads, creation, guarded mutations, and token TTL metadata. |
| Concord materialized source | Readiness, currentness, source generation, cached raw exact-key and prefix snapshots, cached revisions, and runtime subscriptions. |
| Concord maintenance capability | Exact prefix lists, exact reads, and guarded maintenance mutations through dedicated raw stores. |

Runtime contract and token stores give exact access and materialized sources. They do not give access to maintenance mutations. Runtime reads use the materialized sources. Mutations use exact reads to check identity and revision.
Production runtime consumers must use the managed Concord view. They must not
subscribe directly to raw contract or participant-token stores.

Exact reads are also available for strict validation, recovery, and diagnostics. Only the maintenance path uses broad exact prefix scans at low frequency. The maintenance store is not part of runtime Concord readiness or validity.

An implementation should expose managed Concord lifecycle and view access.
The application binding selects and reserves the authority and maintenance
bucket names. Generic application bucket access must reject these reserved
stores. Only the dedicated maintenance capability receives all three raw stores.

### 5.3 Token TTL policy

The token bucket TTL is the only source of `ttlSeconds`. Each token record must use that TTL. Participants cannot configure a different token TTL or refresh interval. The default token bucket TTL is 120 seconds.

The broker controls token expiry. All Concord buckets must obey the
[shared KV storage requirements](nats.md#7-shared-kv-storage-requirements).
Those requirements define policy comparison, retained messages, delete markers,
cache removal, and restrictions on per-write TTL.

If the bucket TTL decreases during a refresh wait, a token can expire before its participant wakes. The participant then loses authority through the termination rules in section 9.

## 6 Record requirements

### 6.1 Contract record

A contract record must obey these requirements:

| Field | Requirement |
| --- | --- |
| `contractId` | A nonempty string without whitespace at its start or end. |
| `generation` | Greater than zero. |
| `participants` | A nonempty list of endpoint addresses, without duplicates, in lexicographic order. |
| `attachedParticipants` | A list without duplicates, in lexicographic order. Each entry must also be in `participants`. |
| `participantSessions` | Optional. Each key names a participant. Each value is a nonempty session ID. |
| `state` | `open` or `cancelled`. |
| `profile`, `terms`, `termsHash` | Optional, subject to the checks below. |
| Diagnostics and `supersedes` | Optional. `supersedes` gives diagnostic information only. |

If the contract has `terms`, it must also have `termsHash`. This hash must be the canonical JSON hash of `terms`. If the contract has `profile` and `terms.profile`, their values must be the same. `supersedes` does not change validity.

### 6.2 Participant sessions

`participantSessions` fixes the participant session IDs for this proposal. These IDs cannot change. They are not persistent service identities.

The proposal owner gets its own session entry from the supplied endpoint. A managed consumer includes the selected peer session. Managed service and hardware proposals include each known participant session.

If `participantSessions` has an entry for the participant, compare that entry with the participant's current session ID before token creation. If the IDs are different, the participant must reject the contract. It must also try to cancel the contract. It must not create a token for that proposal.

Subsequent Beacon changes must not change the accepted session IDs. Those changes must not invalidate the contract session fence.

### 6.3 Participant token record

A participant token record must obey these requirements:

| Field | Requirement |
| --- | --- |
| `contractId`, `sessionId`, `tokenId` | Nonempty strings without whitespace at their start or end. |
| `generation` | Greater than zero. |
| `participant` | An endpoint address that obeys section 4.1. |
| `refreshSeq` | Greater than zero. |
| `ttlSeconds` | Greater than zero. Use the token bucket TTL. |
| `termsHash`, `contractHash`, `observed` | Optional. |

The token `sessionId` identifies the participant's endpoint session.

## 7 Contract creation and token attachment

### 7.1 Create a proposal

Create the candidate contract with `create(contractKey, contractRecord)`. Set `attachedParticipants` to an empty list. Contract creation alone does not give authority.

A proposal uses one new opaque contract id. Do not use a contract id again. Recovery uses a new opaque contract id with generation 1.

### 7.2 Attach a participant

Each participant creates only its own token. A participant must not write a token for a different participant.

1. Read the exact contract key.
2. Make sure that the contract is `open`.
3. Make sure that `participants` contains the local participant.
4. Apply the session checks in section 6.2.
5. Generate the token identity.
6. Keep that identity in the local handle before token creation.
7. Create the local participant-token key.
8. Add the participant to `attachedParticipants` through a contract update with a revision guard.

If token creation succeeds but the contract update does not succeed, the attachment is partial. A partial attachment gives no authority. No state can report a completed attachment while a partial token is in the store.

### 7.3 Resolve partial attachment

The lifecycle owner can try the contract compare-and-set operation a maximum of eight times. After the first conflict, it must wait for an advance in the filtered Concord view before each retry.

If the retry limit is reached, the owner must withdraw the token that its retained handle identifies. It must use exact state for this withdrawal. If withdrawal cannot complete, the owner must try to cancel the contract.

If the store is unavailable, the runtime must stop local authority. It must keep the partial handle for cleanup during shutdown. The time limit also applies to this cleanup.

## 8 Token refresh

Refresh only a token for which the runtime holds the exact handle.

1. Read the exact contract key.
2. Read the exact participant-token key.
3. Make sure that the two records agree with the local handle.
4. Increase `refreshSeq`.
5. Write the token with `update(tokenKey, tokenRecord)` and a revision guard.

The runtime schedules refreshes with jitter between `ttlSeconds * 0.5` and `ttlSeconds * 0.75`. With the default TTL, the refresh interval is 60 to 90 seconds.

Validation can find more recent details for a held token. The lease can update its handle without a write at that time if these identity values stay the same:

- Contract id and generation.
- Participant.
- Session.
- Token id.
- Terms hash.

A lease without a local handle must not use a stored token as authority from participant and session identity alone.

A refresh loop must only refresh owned tokens. It must not do full Concord reconciliation, prefix discovery, broad KV scans, or attachment to successor contracts. Full discovery on a fast renewal interval is prohibited. This includes the shared five-second state-renewal interval.

Discovery uses materialized views, watch notifications, cache rebuilds at startup or reconnect, and explicit policy wakeups.

## 9 Permanent loss of authority

### 9.1 Heartbeat deadlines

A missed required Concord heartbeat must immediately stop local authority for the affected contract generation. There is no grace period. That local authority cannot pause or start again.

A Concord heartbeat is missed when one of these conditions occurs:

- A required token renewal fails.
- The store is unavailable for a required renewal.
- A required renewal times out.
- There is no confirmed renewal success by its deadline.
- A contract or token authority watch does not receive its required watch heartbeat by its liveness deadline.

Before each renewal starts, the runtime must set a deadline. The deadline must be finite and at or before the held token expiry. Retries must not extend the deadline.

Each authority watch must also have a finite liveness deadline. A connected transport must not extend or replace that deadline.

The runtime can omit a write that is not necessary only while confirmed renewal evidence meets the requirement for the same held token. This optimization must not hide or excuse a missed required heartbeat.

### 9.2 Actions after a heartbeat failure

When a required Concord heartbeat is missed, the runtime must immediately do these actions:

1. Stop protected operations through the affected agreement.
2. Stop token renewal for that generation.
3. Close local authority.
4. Try to cancel the contract.
5. Try to withdraw its owned token.

A remote cleanup failure must not cause a delay in local termination. Token expiry removes peer authority if cancellation and withdrawal cannot reach the store. The participant that found the failure must not continue operations while it waits for token expiry.

### 9.3 Other conditions that stop authority

These conditions also cause permanent loss of local authority:

- Loss of a token after completed participant attachment.
- Contract cancellation.
- A change to the held token owner, session, token id, or terms hash.
- Loss of currentness in the contract source or token source.

Loss of source currentness must stop local authority for each agreement that depends on that source. This rule also applies when exact reads work.

A source must report a missed watch heartbeat as loss of currentness. It must also report consumer recovery as loss of currentness. Internal transport recovery must not hide this change.

### 9.4 Recovery

Transport reconnection must not let terminated local authority start again. Subsequent completed reads, renewed watch delivery, and stored tokens must not let this authority start again.

Recovery needs a new opaque contract id and generation 1. The runtime can continue transport reconnection and view rebuilds to negotiate new agreements.

A missed advertisement heartbeat, a missed Beacon watch heartbeat, or an expired advertisement affects discovery only. It must not stop an existing Concord agreement.

## 10 Withdrawal and successor agreements

### 10.1 Clean close

Recommendation: When a participant lease or owner agreement closes cleanly, try to withdraw its owned token.

Use this procedure for the withdrawal:

1. Read the exact token key.
2. Check the contract id, generation, participant, session, token id, and terms hash against the local handle.
3. If all identity checks pass, delete the token with a revision guard.

A cleanup failure or changed ownership must not cause deletion of a different participant's token.

Reconciliation can release a managed lease without token deletion when only the local selection state changes. This does not let the runtime use a stored token to start again after authority loss.

### 10.2 Lost token or local lease state

After completed attachment, token loss means authority loss for that generation. The participant must not silently create a replacement token for the same generation.

Recommendation: Cancel the previous contract when possible. Negotiate a successor with a new opaque contract id and generation 1. Keep `supersedes` as diagnostic information only.

A runtime can know that it lost local lease state while its same-session token stays in KV. In this condition, the runtime must give no local authority to the previous contract. It must try to cancel the previous contract and negotiate a successor. It must not use the stored token as local authority.

An empty local ownership registry alone does not prove that local lease state was lost.

## 11 Owner bootstrap and competing sessions

Before a local endpoint creates a new proposal, its owner must distinguish lost local ownership from participation maintained by a different live session.

None of these conditions alone permits cancellation:

- The contract is not in the local ownership registry.
- The session id is different from the local session id.
- Beacon advertisements are missing.

Bootstrap cleanup can cancel a contract when this runtime knows that the contract lost local authority. Cleanup can also cancel a contract when exact Concord state proves permanent authority loss.

A different session's token shows participation if that token passes the Concord checks. This rule also applies to a pending contract.

A missing token does not prove process failure if the participant did not attach. Store unavailability also does not prove process failure. Apply these distinctions to pending, partial, attached, same-session, and previous-session records.

Service admission, capacity, and deliberate replacement are profile policy. Bootstrap must not replace a live session without an explicit profile policy.

If a profile needs exclusive ownership, unresolved contention must block these actions by the contender:

- Proposal commit.
- Readiness.
- Beacon publication.

Deliberate replacement must obey the profile policy. The displaced session must not cancel its successor only because the two sessions use the same endpoint address.

Implementations can select their contention mechanism. Random backoff can help retries. It does not prove authority or lost ownership.

After permanent authority loss is established, a process can do the necessary cleanup and negotiate new agreements. A displaced process can compete again when the role becomes available. It must not let its previous authority start again.

Exact checks must show that required bootstrap cleanup is complete before these actions can occur:

- Proposal commit.
- Provider readiness.
- Hardware-manager readiness.
- Beacon publication.

Bootstrap must not use a previous token or contract as local authority.

## 12 Contract validation

### 12.1 Conditions for authority

Strict validation reads the exact contract and each named participant token. A contract passes validation only when all these conditions are satisfied:

- The store contains the contract record, and its state is `open`.
- Each named participant is in `attachedParticipants`.
- The store contains each named participant token.
- Each token has the same `contractId` and `generation` as the contract.
- Each token belongs to the participant identified by the checked key.
- If the contract has `termsHash`, each token has that hash.
- Each token session agrees with applicable current-session evidence.

If external current-session evidence is not available, token presence and internal identity checks give the session evidence.

A missing token has different meanings before and after attachment:

| Participant state | Meaning of a missing token |
| --- | --- |
| Not in `attachedParticipants` | The contract is not yet fulfilled. |
| In `attachedParticipants` | Authority was lost for that generation. |

A token that is missing, stale, malformed, or from a different generation cannot give authority. A canceled contract cannot start again. Recovery uses a successor.

### 12.2 Malformed records

Malformed external state must give no authority. A contract or token that cannot be parsed must not be reported as missing or pending. The runtime must not use that record to regain local ownership.

Parsing exceptions must not escape a lease boundary. Before a mutation, normalize malformed records to typed terminal conflicts. Do not write state before these checks.

### 12.3 Cached and exact evaluation

The two validation paths use one pure evaluator. The evaluator does no I/O and makes no mutations. It checks participants in canonical order.

Cached validation is the usual runtime path. It gets raw cached entries by their canonical exact keys from ready, current materialized sources.

Exact validation gets the same observations through exact KV reads. This path is available for maintenance, recovery, diagnostics, and operations that must bypass the cache.

The observations cannot change during evaluation. Supplied current-session assertions must be copied, normalized, sorted, and frozen before evaluation.

For the same observations, the two paths must give the same outputs:

- Status.
- Parsed contract.
- Token mapping with correct identity information.
- Typed reason code.

The paths must not use an index that removes malformed records or records with incorrect identity before evaluation. The token mapping must keep each named token that parses successfully and passes canonical key and identity checks. It must keep those tokens if a subsequent participant causes a terminal result.

If a started runtime has a stale contract or token source, cached validation reports these values:

```text
status:      ContractValidityStatus.UNAVAILABLE
reason_code: ContractValidityReason.SOURCE_UNAVAILABLE
```

The runtime must also stop local authority as specified in section 9. `UNAVAILABLE` describes an observation failure. It does not let an existing agreement pause and start again.

Exact validation can stay available for diagnostics, cleanup, and new negotiations. It must not let terminated local authority start again. Maintenance readiness and maintenance records do not change contract validity.

### 12.4 Status and reason codes

Use `ContractValidity.reason_code` to select lifecycle behavior. The mapping below is fixed.

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
| `VALID` | None. |

`ContractValidity.reason` is diagnostic text for people. Code must not compare or parse this text to select lifecycle behavior.

### 12.5 Mutation errors

Mutation paths must read exact state, check canonical keys, and check the full record identity. Updates and deletes use revision guards. If identity values disagree, do not create, update, or delete records.

Callers must use the typed `.code` value. They must not select behavior from exception messages.

| Condition | Code |
| --- | --- |
| Creation collides with an existing key | `ConcordConflictCode.KEY_ALREADY_EXISTS` |
| Revision guard does not agree with the stored revision | `ConcordConflictCode.REVISION_CHANGED` |
| Malformed contract | `ConcordConflictCode.CONTRACT_INVALID` |
| Malformed token | `ConcordConflictCode.TOKEN_INVALID` |
| Contract identity mismatch | `ConcordConflictCode.CONTRACT_IDENTITY_MISMATCH` |
| Token identity mismatch | `ConcordConflictCode.TOKEN_IDENTITY_MISMATCH` |
| Exact store unavailable | `ConcordUnavailableCode.STORE_UNAVAILABLE` |
| Started materialized source not current | `ConcordUnavailableCode.SOURCE_STALE` |
| Token TTL metadata missing | `ConcordUnavailableCode.TTL_METADATA_MISSING` |
| Token TTL metadata incorrect | `ConcordUnavailableCode.TTL_INVALID` |

## 13 Runtime views and contexts

### 13.1 Concord view

The runtime owns one materialized view of contracts, tokens, observations with errors, and cached validity. The view uses separate contract and token materialized sources for store observations and revisions.

Filters start with the smallest applicable index. The indexes cover profile, participant, contract state, contract id, and contract pointer. Cached validation gets only the selected contract and its named token observations. A filtered watch must not parse or validate unrelated contracts.

A contract watch first yields a snapshot containing `version`, `current`, and
the full filtered collection of contract states. These are local observation
interfaces, not additional wire records.

The watch must obey the [public state watch requirements](nats.md#9-public-state-watches).
Those requirements define atomic registration, change limits, overflow,
slow subscribers, and registration limits.
Later change values contain the full filtered membership,
bounded changed contract pointers, and `resnapshot_required`.
If pointer detail overflows, the full membership gives the current result.

Reconnect rebuilds of contract and token materialized sources are atomic. The Concord view includes removal of missing pointers after these rebuilds.
Both sources must obey the [shared recovery requirements](nats.md#8-materialized-sources-and-recovery).
Section 9 defines the permanent effect of lost currentness on local authority.

### 13.2 Context entry and exit

An implementation exposes managed proposal and participant lifetimes. A proposal
combines an endpoint session with agreement intent. A participant lifetime
combines an endpoint session with a selection policy. The language-specific
resource-management API is an implementation choice.

The runtime owns lifecycle logging. Context entry owns bootstrap and private workers. Context exit does these actions:

1. Cancel the private workers.
2. Wait for those workers to finish.
3. Try remote cleanup, with cancellation shielding and a five-second limit.
4. Publish a closed state.
5. Wake subscribers.

A proposal creates only one new opaque contract. It must not loop to create a successor. When recovery needs a new proposal, the caller negotiates it. Higher-level managed contexts own the successor policy for ordinary clients.

### 13.3 Agreement intent and participant policy

Agreement intent contains:

- Profile.
- Participants.
- Terms.
- Supersedes.
- Optional static current-session assertions.
- Creator.

The endpoint supplies the local identity and session.

Participant policy is an immutable batch policy. Its selector receives an
immutable snapshot of participant candidates and supplies selected contract
pointers. Selection can be asynchronous. The selector does not hold runtime
state locks during selection.

Unknown pointers, malformed selections, and selector failures must give explicit failures.

Participant selection is a runtime infrastructure operation. Application code
must not classify Concord validity statuses to maintain an accepted agreement.
A change from `not_yet_fulfilled` to `valid` must not, by itself, cancel the
agreement or withdraw participation.

Domain policy can reject a proposal or withdraw consent for a domain reason.
The managed context must apply that decision through the Concord lifecycle.
The runtime must enforce protocol validation and termination independently of
domain consent. Continued consent must not preserve or restore authority after
a termination condition from section 9.

### 13.4 State access

Both lifetimes expose immutable state with monotonically increasing versions,
atomic predicate waits with an optional timeout, and watches that supply current
state first.

The lifecycle phases are `starting`, `ready`, `unavailable`, `terminal`, `closing`, and `closed`.

Agreement state contains currentness, validity, the contract handle, and local-authority status. Participant state contains currentness and the full immutable membership of managed contracts. Each member includes contract validity and local-authority status.

Waits distinguish timeout from closure before the predicate is satisfied.
Implementations choose suitable language-specific error types for these outcomes.

### 13.5 Reconciliation

The participant context uses one filtered subscription to the Concord view. It uses a coalesced selector worker and serialized workers for individual contract pointers.

Only these inputs cause reconciliation:

- Startup.
- Changes in the filtered view.
- Reconnect.
- Resnapshots after overflow.
- Explicit policy wakeups.

There is no periodic repair loop. A blocked selector, exact-store operation, or subscriber must not block unrelated pointer workers. It must not prevent shutdown in its time limit.

Views help local state converge. They do not give independent authority. Contract creation, cancellation, and participant-token validity are the only lifecycle authority.

## 14 Contract pointers on messages

An application message that needs Concord authority must carry an exact contract
pointer. The reusable pointer has this shape; an application defines its placement:

```json
{
  "contract": {
    "contractId": "opaque-contract-id",
    "generation": 1
  }
}
```

The receiver must validate that exact contract id and generation against Concord token validity. It must also check sender and receiver endpoint sessions as specified by the profile.

The receiver must not authorize a protected message through a search for a different contract. A route id, Beacon record, endpoint presence, catalog, current-state record, or service-use id cannot replace the contract pointer and validation.

Each identifier has a different purpose:

| Identifier | Purpose |
| --- | --- |
| `contract.contractId` and `contract.generation` | Identify the agreement that authorizes the message. |
| `senderSessionId` and `recipientSessionId` | Identify endpoint sessions. Check these against Concord participant tokens. |
| Device refs, action instance ids, binding ids, page session ids, service view refs, route ids, output generations | Identify the target or stale-state fence in the agreement. |

Application policy specifies whether a message requires a pointer.
[Deckr messaging](runtime-architecture.md) places it in the `contract` envelope
field and requires it for hardware and service lane traffic.

Public discovery, advertisements, availability probes, interest updates, and lifecycle candidates do not contain Concord authority in the envelope.

## 15 Concord maintenance

### 15.1 Maintenance ownership

Core Concord maintenance is optional. `ConcordMaintenance` receives exact raw stores for contracts, tokens, and maintenance records. Construction starts no task or watch.

Runtime `Concord` has no maintenance store, watcher, cache, readiness dependency, or mutation route.

The component `dev.deckr.concord.reaper` has no lane. It owns `ConcordReaperService.run()`. Infrastructure can also create the service from `ConcordMaintenance` and call `scan_once()` explicitly. Ordinary callers do not supply a task group.

The reaper scans at low frequency. It is not a materialized-view runtime or an immediate notification service. It uses only Concord contracts and participant tokens to calculate validity.

The reaper must not use Beacon advertisements, endpoint presence, catalogs, lane subscriptions, or other sources as parallel authority. It does not remove Beacon advertisements.

### 15.2 Stale contracts

For each `contractId:generation`, the reaper stores `firstObservedStaleAt` in `<maintenance-store>`.

An open contract counts as stale only when scan-time validation reports one of these statuses:

- `missing_token`.
- `invalid_token`.
- `session_mismatch`.
- `terms_hash_mismatch`.
- `generation_mismatch`.
- `invalid_contract`.

Validation uses the current contract and participant-token keys. `not_yet_fulfilled` counts as stale only if exact validation finds no valid refresh tokens. A pending contract with one or more valid participant tokens stays pending. `unavailable` is not evidence of a stale contract.

After the stale grace period, maintenance cancels the open contract. The default grace period is 900 seconds. Maintenance does not use a named participant identity.

The cancellation is permanent and uses these values:

```text
cancelReason: concord_reaper_stale_contract
cancelledBy:  concord:maintenance
```

This maintenance grace period does not cause a delay in local termination after authority loss.

### 15.3 Retention and audit

Keep canceled contract records until `cancelledAt` plus the retention period. The default retention period is 3600 seconds.

Before contract deletion, maintenance records these details in the audit log:

- Contract identity and profile.
- Participants and attached participants.
- Lifecycle timestamps.
- Cancellation metadata.
- Supersession.
- Terms hash.
- Current validation status.
- Participant-token summaries.

The default audit does not include full `terms`. The audit before mutation reports the listed token-key count. The cleanup result and reaper scan report completed delete counts independently.

### 15.4 Delete a retained contract

Before contract deletion, maintenance must do these actions:

1. Validate each listed participant-token entry against the exact requested prefix.
2. Write a persistent `cleanup.*` marker for the contract generation.
3. Include the contract revision to be deleted and the terms hash in that marker.
4. Delete the contract with a revision guard.

If a listed key, token identity, or cleanup marker disagrees with the expected identity, do not delete the contract. If the contract changes during deletion, log the conflict. Keep the record for a subsequent scan.

### 15.5 Complete token cleanup

After contract deletion, maintenance makes a maximum of eight immediate cleanup passes over that generation. Each token deletion uses a revision guard.

Cleanup is complete only after the two conditions are satisfied:

- An exact rescan finds no participant tokens.
- A delete with a revision guard removes the cleanup marker.

Keep the marker and report cleanup pending if one of these conditions occurs:

- The conflict retry limit is reached.
- A rescan finds malformed state.
- A rescan finds inconsistent identity information.
- The token store is unavailable.

Subsequent scans list `cleanup.*` markers independently of contract records. They continue cleanup with the same retry limit. Token TTL is only a fallback.

At the end of each scan, remove orphaned `stale.*` observations that have no contract record. Use deletes with revision guards.

## 16 Application profile requirements

Generic Concord validates agreement and token mechanics. Profile owners define
the meaning of terms and application policy. Applications compose the generic
record with their own profile/terms schemas. Generic Concord must not import an
application schema or use application policy in place of token authority.

The [Deckr profiles](deckr-profiles.md) specify hardware claims, termless
service-use contracts, Action Runtime and managed application lifecycle boundaries.
Those profiles are not prerequisites for an independent Concord implementation.

## 17 Conformance checks

Recommendation: Use the Concord artifacts in `contract/v1` as acceptance inputs for each implementation.

Schema validation alone is not sufficient. Check these Concord requirements:

- Endpoint parsing.
- Key-token encoding and Concord key generation.
- Canonical terms hashing.
- Positive generation, refresh counters, and TTL values.
- Canonical participant lists.
- Record, key, pointer, and token identity agreement.
- Profile identity agreement.
- Continued participation when a pending agreement becomes valid and domain consent is unchanged.
- Permanent authority loss and successor creation.
- The same evaluation results for the same cached and exact observations.
- Maintenance cleanup with revision guards and persistent cleanup markers.
- Applicable application requirements from [Deckr profiles](deckr-profiles.md), when implementing Deckr.

Use the valid and invalid fixtures, key-token vectors, Concord key vectors, and Concord terms-hash vectors.
Use applicable lane message schemas and vectors for contract pointers. The
artifacts also contain NATS lane subject and header vectors.

The language reference for this rewrite is [ASD-STE100 Issue 9](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf), dated January 15, 2025.
