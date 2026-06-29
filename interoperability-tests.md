# Deckr Interoperability Tests

> Draft v1 conformance catalog. These scenarios define language-neutral tests
> for implementations of the Deckr v1 contracts. They are intentionally written
> as pseudocode, not Python, Rust, or NATS CLI tests.

This catalog complements the normative and live-reference documents:

- [`docs/beacon-concord.md`](docs/beacon-concord.md)
- [`docs/nats-bus.md`](docs/nats-bus.md)
- [`docs/runtime-architecture.md`](docs/runtime-architecture.md)
- [`contract/v1`](contract/v1)

The goal is to make interoperability enumerable. An implementation should be
able to publish a compatibility matrix such as:

```text
DIT-KEY-001 pass
DIT-BCN-004 fail
DIT-MAT-002 pass
```

Passing JSON Schema validation is not enough. These scenarios cover semantic
rules that are required for independent implementations to behave the same way
when they share a broker, restart, miss watch events, or recover state from the
same KV stores.

## Test ID Rules

IDs are stable once published. Do not rename or reuse an ID for a different
scenario. If a scenario changes incompatibly, retire the old ID and add a new
one.

ID format:

```text
DIT-<AREA>-<NNN>
```

Current areas:

| Area | Meaning |
| --- | --- |
| `KEY` | endpoint addresses, key-token encoding, derived keys |
| `HASH` | canonical JSON and Concord terms hashes |
| `BCN` | Beacon advertisement and discovery semantics |
| `CCD` | Concord contract and participant-token semantics |
| `MAT` | materialized KV views, watches, recovery, and restart behavior |
| `SVC` | service view and protected-view semantics |
| `OPS` | broker resource and operational behavior |

## Shared Harness Terms

The pseudocode assumes the harness can start multiple independent
implementations against the same broker. `ImplA` and `ImplB` may be different
languages, processes, versions, or runtime hosts.

```text
fresh_broker()
start_runtime(implementation, endpoint)
stop_runtime(runtime, mode = clean | crash)
advance_time(seconds)
put_kv(bucket, key, value, ttl = optional)
create_kv(bucket, key, value, ttl = optional)
update_kv(bucket, key, value, expected_revision, ttl = optional)
delete_kv(bucket, key, expected_revision = optional)
exact_get(bucket, key) -> record | missing
list_keys(bucket, prefix) -> keys
watch_drop(runtime, bucket, count | predicate)
watch_pause(runtime, bucket)
watch_resume(runtime, bucket)
assert_eventually(condition, timeout)
assert_never(condition, duration)
```

Exact syntax is not normative. Observable behavior is normative.

## Key And Hash Tests

### DIT-KEY-001: Endpoint Address Acceptance

Spec source: `docs/beacon-concord.md#shared-rules`

Scenario: implementations accept valid endpoint families and ids and reject
malformed endpoint addresses before writing protocol state.

Pseudocode:

```text
for address in [
  "controller:main",
  "hardware_manager:mirabox-main",
  "action_provider:clock.1",
  "service:sonos_home"
]:
  assert parse_endpoint(address).ok

for address in [
  "",
  "controller:",
  ":main",
  "controller:main:extra",
  " controller:main",
  "controller:main ",
  "action_provider:dev.deckr.controller.builtin"
]:
  assert parse_endpoint(address).error
  assert implementation refuses Beacon or Concord writes using address
```

Pass criteria: invalid endpoint addresses never appear in Beacon
advertisements, Concord contracts, participant tokens, service views, or lane
envelopes emitted by the implementation.

### DIT-KEY-002: Key-Token Vector Compatibility

Spec source: `docs/beacon-concord.md#shared-rules`,
`contract/v1/vectors/key-tokens.v1.json`

Scenario: all implementations derive identical key tokens for the same logical
input.

Pseudocode:

```text
vectors = load("contract/v1/vectors/key-tokens.v1.json")

for vector in vectors:
  assert encode_key_token(vector.input) == vector.encoded
  assert decode_key_token(vector.encoded) == vector.input
```

Pass criteria: every vector matches exactly, including `b64_` escaping and
padding removal behavior.

### DIT-KEY-003: Beacon And Concord Key Vector Compatibility

Spec source: `docs/nats-bus.md#beacon-store`,
`docs/nats-bus.md#concord-stores`,
`contract/v1/vectors/beacon-concord-keys.v1.json`

Scenario: independent implementations read and write the same logical records
under the same KV keys.

Pseudocode:

```text
vectors = load("contract/v1/vectors/beacon-concord-keys.v1.json")

for vector in vectors:
  assert derive_protocol_key(vector.input) == vector.key
```

Pass criteria: the derived keys match the vector byte-for-byte.

### DIT-HASH-001: Concord Terms Hash Compatibility

Spec source: `docs/beacon-concord.md#shared-rules`,
`contract/v1/vectors/concord-terms-hash.v1.json`

Scenario: all implementations compute the same canonical hash for equivalent
Concord terms.

Pseudocode:

```text
vectors = load("contract/v1/vectors/concord-terms-hash.v1.json")

for vector in vectors:
  assert concord_terms_hash(vector.terms) == vector.termsHash
```

Pass criteria: hashes match exactly, including sorted object keys, compact JSON,
UTF-8 text, and `sha256:<hex>` serialization.

## Beacon Tests

### DIT-BCN-001: Advertisement Create, Refresh, And Withdraw

Spec source: `docs/beacon-concord.md#beacon`

Scenario: one implementation advertises a feature and another discovers it as a
candidate, then observes withdrawal.

Pseudocode:

```text
broker = fresh_broker()
advertiser = start_runtime(ImplA, "hardware_manager:n4")
consumer = start_runtime(ImplB, "controller:main")

handle = advertiser.beacon.advertise(
  featureId = "dev.deckr.hardware",
  endpoint = "hardware_manager:n4",
  sessionId = "n4-session",
  ttlSeconds = 30,
  payload = valid_hardware_profile_payload()
)

assert_eventually(
  consumer.beacon.candidates("dev.deckr.hardware")
    contains endpoint "hardware_manager:n4",
  timeout = 5
)

handle.refresh()
assert exact_get(beacon_bucket, handle.key).value.refreshSeq > 1

handle.withdraw()
assert_eventually(
  consumer.beacon.candidates("dev.deckr.hardware")
    does_not_contain endpoint "hardware_manager:n4",
  timeout = 5
)
```

Pass criteria: discovery appears after create or refresh and disappears after
withdrawal or TTL expiry.

### DIT-BCN-002: Advertisement Identity And Key Agreement

Spec source: `docs/beacon-concord.md#beacon`

Scenario: an implementation rejects a Beacon record whose key and payload
identity disagree.

Pseudocode:

```text
broker = fresh_broker()
consumer = start_runtime(ImplB, "controller:main")

put_kv(
  beacon_bucket,
  key = "advertisements.by_feature.dev.deckr.hardware.ad-1",
  value = valid_advertisement(
    advertisementId = "ad-2",
    featureId = "dev.deckr.hardware"
  )
)

assert_never(
  consumer.beacon.candidates("dev.deckr.hardware")
    contains advertisementId "ad-2",
  duration = 5
)
```

Pass criteria: mismatched records are ignored or surfaced as invalid
diagnostics, but never returned as usable candidates.

### DIT-BCN-003: Beacon Is Discovery Only

Spec source: `docs/beacon-concord.md#discovery-and-agreement-boundary`

Scenario: removing a Beacon advertisement after a Concord agreement exists does
not invalidate that agreement.

Pseudocode:

```text
broker = fresh_broker()
controller = start_runtime(ImplA, "controller:main")
manager = start_runtime(ImplB, "hardware_manager:n4")

ad = manager.beacon.advertise(valid_hardware_advertisement())
claim = negotiate_hardware_claim(controller, manager, deviceId = "n4")

assert controller.concord.validate(claim.contract).valid

ad.withdraw()

assert_eventually(
  controller.beacon.candidates("dev.deckr.hardware")
    does_not_contain endpoint "hardware_manager:n4",
  timeout = 5
)

assert controller.concord.validate(claim.contract).valid
```

Pass criteria: continued live authority depends on Concord contract and
participant tokens only.

### DIT-BCN-004: Startup Cleanup Does Not Revoke Existing Concord Claims

Spec source: `docs/beacon-concord.md#beacon`,
`docs/beacon-concord.md#profiles`

Scenario: a restarted advertiser removes stale same-feature Beacon records for
its endpoint without cancelling or invalidating an existing Concord claim.

Pseudocode:

```text
broker = fresh_broker()
controller = start_runtime(ImplA, "controller:main")
manager = start_runtime(ImplB, "hardware_manager:n4")

old_ad = manager.beacon.advertise(valid_hardware_advertisement(sessionId = "old"))
claim = negotiate_hardware_claim(controller, manager, deviceId = "n4")
assert controller.concord.validate(claim.contract).valid

stop_runtime(manager, mode = crash)
manager2 = start_runtime(ImplB, "hardware_manager:n4")
manager2.beacon.advertise(valid_hardware_advertisement(sessionId = "new"))

assert_eventually(exact_get(beacon_bucket, old_ad.key) == missing, timeout = 5)
assert controller.concord.validate(claim.contract).valid
```

Pass criteria: Beacon cleanup affects future discovery only. Concord validity is
unchanged while valid participant tokens remain.

## Concord Tests

### DIT-CCD-001: Contract Is Not Valid Until All Participants Attach

Spec source: `docs/beacon-concord.md#concord`

Scenario: creating an open contract with no attached participants does not
grant authority.

Pseudocode:

```text
broker = fresh_broker()
controller = start_runtime(ImplA, "controller:main")
manager = start_runtime(ImplB, "hardware_manager:n4")

contract = controller.concord.propose(
  participants = ["controller:main", "hardware_manager:n4"],
  localParticipant = "controller:main",
  localSessionId = "controller-session"
)

validity = manager.concord.validate_exact(contract)

assert validity.status == "not_yet_fulfilled"
assert validity.valid == false
```

Pass criteria: implementations do not treat an open contract record alone as
valid authority.

### DIT-CCD-002: Participant Token Ownership

Spec source: `docs/beacon-concord.md#concord`

Scenario: a participant must write only its own token, and validation rejects a
token whose payload participant does not match the participant key.

Pseudocode:

```text
broker = fresh_broker()
contract = create_open_contract(
  participants = ["controller:main", "hardware_manager:n4"],
  attachedParticipants = ["controller:main", "hardware_manager:n4"]
)

put_kv(
  token_bucket,
  key = token_key(contract, participant = "hardware_manager:n4"),
  value = valid_token(
    contract = contract,
    participant = "controller:main"
  )
)

validity = validate_exact(contract)

assert validity.valid == false
assert validity.status == "invalid_token"
```

Pass criteria: token key identity and token payload identity must agree.

### DIT-CCD-003: Missing Attached Token Invalidates Authority

Spec source: `docs/beacon-concord.md#concord`

Scenario: once a participant is attached, losing that participant's token makes
the contract invalid for that generation.

Pseudocode:

```text
broker = fresh_broker()
claim = create_fully_attached_claim(
  participants = ["controller:main", "hardware_manager:n4"]
)

assert validate_exact(claim.contract).valid

delete_kv(token_bucket, token_key(claim.contract, "hardware_manager:n4"))

validity = validate_exact(claim.contract)

assert validity.valid == false
assert validity.status == "missing_token"
```

Pass criteria: an attached participant's missing token is loss of authority,
not a temporary unknown state.

### DIT-CCD-004: Cancelled Contract Is Terminal

Spec source: `docs/beacon-concord.md#concord`

Scenario: a cancelled contract does not become valid again even if tokens are
refreshed or recreated.

Pseudocode:

```text
broker = fresh_broker()
claim = create_fully_attached_claim()

cancel_contract(claim.contract, cancelledBy = "controller:main")
refresh_or_recreate_all_tokens(claim.contract)

validity = validate_exact(claim.contract)

assert validity.valid == false
assert validity.status == "cancelled"
```

Pass criteria: recovery uses a successor contract, not resurrection of a
cancelled generation.

### DIT-CCD-005: Terms Hash Mismatch Is Invalid

Spec source: `docs/beacon-concord.md#concord`

Scenario: a token with a different terms hash from the contract does not
maintain authority.

Pseudocode:

```text
broker = fresh_broker()
contract = create_open_contract(
  terms = {"profile": "dev.deckr.profile.hardware_claim.v1", "deviceIds": ["n4"]},
  termsHash = hash_of_terms_above
)

attach_participant(
  contract,
  participant = "controller:main",
  tokenTermsHash = contract.termsHash
)
attach_participant(
  contract,
  participant = "hardware_manager:n4",
  tokenTermsHash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
)

validity = validate_exact(contract)

assert validity.valid == false
assert validity.status == "terms_hash_mismatch"
```

Pass criteria: the contract terms hash is enforced consistently for every
participant token.

### DIT-CCD-006: Exact Validation Bypasses Stale Local Views

Spec source: `docs/beacon-concord.md#concord`,
`docs/nats-bus.md#jetstream-consumer-hygiene`

Scenario: exact validation reads current KV state even when a runtime's
materialized view missed a token deletion.

Pseudocode:

```text
broker = fresh_broker()
runtime = start_runtime(ImplA, "controller:main")
claim = create_fully_attached_claim()

watch_drop(runtime, token_bucket, predicate = next_delete_for_claim_token)
delete_kv(token_bucket, token_key(claim.contract, "hardware_manager:n4"))

cached_result = runtime.concord.validate(claim.contract)
exact_result = runtime.concord.validate_exact(claim.contract)

assert exact_result.valid == false
assert exact_result.status == "missing_token"
assert cached_result.valid == false or cached_result.status == "unavailable"
```

Pass criteria: strict validation must not be satisfied by stale materialized
state. If the normal hot path cannot prove currentness, it reports unavailable
or refreshes before answering.

## Materialized View And Restart Tests

### DIT-MAT-001: Watch Events Are Wakeups, Exact State Is Recoverable

Spec source: `docs/beacon-concord.md#shared-rules`,
`docs/nats-bus.md#jetstream-consumer-hygiene`

Scenario: a runtime that misses a watch event detects that its view is not
current and can recover from exact bucket state.

Pseudocode:

```text
broker = fresh_broker()
consumer = start_runtime(ImplA, "controller:main")
producer = start_runtime(ImplB, "hardware_manager:n4")

producer.beacon.advertise(valid_hardware_advertisement(advertisementId = "ad-1"))
assert_eventually(consumer.beacon.candidates("dev.deckr.hardware") contains "ad-1")

watch_drop(consumer, beacon_bucket, predicate = next_delete_for("ad-1"))
producer.beacon.withdraw("ad-1")

assert consumer.beacon.is_current == false

consumer.beacon.wait_current()

assert consumer.beacon.is_current == true
assert consumer.beacon.candidates("dev.deckr.hardware") does_not_contain "ad-1"
```

Pass criteria: missed deletes, expires, or puts do not leave a permanently stale
candidate cache.

### DIT-MAT-002: Restart Rebuilds Beacon State Before Candidate Reads

Spec source: `docs/nats-bus.md#beacon-store`,
`docs/nats-bus.md#troubleshooting`

Scenario: after a controller restart, hardware candidates are read only after
the Beacon view has reached current state or the read reports unavailable.

Pseudocode:

```text
broker = fresh_broker()
manager = start_runtime(ImplA, "hardware_manager:n4")
manager.beacon.advertise(valid_hardware_advertisement())

controller = start_runtime(ImplB, "controller:main")
stop_runtime(controller, mode = crash)

manager.beacon.refresh()
controller2 = start_runtime(ImplB, "controller:main")

result = controller2.discover_hardware_candidates("dev.deckr.hardware")

assert result contains endpoint "hardware_manager:n4"
assert controller2.beacon.is_current == true
```

Pass criteria: restart does not produce an empty candidate set from a stale or
not-yet-ready materialized view.

### DIT-MAT-003: Missed Beacon Put Is Recovered Without Per-Query Full Scans

Spec source: `docs/nats-bus.md#jetstream-consumer-hygiene`

Scenario: an implementation recovers from a missed advertisement create/update
without performing a full KV scan on every `candidates` call.

Pseudocode:

```text
broker = fresh_broker()
consumer = start_runtime(ImplA, "controller:main")
producer = start_runtime(ImplB, "hardware_manager:n4")

watch_drop(consumer, beacon_bucket, predicate = next_put_for_feature("dev.deckr.hardware"))
producer.beacon.advertise(valid_hardware_advertisement(advertisementId = "ad-1"))

repeat 100 times:
  result = consumer.beacon.candidates("dev.deckr.hardware")
  assert result == unavailable or result does_not_contain "ad-1"

assert broker.temporary_consumer_count(beacon_bucket) remains_bounded

consumer.beacon.wait_current()
assert consumer.beacon.candidates("dev.deckr.hardware") contains "ad-1"
```

Pass criteria: hot-path reads do not create unbounded watch/list consumers or
scan the whole bucket per query. Recovery occurs at explicit currentness or
reconciliation boundaries.

### DIT-MAT-004: Missed Concord Token Deletion Is Recovered

Spec source: `docs/beacon-concord.md#concord`,
`docs/nats-bus.md#concord-stores`

Scenario: a missed token deletion must not allow a stale materialized Concord
view to continue granting authority.

Pseudocode:

```text
broker = fresh_broker()
runtime = start_runtime(ImplA, "controller:main")
claim = create_fully_attached_claim()
assert runtime.concord.validate(claim.contract).valid

watch_drop(runtime, token_bucket, predicate = next_delete_for_manager_token)
delete_kv(token_bucket, token_key(claim.contract, "hardware_manager:n4"))

result = runtime.concord.validate(claim.contract)

assert result.valid == false or result.status == "unavailable"

runtime.concord.wait_current()
result_after_recovery = runtime.concord.validate(claim.contract)

assert result_after_recovery.valid == false
assert result_after_recovery.status == "missing_token"
```

Pass criteria: a stale token cache is detected and repaired before authority is
granted.

### DIT-MAT-005: Snapshot Reconciliation Synthesizes Missing Tombstones

Spec source: `docs/nats-bus.md#jetstream-consumer-hygiene`

Scenario: a recovered watch snapshot reconciles cached keys that are absent
from the current bucket.

Pseudocode:

```text
broker = fresh_broker()
runtime = start_runtime(ImplA, "controller:main")
producer = start_runtime(ImplB, "service:music")

producer.put_view_entry(key = "zones.living_room", value = "playing")
assert_eventually(runtime.service_view.get("zones.living_room") == "playing")

watch_pause(runtime, service_view_bucket)
producer.delete_view_entry(key = "zones.living_room")
watch_resume(runtime, service_view_bucket)

runtime.service_view.wait_current()

assert runtime.service_view.get("zones.living_room") == missing
```

Pass criteria: missed deletes or expiries do not leave stale local entries after
snapshot recovery.

## Service View Tests

### DIT-SVC-001: Protected View Fence Hides Replacement Payloads

Spec source: `docs/beacon-concord.md#profiles`,
`docs/nats-bus.md#deckr-profiles`

Scenario: a protected watcher sees a removal when a visible entry is replaced
by another service identity or session, and does not see the replacement
payload.

Pseudocode:

```text
broker = fresh_broker()
client = start_runtime(ImplA, "controller:main")
service1 = start_runtime(ImplB, "service:music")
service2 = start_runtime(ImplB, "service:music")

lease = negotiate_service_use(client, service1, sessionId = "session-1")
watch = client.service_view.watch(
  key = "zones.living_room",
  protectedBy = lease
)

service1.put_protected_view(
  key = "zones.living_room",
  serviceId = "music",
  sessionId = "session-1",
  payload = {"state": "playing"}
)
assert watch.next().payload.state == "playing"

service2.put_protected_view(
  key = "zones.living_room",
  serviceId = "music",
  sessionId = "session-2",
  payload = {"state": "stopped"}
)

event = watch.next()

assert event.kind == "removed"
assert event.payload is absent
```

Pass criteria: protected views are fenced by the service-use contract identity
and session, not just by key.

### DIT-SVC-002: Delete For Never-Visible Entry Is Suppressed

Spec source: `docs/beacon-concord.md#profiles`

Scenario: a watcher does not receive delete or expire events for entries that
were never visible under its service-use fence.

Pseudocode:

```text
broker = fresh_broker()
client = start_runtime(ImplA, "controller:main")
service = start_runtime(ImplB, "service:music")
other = start_runtime(ImplB, "service:other")

lease = negotiate_service_use(client, service, sessionId = "session-1")
watch = client.service_view.watch("zones.kitchen", protectedBy = lease)

other.put_protected_view(
  key = "zones.kitchen",
  serviceId = "other",
  sessionId = "other-session",
  payload = {"state": "playing"}
)
other.delete_view_entry("zones.kitchen")

assert_never(watch.has_event(), duration = 3)
```

Pass criteria: protected watchers do not learn payloads or lifecycle events for
entries outside their fence.

## Operational Tests

### DIT-OPS-001: Steady-State Consumer Count Is Bounded

Spec source: `docs/nats-bus.md#jetstream-consumer-hygiene`

Scenario: repeated discovery, validation, and service-view reads do not create
unbounded JetStream watch/list consumers.

Pseudocode:

```text
broker = fresh_broker(with_monitoring = true)
runtime = start_runtime(ImplA, "controller:main")
seed_beacon_contracts_tokens_and_service_views()

baseline = broker.consumer_count()

repeat 1000 times:
  runtime.beacon.candidates("dev.deckr.hardware")
  runtime.concord.validate(existing_contract)
  runtime.service_view.get(existing_key)

after = broker.consumer_count()

assert after - baseline <= implementation_declared_bound
assert no_unbound_consumers_accumulate_after_idle(timeout = 30)
```

Pass criteria: normal hot paths reuse managed materialized views or exact reads
that clean up after themselves. Consumer count stabilizes.

### DIT-OPS-002: Heartbeat Refresh Cadence Is TTL-Governed

Spec source: `docs/beacon-concord.md#beacon`,
`docs/beacon-concord.md#concord`

Scenario: aggressive caller refresh loops do not write Beacon advertisements or
Concord participant tokens faster than the protocol cadence permits.

Pseudocode:

```text
broker = fresh_broker()
runtime = start_runtime(ImplA, "hardware_manager:n4")

ad = runtime.beacon.advertise(refreshInterval = 0.1)
token = runtime.concord.attach_token(refreshInterval = 0.1)

for duration long_enough_to_observe_two_refreshes:
  ad.refresh()
  token.refresh()
  sleep(0.1)

assert write_intervals(ad.key) are between 150 and 225 seconds
assert write_intervals(token.key) are between 60 and 90 seconds
```

Pass criteria: Beacon derives `ttlSeconds` from the 300-second Beacon KV
bucket TTL and schedules unchanged heartbeat writes with jitter between
`ttlSeconds * 0.5` and `ttlSeconds * 0.75`. Concord does the same from the
120-second participant-token bucket TTL. Real payload or token changes still
publish immediately.

### DIT-OPS-003: Revision Guards Prevent Deleting Another Owner's State

Spec source: `docs/beacon-concord.md#shared-rules`,
`docs/beacon-concord.md#concord`

Scenario: cleanup code does not delete a replacement advertisement or token
that changed owner, session, token id, or revision after the cleanup process
read it.

Pseudocode:

```text
broker = fresh_broker()
runtime = start_runtime(ImplA, "hardware_manager:n4")

old = create_kv(beacon_bucket, key, advertisement(sessionId = "old"))
cleanup_read = exact_get(beacon_bucket, key)

update_kv(
  beacon_bucket,
  key,
  advertisement(sessionId = "new"),
  expected_revision = old.revision
)

cleanup_attempt = runtime.cleanup_stale_advertisement(cleanup_read)

assert cleanup_attempt.did_not_delete
assert exact_get(beacon_bucket, key).value.sessionId == "new"
```

Pass criteria: all cleanup deletes are revision-guarded and owner-checked.

## Reporting Format

Implementations should report each scenario independently:

```json
{
  "implementation": "example-runtime 0.4.0",
  "deckrContractVersion": "v1",
  "results": {
    "DIT-KEY-001": "pass",
    "DIT-BCN-003": "pass",
    "DIT-MAT-004": "fail",
    "DIT-OPS-001": "not-applicable"
  },
  "notes": {
    "DIT-OPS-001": "runtime uses exact reads only and owns no long-lived watches"
  }
}
```

`not-applicable` should be rare and must include a note explaining which
protocol surface the implementation does not claim to implement.
