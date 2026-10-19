# Deckr v1 conformance scenarios

## Purpose and scope

These scenarios define language-neutral checks for implementations of the Deckr v1 contracts.
They cover identity, discovery, agreement authority, state recovery, and broker resource use.
Each consuming repository supplies its own test harness.

Schema validation checks record structure.
These scenarios also check behavior that depends on time, stored records, and independent participants.
Passing structural validation alone does not establish protocol conformance.

| Reference | Purpose |
| --- | --- |
| [Beacon](docs/beacon.md) | Advertisement lifecycle and discovery. |
| [Concord](docs/concord.md) | Agreement authority and participant lifecycle. |
| [Deckr NATS binding](docs/deckr-nats-binding.md) | Transport, stores, watches, and recovery. |
| [Deckr messaging](docs/deckr-messaging.md) | Message envelopes and lane rules. |
| [Deckr services](docs/deckr-services.md) | Service-use agreements and protected views. |
| [Semantic validation rules](docs/semantic-rules.md) | Checks that supplement structural validation. |
| [Contract manifest](contract/v1/manifest.json) | Schemas, fixtures, catalogs, and vectors. |

## Test ID rules

Scenario IDs are stable once published.
Do not rename an ID or reuse it for a different scenario.
If a scenario changes incompatibly, retire its ID and assign a new ID.

The ID format is `DIT-<AREA>-<NNN>`.

| Area | Subject |
| --- | --- |
| `KEY` | Endpoint addresses, key tokens, and derived keys. |
| `HASH` | Canonical JSON and Concord terms hashes. |
| `BCN` | Beacon advertisements and discovery. |
| `CCD` | Concord contracts and participant tokens. |
| `MAT` | Materialized views, watches, recovery, and restart behavior. |
| `SVC` | Protected service views. |
| `OPS` | Broker resources and operational behavior. |

## Technical terms

Protocol terms retain their meanings in the referenced specifications.
The following terms describe the test harness:

| Term | Meaning |
| --- | --- |
| Implementation A and implementation B | Independent implementations under test. They can use different languages, processes, versions, or hosts. |
| Fault injection | A controlled change to records or delivery that creates the condition under test. |
| Observation limit | A finite test limit for an expected result. It does not replace a protocol deadline. |
| Setup | The initial records, participants, and source state for a scenario. |
| Actions | The ordered operations that exercise the scenario. |
| Expected results | The observations necessary for the scenario to pass. |

## Shared harness requirements

For each scenario that uses a broker, start with isolated stores and known bucket policies.
Use the bucket assignments in the [Deckr NATS binding](docs/deckr-nats-binding.md#protocol-store-bindings).
The harness must support these operations:

- Start and stop independent participants.
- Observe exact records, store revisions, and source currentness.
- Control time or wait for protocol deadlines.
- Inject records and interrupt watch delivery.
- Observe protected operations and public watch results.
- Count broker consumers and exact scans.

Use valid fixtures or equivalent records for setup.
Change only the fields necessary for a negative case.
Fault injection can bypass a normal writer's validation.
This does not permit a production writer to emit invalid records.

Record test observation limits and clock behavior in the test report.
Keep unrelated participants and token refreshes active unless a scenario stops them.
Do not infer immediate fault detection from a dropped watch event alone.
Use a specified currentness failure, such as a missed watch heartbeat, to test stale-source behavior.

Read the initial snapshot before checking later watch changes.
Distinguish entry changes from initial snapshots and currentness notifications.
The expected results apply to observable behavior, independently of the implementation API.
Pseudocode below illustrates artifact access; its function names are not required APIs.

## Key and hash tests

### DIT-KEY-001: Endpoint address acceptance

**Setup**

- Select the generic protocol validator and the Deckr endpoint validator.
- Use the address cases below.

| Address | Generic protocol result | Deckr result |
| --- | --- | --- |
| `controller:main` | Accept. | Accept. |
| `hardware_manager:mirabox-main` | Accept. | Accept. |
| `action_provider:clock.1` | Accept. | Accept. |
| `service:sonos_home` | Accept. | Accept. |
| `worker:example` | Accept. | Reject. |
| `action_provider:dev.deckr.controller.builtin` | Accept. | Reject. |
| Empty string | Reject. | Reject. |
| `controller:` | Reject. | Reject. |
| `:main` | Reject. | Reject. |
| `controller:main:extra` | Reject. | Reject. |
| ` controller:main` | Reject. | Reject. |
| `controller:main ` | Reject. | Reject. |

**Actions**

1. Validate each address at both boundaries.
2. Try to publish records with addresses rejected at the applicable boundary.

**Expected results**

- Each validator gives the result in the table.
- Invalid addresses do not appear in emitted advertisements, contracts, tokens, service views, or lane envelopes.
- Generic Beacon and Concord accept application-defined families independently of Deckr restrictions.

**References:** [Beacon: Endpoint addresses](docs/beacon.md#43-endpoint-addresses),
[Concord: Endpoint addresses](docs/concord.md#41-endpoint-addresses),
[Deckr messaging: Endpoint identity](docs/deckr-messaging.md#endpoint-identity).

### DIT-KEY-002: Key-token vector compatibility

**Setup**

- Load the key-token vector document.
- Select its `cases` array.

**Actions**

1. Encode each case's `raw` value with the implementation under test.
2. Compare the result with the case's `token` value.
3. Decode the token and compare the result with `raw`.

```text
document = load_json("contract/v1/vectors/key-tokens.v1.json")
for case in document.cases:
  assert encode_key_token(case.raw) == case.token
  assert decode_key_token(case.token) == case.raw
```

**Expected results**

- Every encoded token matches exactly.
- Decoding preserves the original value, including punctuation and reserved `b64_` prefixes.
- Encoded base64 tokens have no padding.

**References:** [Deckr NATS binding: Key tokens](docs/deckr-nats-binding.md#51-key-tokens),
[Key-token vectors](contract/v1/vectors/key-tokens.v1.json).

### DIT-KEY-003: Beacon and Concord key vector compatibility

**Setup**

- Load the Beacon and Concord key vector document.
- Select its `cases` array.

**Actions**

1. Select the key construction operation identified by each case's `helper`.
2. Supply that case's `input` fields.
3. Compare the derived key with the case's `key` value.

```text
document = load_json("contract/v1/vectors/beacon-concord-keys.v1.json")
for case in document.cases:
  actual = derive_protocol_key(case.helper, case.input)
  assert actual == case.key
```

**Expected results**

- Each key matches the vector byte for byte.
- Independent implementations address the same logical records with the same store keys.

**References:** [Beacon: Bucket and key](docs/beacon.md#51-bucket-and-key),
[Concord: Buckets and keys](docs/concord.md#51-buckets-and-keys),
[Beacon and Concord key vectors](contract/v1/vectors/beacon-concord-keys.v1.json).

### DIT-HASH-001: Concord terms hash compatibility

**Setup**

- Load the Concord terms-hash vector document.
- Select its `cases` array.
- Keep each `inputJson` string unchanged until the implementation's terms decoder reads it.

**Actions**

1. Decode `inputJson` with a decoder that preserves integer tokens and decimal or exponent tokens.
2. Produce the canonical UTF-8 bytes with the implementation under test.
3. Compare those bytes with the UTF-8 encoding of `canonicalJson`.
4. Calculate SHA-256 over the produced bytes.
5. Add `sha256:` before the lowercase hexadecimal digest.
6. Compare that value with `hash`.

```text
document = load_json("contract/v1/vectors/concord-terms-hash.v1.json")
for case in document.cases:
  terms = decode_terms_preserving_number_tokens(case.inputJson)
  actual_bytes = canonical_terms_utf8(terms)
  assert actual_bytes == utf8(case.canonicalJson)
  actual_hash = "sha256:" + lowercase_hex(sha256(actual_bytes))
  assert actual_hash == case.hash
```

**Expected results**

- Canonical bytes and hashes both match every vector.
- `1` and `1.0` retain their different canonical encodings.
- The results cover Unicode scalar ordering, negative zero, large integers, binary64 rounding, and notation thresholds.

**Note:** Parsing and serializing the input through an ordinary JSON number representation can erase distinctions before the test starts.
Hashing only the supplied `canonicalJson` does not test the implementation's canonical encoder.

**References:** [Concord: Terms hashes](docs/concord.md#43-terms-hashes),
[Concord terms hashing](docs/concord-terms-hashing.md),
[Concord terms-hash vectors](contract/v1/vectors/concord-terms-hash.v1.json).

## Beacon tests

### DIT-BCN-001: Advertisement create, refresh, and withdraw

**Setup**

- Start an advertiser in implementation A and a consumer in implementation B.
- Use the same advertisement store and a known positive bucket TTL.
- Prepare a valid hardware advertisement for the advertiser's current endpoint session.

**Actions**

1. Publish the advertisement through the managed advertisement lifecycle.
2. Wait for the consumer to observe the candidate.
3. Observe a scheduled refresh and read the exact advertisement record.
4. Withdraw the advertisement through its managed lifecycle.
5. Wait for the consumer's candidate set to remove it.
6. Publish a new advertisement.
7. Stop the advertiser without withdrawal.
8. Wait for broker expiry.

**Expected results**

- Publication makes the advertisement available as a candidate.
- Refresh increases `refreshSeq` and preserves the held identity.
- `ttlSeconds` comes from the observed bucket TTL.
- Withdrawal and expiry both remove the advertisement from discovery.

**References:** [Beacon: Advertisement lifecycle](docs/beacon.md#6-advertisement-lifecycle).

### DIT-BCN-002: Advertisement identity and key agreement

**Setup**

- Start a consumer with a current Beacon view.
- Prepare a valid advertisement with `advertisementId = ad-2` and `featureId = dev.deckr.hardware`.

**Actions**

1. Derive a valid advertisement key for that feature and `advertisementId = ad-1`.
2. Write the prepared `ad-2` record at that key through fault injection.
3. Wait for the view to observe the injected revision.
4. Query candidates for `dev.deckr.hardware`.

**Expected results**

- The mismatching record is not a usable candidate.
- Invalid-record diagnostics can expose the mismatch without granting candidate status.

**Note:** Encode the feature ID with the key-token algorithm.
An unencoded dotted feature ID would also test an incorrect key layout.

**References:** [Beacon: Candidate selection](docs/beacon.md#7-candidate-selection),
[Beacon: Bucket and key](docs/beacon.md#51-bucket-and-key).

### DIT-BCN-003: Beacon is discovery only

**Setup**

- Start a controller and a hardware manager in independent implementations.
- Publish a hardware advertisement and negotiate a valid hardware claim.
- Keep the claim's participant tokens and authority sources current.

**Actions**

1. Withdraw the hardware advertisement.
2. Wait for the controller's Beacon view to remove the candidate.
3. Validate the exact claim and observe protected operations under the held agreement.

**Expected results**

- The advertisement disappears from discovery.
- Advertisement withdrawal alone does not invalidate the claim or stop protected operations.
- Authority continues to depend on Concord and the hardware profile checks.

**References:** [Beacon: Discovery boundary](docs/beacon.md#3-discovery-boundary),
[Concord: Authority and discovery](docs/concord.md#3-authority-and-discovery).

### DIT-BCN-004: Startup cleanup does not revoke existing Concord claims

**Setup**

- Maintain a valid hardware claim between a controller and a hardware manager.
- Seed stale advertisements for the same feature, advertiser, and endpoint.
- Select an implementation that performs the recommended advertisement startup cleanup.
- Keep the active claim's sessions, tokens, and authority sources current.

**Actions**

1. Start advertisement publication for the current hardware-manager session.
2. Observe the startup cleanup of stale advertisements.
3. Validate the claim and observe its held local authority.

**Expected results**

- Startup cleanup removes stale advertisements with owner checks and revision guards.
- Cleanup alone does not cancel the existing claim or withdraw its participant tokens.
- The held agreement retains authority while its Concord and profile checks pass.

**Note:** This scenario isolates advertisement startup from the Concord participant lifecycle.
A full runtime crash can separately cause permanent local authority loss.
Implementations without the recommended cleanup can report this scenario as `not-applicable` with that explanation.

**References:** [Beacon: Create an advertisement](docs/beacon.md#61-create-an-advertisement),
[Deckr hardware: Hardware claims](docs/deckr-hardware.md#hardware-claims).

## Concord tests

### DIT-CCD-001: Contract is not valid until all participants attach

**Setup**

- Prepare an open contract with two named participants.
- Set `attachedParticipants` to an empty list.
- Prevent background attachment while the initial state is checked.

**Actions**

1. Create the contract record.
2. Validate the exact contract and participant-token keys.
3. Attach only one participant and repeat validation.
4. Attach the second participant with a valid token and repeat validation.

**Expected results**

- The first two validations return `not_yet_fulfilled` and no authority.
- The contract becomes valid only after all participants attach with valid tokens.
- An open contract record alone does not grant authority.

**References:** [Concord: Contract creation and token attachment](docs/concord.md#7-contract-creation-and-token-attachment),
[Concord: Conditions for authority](docs/concord.md#121-conditions-for-authority).

### DIT-CCD-002: Participant token ownership

**Setup**

- Prepare a fully attached contract and valid tokens for its controller and hardware manager.
- Confirm that exact validation returns `valid`.
- Hold background mutations while checking injected records.

**Actions**

1. Exercise participant creation and refresh through each participant's managed lifecycle.
2. Check which participant-token keys each lifecycle writes.
3. Inject a token whose `participant` names the controller at the hardware-manager token key.
4. Leave the controller's own token valid.
5. Validate the exact contract and tokens.

**Expected results**

- Each participant lifecycle writes only its own token.
- The injected key and participant mismatch returns `invalid_token`.
- The mismatching token gives no authority.

**References:** [Concord: Attach a participant](docs/concord.md#72-attach-a-participant),
[Concord: Token refresh](docs/concord.md#8-token-refresh).

### DIT-CCD-003: Missing attached token invalidates authority

**Setup**

- Prepare a fully attached hardware claim with valid participant tokens.
- Confirm that exact validation returns `valid`.
- Hold background mutations during the record validation check.

**Actions**

1. Delete the hardware manager's token through fault injection.
2. Keep the contract open and its attachment list unchanged.
3. Validate the exact contract and remaining token.

**Expected results**

- Validation returns `missing_token` and no authority.
- Completed attachment prevents the missing token from being treated as a pending attachment.

**References:** [Concord: Permanent loss of authority](docs/concord.md#9-permanent-loss-of-authority),
[Concord: Conditions for authority](docs/concord.md#121-conditions-for-authority).

### DIT-CCD-004: Cancelled contract is terminal

**Setup**

- Prepare a fully attached, valid hardware claim.
- Retain copies of its valid participant-token records for fault injection.

**Actions**

1. Cancel the contract through a named participant.
2. Inject refreshed or recreated token records for that same contract generation.
3. Validate the exact contract and tokens.

**Expected results**

- Validation returns `cancelled` and no authority.
- Token records do not restore a cancelled agreement.
- Recovery requires a new agreement under the Concord successor rules.

**References:** [Concord: Permanent loss of authority](docs/concord.md#9-permanent-loss-of-authority),
[Concord: Withdrawal and successor agreements](docs/concord.md#10-withdrawal-and-successor-agreements).

### DIT-CCD-005: Terms hash mismatch is invalid

**Setup**

- Prepare a fully attached contract with valid terms, their canonical hash, and matching participant tokens.
- For a hardware claim, use terms that satisfy the hardware claim schema.
- Confirm that exact validation returns `valid`.
- Hold background mutations during the record validation check.

**Actions**

1. Change only one participant token's `termsHash` through fault injection.
2. Use a structurally valid SHA-256 value that differs from the contract hash.
3. Validate the exact contract and tokens.

**Expected results**

- Validation returns `terms_hash_mismatch` and no authority.
- The contract's terms hash is checked for every named participant token.

**References:** [Concord: Contract validation](docs/concord.md#12-contract-validation),
[Hardware claim terms schema](contract/v1/schemas/deckr/hardware/claim-terms.v1.schema.json),
[Concord terms hashing](docs/concord-terms-hashing.md).

### DIT-CCD-006: Exact validation bypasses stale local views

**Setup**

- Load valid contract and token records into a runtime's current materialized sources.
- Use controlled records without a local managed lease that could cancel them during this observation check.
- Keep exact store reads available.

**Actions**

1. Interrupt the token watch so that it misses the next token deletion.
2. Delete an attached participant's token.
3. Cause a specified currentness failure, such as a missed required watch heartbeat.
4. Wait for the token source to report stale state.
5. Compare cached validation with exact validation before watch recovery.

**Expected results**

- Cached validation returns `unavailable` with reason `source_unavailable`.
- Exact validation returns `missing_token` from the current store records.

**Note:** [DIT-MAT-004](#dit-mat-004-missed-concord-token-deletion-is-recovered) checks permanent local authority loss and recovery.

**References:** [Concord: Cached and exact evaluation](docs/concord.md#123-cached-and-exact-evaluation),
[Deckr NATS binding: Currentness](docs/deckr-nats-binding.md#82-currentness).

### DIT-CCD-007: Missed watch heartbeat stops authority with valid records

**Setup**

- Maintain a valid managed agreement between two participants.
- Confirm that the tested participant can do protected operations.
- Keep its broker connection, exact reads, and required token renewals available.
- Keep optional maintenance scans stopped.
- Make cancellation and owned-token deletion fail through fault injection.
- Use controlled time. Finish both record checks before any token expires.
- Run the scenario once for the contract source and once for the token source.

**Actions**

1. Suppress required watch heartbeats for the selected source only.
2. Advance time to its liveness deadline. Keep the broker connection active.
3. Observe source currentness and local authority. Try a protected operation.
4. Use an independent observer to validate the exact contract and tokens.
5. Restore watch heartbeats and let the source recover. Keep cleanup writes blocked.
6. Repeat exact validation before any token expires. Check the previous local agreement.
7. Allow cleanup writes. Complete required cleanup and explicitly negotiate a new agreement.

**Expected results**

- The selected source reports stale state at its liveness deadline.
- The broker stays connected. No required token renewal fails before authority loss.
- Local authority terminates when the source becomes stale. Protected operations stop.
- The runtime stops token renewal for the affected generation and tries cancellation and withdrawal.
- Exact validation returns `valid` during both record checks. The contract stays open and its tokens remain acceptable.
- Successful exact reads and source recovery do not restore the previous local authority.
- New authority requires a new opaque contract ID and generation 1.
- An implementation cannot pass this scenario by detecting token loss or contract cancellation.

**References:** [SEM-CCD-003: Exact authority](docs/semantic-rules.md#sem-ccd-003-exact-authority),
[Concord: Permanent loss of authority](docs/concord.md#9-permanent-loss-of-authority),
[Concord: Cached and exact evaluation](docs/concord.md#123-cached-and-exact-evaluation),
[Deckr NATS binding: Currentness](docs/deckr-nats-binding.md#82-currentness).

### DIT-CCD-008: Renewal failure with blocked cleanup and reconnection

**Setup**

- Maintain a valid managed agreement with current contract and token sources.
- Confirm that the tested participant holds local authority and the exact records pass validation.
- Record the held token identity, revision, expiry, and renewal deadline.
- Select a required renewal that cannot be omitted under the confirmed-freshness rule.
- Keep the peer's token renewals active and optional maintenance scans stopped.
- Make cancellation and owned-token deletion fail through fault injection.
- Use controlled time to permit the record checks and reconnection before token expiry.

**Actions**

1. Make the selected required renewal return a store failure. Keep required watch heartbeats and broker delivery active.
2. Observe local authority loss and cleanup attempts before causing a watch or transport failure.
3. Try a protected operation. Validate the exact records through an independent observer.
4. Disconnect the tested runtime from the broker while remote cleanup remains blocked.
5. Restore its connection and token-write access. Let its sources rebuild. Keep cancellation and withdrawal blocked.
6. Repeat exact validation and try a protected operation through the previous agreement.
7. Allow cleanup writes. Complete required cleanup and explicitly negotiate a successor.

**Expected results**

- The renewal failure immediately terminates local authority without a separate watch or transport failure.
- Cancellation and withdrawal are attempted. Their failure does not delay termination.
- Protected operations stop before token expiry or remote cleanup.
- The runtime does not retry renewal to restore the previous agreement or extend its renewal deadline.
- The stored contract stays open and its unexpired tokens pass exact validation during both record checks.
- Local authority loss does not stop broker reconnection or source recovery.
- Reconnection, current sources, and successful exact reads do not restore the previous local authority.
- The runtime does not renew or recreate its previous token after write access returns.
- A successor uses a new opaque contract ID and generation 1. Only the new agreement can authorize new protected operations.

**References:** [SEM-CCD-003: Exact authority](docs/semantic-rules.md#sem-ccd-003-exact-authority),
[SEM-CCD-004: Mutations and maintenance](docs/semantic-rules.md#sem-ccd-004-mutations-and-maintenance),
[Concord: Heartbeat deadlines](docs/concord.md#91-heartbeat-deadlines),
[Concord: Actions after a heartbeat failure](docs/concord.md#92-actions-after-a-heartbeat-failure),
[Concord: Recovery](docs/concord.md#94-recovery),
[Deckr NATS binding: Reconnection](docs/deckr-nats-binding.md#42-reconnection).

### DIT-CCD-009: Bootstrap preserves another live session

**Setup**

- Start an owner session and a contender with the same endpoint address and different session IDs.
- Use a profile that permits only one live session for the owner role. Its policy does not permit deliberate replacement.
- The owner maintains an open contract and an acceptable token for its own session.
- The contender has an empty ownership registry and no local handle for that contract.
- Remove the owner's Beacon advertisement without stopping its Concord participation.
- Keep exact stores and authority sources available. Keep optional maintenance scans stopped.
- Run both attachment cases below. Keep the selected case stable during contention checks.

| Case | Existing contract and tokens |
| --- | --- |
| Fully attached. | All participants are attached and maintain acceptable tokens. Exact validation returns `valid`. |
| Pending. | The owner is attached and maintains an acceptable token. The peer has never attached and has no token. Exact validation returns `not_yet_fulfilled`. |

**Actions**

1. Start the contender's owner-bootstrap checks.
2. Observe those checks and repeated contention wakeups while the owner's token remains acceptable.
3. Try to commit a contender proposal, report readiness, and publish its Beacon advertisement.
4. Delay exact confirmation of required bootstrap cleanup. Then have the owner deliberately withdraw and cancel its contract through its normal lifecycle.
5. Repeat the contender's gated operations while cleanup confirmation is blocked.
6. Allow exact cleanup confirmation. Let the contender negotiate a new agreement.

**Expected results**

- The contender does not cancel the existing contract or change the owner's token during contention.
- An empty registry, a different session ID, and missing Beacon discovery do not prove abandoned ownership.
- In the pending case, the peer's missing token does not prove process failure or permit bootstrap cancellation.
- The contender cannot use the stored contract or token as local authority.
- Unresolved exclusive contention blocks proposal commit, readiness, and Beacon publication.
- Those gates stay blocked until exact checks confirm required cleanup after owner withdrawal.
- The contender can then negotiate with a new opaque contract ID and generation 1.

**References:** [SEM-CCD-004: Mutations and maintenance](docs/semantic-rules.md#sem-ccd-004-mutations-and-maintenance),
[Concord: Owner bootstrap and competing sessions](docs/concord.md#11-owner-bootstrap-and-competing-sessions),
[Concord: Conditions for authority](docs/concord.md#121-conditions-for-authority).

## Materialized view and restart tests

### DIT-MAT-001: Watch events are wakeups, exact state is recoverable

**Setup**

- Start a producer and a consumer with a current Beacon view.
- Publish an advertisement and wait until the consumer sees it.

**Actions**

1. Interrupt the consumer's watch so that it misses the advertisement deletion.
2. Withdraw the advertisement.
3. Cause a specified currentness failure and observe the stale transition.
4. Restore delivery and allow snapshot recovery to complete.
5. Read the recovered candidate set.

**Expected results**

- The source reports stale state before recovery completes.
- The recovered current view does not contain the deleted advertisement.
- Recovery removes stale entries even when their individual delete events were missed.

**References:** [Beacon: View recovery](docs/beacon.md#83-view-recovery),
[Deckr NATS binding: Materialized sources and recovery](docs/deckr-nats-binding.md#8-materialized-sources-and-recovery).

### DIT-MAT-002: Restart rebuilds Beacon state before candidate reads

**Setup**

- Keep a hardware manager's advertisement active.
- Start a controller and wait until it observes that advertisement.

**Actions**

1. Stop the controller.
2. Start a new controller runtime with the same endpoint address.
3. Query discovery while its initial snapshot is incomplete.
4. Allow the snapshot to complete and query discovery again.

**Expected results**

- A read waits for current state or reports that current state is unavailable.
- An incomplete snapshot is not presented as an authoritative empty candidate set.
- After startup recovery, the current candidate set contains the active hardware manager.

**References:** [Beacon: Runtime view and watches](docs/beacon.md#8-runtime-view-and-watches),
[Deckr NATS binding: Currentness](docs/deckr-nats-binding.md#82-currentness).

### DIT-MAT-003: Missed Beacon put is recovered without per-query full scans

**Setup**

- Start a producer and a consumer with a current Beacon view.
- Record exact scan counts and broker consumer counts.

**Actions**

1. Interrupt the consumer's watch so that it misses a new advertisement.
2. Publish that advertisement.
3. Cause a specified currentness failure and hold snapshot recovery incomplete.
4. Make 100 ordinary discovery queries.
5. Restore delivery and let recovery complete.
6. Query the recovered candidate set.

**Expected results**

- Ordinary queries do not perform a full store scan for each call.
- Queries do not present incomplete source state as current.
- Repeated queries do not accumulate temporary broker consumers.
- The recovered current view contains the advertisement.

**References:** [Beacon: Runtime view and watches](docs/beacon.md#8-runtime-view-and-watches),
[Deckr NATS binding: Broker resource lifecycle](docs/deckr-nats-binding.md#10-broker-resource-lifecycle).

### DIT-MAT-004: Missed Concord token deletion is recovered

**Setup**

- Maintain a valid managed hardware claim with current contract and token sources.
- Confirm that protected operations can use the held local authority.
- Keep optional maintenance scans stopped during this scenario.

**Actions**

1. Interrupt the token watch so that it misses the hardware manager's token deletion.
2. Delete that token through fault injection.
3. Cause a specified currentness failure and observe the stale transition.
4. Try a protected operation through the held agreement.
5. Restore watch delivery and allow snapshot recovery.
6. Validate the recovered contract records independently of the terminated lease.
7. Check the previous agreement's local authority.

**Expected results**

- Loss of source currentness immediately stops the affected local authority.
- The protected operation is not authorized through that agreement.
- Recovery removes the missing token from the cached observations.
- If the contract remains open, validation reports `missing_token`.
- If lifecycle cleanup cancels the contract, validation reports `cancelled`.
- Recovery does not restore the terminated local authority.

**References:** [Concord: Permanent loss of authority](docs/concord.md#9-permanent-loss-of-authority),
[Concord: Cached and exact evaluation](docs/concord.md#123-cached-and-exact-evaluation),
[Deckr NATS binding: Materialized sources and recovery](docs/deckr-nats-binding.md#8-materialized-sources-and-recovery).

### DIT-MAT-005: Snapshot reconciliation synthesizes missing tombstones

**Setup**

- Maintain an active service-use lease between a provider and consumer.
- Publish a protected view entry that matches the lease's fence.
- Wait until the consumer sees its payload.
- Keep Concord participation current while interrupting only the service-view source.

**Actions**

1. Pause the consumer's service-view watch.
2. Delete the entry while the watch is paused.
3. Resume the watch through snapshot recovery.
4. Wait until the service-view source is current.
5. Read the entry through the same authorized view context.

**Expected results**

- The entry is absent from the recovered view.
- The old payload does not survive because its delete event was missed.
- Snapshot reconciliation removes cached keys that are absent from the store.

**References:** [Deckr NATS binding: Snapshot barrier](docs/deckr-nats-binding.md#81-snapshot-barrier),
[Deckr services: Cached reads, writes, and recovery](docs/deckr-services.md#cached-reads-writes-and-recovery).

## Service view tests

### DIT-SVC-001: Protected view fence hides replacement payloads

**Setup**

- Maintain a valid service-use lease and a protected watch for one view key.
- Read the watch's initial snapshot.
- Publish a matching entry and wait until the watcher sees its payload.
- Keep the agreement's tokens and sessions unchanged throughout the test.

**Actions**

1. Replace the entry at the same physical store key through fault injection.
2. Give the replacement a different service identity or service session and a distinguishable payload.
3. Keep the other fence values unchanged.
4. Read the resulting entry change from the watch.

**Expected results**

- The watcher receives a removal-style event for the previously visible entry.
- The watcher does not receive the replacement payload.
- The matching key alone does not authorize the replacement.

**References:** [Deckr services: Watch visibility](docs/deckr-services.md#watch-visibility),
[Service-view key vectors](contract/v1/vectors/service-view-keys.v1.json).

### DIT-SVC-002: Delete for never-visible entry is suppressed

**Setup**

- Maintain a valid service-use lease and a protected watch for an empty view key.
- Read the initial snapshot before checking entry changes.
- Keep the lease and view source current.

**Actions**

1. Inject an entry at the watched storage key with a different service identity or session.
2. Wait for the source to observe the injected revision.
3. Delete the entry and wait for the source to observe its removal.
4. Check the protected watch for entry payloads and removal events.
5. Repeat with broker expiry if the service-view bucket policy permits expiry.

**Expected results**

- The mismatching entry is never visible under the watch's fence.
- Its payload is not sent to the watcher.
- Its deletion or expiry does not produce an entry removal event for that watcher.

**Note:** The initial snapshot and source currentness notifications are separate from entry removal events.

**References:** [Deckr services: Watch visibility](docs/deckr-services.md#watch-visibility).

## Operational tests

### DIT-OPS-001: Steady-state consumer count is bounded

**Setup**

- Start a broker with consumer-count observations.
- Populate advertisements, valid agreements, participant tokens, and authorized service-view entries.
- Start the applicable managed views and wait until they are current.
- Record the baseline consumer count and the implementation's expected resource bound.

**Actions**

1. Repeat ordinary discovery, contract validation, and service-view reads 1,000 times.
2. Observe consumer counts during and after the reads.
3. Allow pending operations to finish.
4. Check for remaining temporary consumers.

**Expected results**

- Consumer counts remain within the declared bound and stabilize after the workload.
- Ordinary reads reuse managed materialized views.
- Temporary watch and list consumers are explicitly removed after use.
- Server-side inactive cleanup is only a fallback.

**References:** [Deckr NATS binding: Broker resource lifecycle](docs/deckr-nats-binding.md#10-broker-resource-lifecycle).

### DIT-OPS-002: Heartbeat refresh cadence is TTL-governed

**Setup**

- Configure the advertisement bucket TTL to 300 seconds and the participant-token bucket TTL to 120 seconds.
- Start a managed advertisement and a valid agreement with active participant refreshes.
- Keep advertisement content and token identity unchanged during cadence observations.
- Observe scheduled heartbeat attempts and committed revisions separately.

**Actions**

1. Observe at least two scheduled refresh intervals for each record type.
2. Make frequent ordinary reads during that interval.
3. If an advertisement refresh request is exposed, request refresh repeatedly.
4. Compare scheduling and writes with the observed bucket TTLs.
5. Change the advertisement payload and observe publication of that change.

**Expected results**

| Observation | Expected result |
| --- | --- |
| Advertisement `ttlSeconds` | `300`, from the observed advertisement bucket TTL. |
| Advertisement heartbeat schedule | Jitter between 150 and 225 seconds. |
| Participant-token `ttlSeconds` | `120`, from the observed token bucket TTL. |
| Participant-token heartbeat schedule | Jitter between 60 and 90 seconds. |
| Frequent caller activity | No caller-selected token TTL or refresh interval. No unnecessary unchanged advertisement writes. |
| Advertisement payload change | Publication occurs immediately, independently of the heartbeat interval. |

**Note:** A scheduled refresh can omit an unnecessary write only under the owning protocol's freshness rules.
Concord requires confirmed renewal evidence for the same held token.
An omitted write must not hide a missed required heartbeat or extend its deadline.

**References:** [Beacon: Schedule refreshes](docs/beacon.md#64-schedule-refreshes),
[Concord: Token refresh](docs/concord.md#8-token-refresh),
[Concord: Heartbeat deadlines](docs/concord.md#91-heartbeat-deadlines).

### DIT-OPS-003: Revision guards prevent deleting another owner's state

**Setup**

- Create an advertisement or participant token owned by the runtime under test.
- Retain its identity and store revision.
- Run the scenario separately for advertisements and participant tokens.

**Actions**

1. Start cleanup and pause it after its exact record read.
2. Replace the record with a newer revision through fault injection.
3. Change the session, ownership, or token identity in the replacement.
4. Resume the original cleanup operation.
5. Read the exact key again.

**Expected results**

- The stale cleanup operation does not delete the replacement record.
- A revision conflict requires a new identity check before another delete attempt.
- Changed ownership prevents deletion through the old handle.
- The final record retains the replacement identity.

**References:** [Beacon: Withdraw an advertisement](docs/beacon.md#63-withdraw-an-advertisement),
[Concord: Mutation errors](docs/concord.md#125-mutation-errors),
[Concord: Clean close](docs/concord.md#101-clean-close).

## Structural and semantic conformance checks

### Structural checks

1. Load the [manifest](contract/v1/manifest.json) from the selected reference revision.
2. Validate every fixture against its declared schema.
3. Use JSON Schema Draft 2020-12 with `date-time` format assertion enabled.
4. Compare each result with the manifest's explicit fixture expectation.
5. Check generic protocol fixtures independently of Deckr, including application-defined endpoint families.

### Semantic checks

1. Load the [semantic vector document](contract/v1/vectors/semantic-validation.v1.json) and select its `cases` array.
2. Apply each case's named [semantic validation rule](docs/semantic-rules.md).
3. Compare structural acceptance with `schemaValid`.
4. Compare semantic acceptance with `semanticValid`.

The reference validator checks the semantic vectors' structural expectations.
Consuming implementations execute the semantic checks and distributed lifecycle scenarios.

### Message and availability checks

For every hardware and Action Runtime operation in the [message catalog](contract/v1/catalogs/messages.v1.json), check these cases:

- The complete message satisfies its schema.
- The payload appears at the required location.
- An incorrect payload type is rejected.
- Sender and recipient policies are enforced.
- Missing or invalid contract identity is rejected.
- Missing or invalid session identity is rejected where the operation requires that identity.

Test Action Runtime availability as both a reusable payload and a stored record.
The stored record must include its complete service-use fence.

## Reporting format

Report each scenario independently under its stable ID.
The report should identify the implementation, wire version, and pinned reference revision.
Include observation limits, clock behavior, and failure details with the test evidence.

| Result | Meaning |
| --- | --- |
| `pass` | The completed scenario met every applicable expected result. |
| `fail` | The scenario did not meet an expected result. |
| `not-applicable` | The implementation does not claim the tested surface or optional behavior. A note explains the exclusion. |

Example report:

```json
{
  "implementation": "example-runtime 0.4.0",
  "deckrContractVersion": "v1",
  "referenceRevision": "<pinned-git-commit>",
  "results": {
    "DIT-KEY-001": "pass",
    "DIT-BCN-003": "pass",
    "DIT-MAT-004": "fail",
    "DIT-SVC-002": "not-applicable"
  },
  "notes": {
    "DIT-MAT-004": "The previous agreement regained local authority after source recovery.",
    "DIT-SVC-002": "This implementation does not claim the protected service-view surface."
  }
}
```

Use `not-applicable` only with an explanation of the excluded surface or optional behavior.
Do not use it to excuse a required behavior within a claimed surface.
The example report is a reporting convention, not an additional wire contract.
