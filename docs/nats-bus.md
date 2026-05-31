# NATS Bus

> Live implementation reference: this document describes behavior currently
> implemented in `deckr`. It should stay in sync with code, tests, generated
> schemas, examples, and scripts. If it differs from the implementation, treat
> that as a bug.

Deckr's distributed substrate is NATS:

- Core NATS carries lane messages.
- JetStream KV backs explicit protocol stores.
- Beacon provides weak feature discovery.
- Concord provides live multi-participant agreements.

NATS subjects, headers, streams, and KV buckets are substrate details. Deckr
participants must treat [`beacon-concord.md`](beacon-concord.md), this document,
and the generated `contract/v1` artifacts as the language-neutral contract
boundary. The Python models are the current reference implementation and source
for generated artifacts.

## Current Contract

The supported shared stores are:

| Store | Default Bucket | Policy |
| --- | --- | --- |
| Beacon advertisements | `deckr_beacon_advertisement_v1` | TTL-bound |
| Concord contracts | `deckr_concord_contract_v1` | persistent |
| Concord participant tokens | `deckr_concord_token_v1` | TTL-bound |

`StateStore` remains the generic CAS/watch abstraction below these protocols.
Production runtime code does not subscribe directly to Beacon or Concord
authority state. Python runtime participants use the shared `BeaconService` and
`ConcordService` APIs, which own raw state watches, semantic lifecycle events,
heartbeats, leases, and lifecycle logging. Non-Python implementations must
follow the same protocol semantics in [`beacon-concord.md`](beacon-concord.md).
Retired shared coordination buckets are not part of the v1 surface. Opening a
store without an explicit policy creates a persistent generic store. Beacon and
Concord services pass their own `StateStorePolicy` values.

Endpoint sessions are local runtime and message-envelope identities. Lane
publish/subscribe does not consult a KV record before delivery. Runtime evidence
for discovery lives in Beacon advertisements. Live agreement authority lives in
Concord contracts and participant tokens.

## Lane Subjects

Deckr lane messages are canonical `DeckrMessage` envelopes. The NATS substrate
publishes them under a sender-hinted subject:

```text
deckr.lane.<lane-token>.<sender-family-token>.<sender-id-token>
```

Tokens use the shared key-token rules in `deckr.contracts.keys`:

- safe tokens stay unchanged
- unsafe tokens are URL-safe base64 with a `b64_` prefix

The NATS subject is an optimization for subscription fan-out. The payload is
authoritative, and the substrate validates the subject and headers against the
envelope when reading from NATS.

## Lane Delivery

Components acquire lane handles with:

```python
async with deckr.lane("actions").register_endpoint("action_provider:clock") as lane:
    await lane.send(
        recipient="controller:main",
        subject=entity_subject("settings", contextId="ctx"),
        message_type="settingsRequest",
        body={"target": target},
    )
```

The lane handle stamps `sender` and `senderSessionId`, validates the lane
contract, and publishes the envelope. Subscribers receive messages only when the
envelope recipient matches the local endpoint/session and lane contract.

Message lanes remain ordinary command/data messaging. Beacon and Concord replace
authority for discovery and agreements; they do not replace action messages,
hardware messages, service command messages, or other lane payloads.

## Beacon Store

Beacon advertisements use:

```text
bucket: deckr_beacon_advertisement_v1
key:    advertisements.by_feature.<feature-id-token>.<advertisement-id-token>
schema: dev.deckr.beacon.advertisement.v1
```

The runtime-facing Python API is `deckr.beacon.BeaconService`.

```python
beacon = BeaconService(
    BeaconDiscovery(
        deckr.state(
            DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
            policy=BEACON_ADVERTISEMENT_STORE_POLICY,
        )
    )
)
advertisement = await beacon.ensure_advertisement(
    BeaconAdvertisementSpec(
        feature_id="dev.deckr.hardware",
        endpoint="hardware_manager:mirabox-main",
        session_id="manager-session",
        payload=payload,
        refresh_interval=5.0,
    )
)
handle = await advertisement.publish()
candidates = await beacon.find("dev.deckr.hardware")
```

Beacon answers "which endpoints currently advertise this feature?" It does not
grant ownership, reserve anything, prove future success, or make a command safe.
Consumers should treat results as candidates and establish any needed Concord
agreement before relying on them. After that agreement exists, Beacon
advertisement changes do not withdraw or invalidate it. Advertisers may
withdraw Beacon advertisements when they are not accepting new Concord
negotiations; that affects only future discovery.
The full Beacon semantic contract is specified in
[`beacon-concord.md`](beacon-concord.md#beacon).

Cross-runtime status:
`deckr-adapter-elgato-node` and the Rust manager implementations are still behind
the managed Beacon/Concord lifecycle contracts used by the Python runtime, so they are
not yet considered parity-complete for this contract profile.

## Concord Stores

Concord contracts use:

```text
bucket: deckr_concord_contract_v1
key:    contracts.<contract-id-token>.<generation>.meta
schema: dev.deckr.concord.contract.v1
```

Concord participant tokens use:

```text
bucket: deckr_concord_token_v1
key:    contracts.<contract-id-token>.<generation>.participants.<participant-token>
schema: dev.deckr.concord.participant-token.v1
```

The runtime-facing Python API is `deckr.concord.ConcordService`.

```python
concord = ConcordService(ConcordCoordinator(contract_state, token_state))
contract = await concord.create_contract(
    ("controller:main", "hardware_manager:mirabox-main"),
    profile="dev.deckr.profile.hardware_claim.v1",
    terms=terms,
)
lease = concord.participant_lease(
    contract=contract,
    participant="controller:main",
    session_id="controller-session",
)
await lease.attach_or_refresh()
validity = await concord.validate(contract)
```

A Concord contract is valid only while the contract is open and every named
participant maintains an acceptable token for the same contract id, generation,
participant, session, and terms hash. Any participant may cancel the contract.
The full Concord semantic contract is specified in
[`beacon-concord.md`](beacon-concord.md#concord).

## Deckr Profiles

Deckr core ships these profile contracts:

| Profile | Protocol | Purpose |
| --- | --- | --- |
| `dev.deckr.profile.hardware.v1` | Beacon | hardware devices, controls, capabilities |
| `dev.deckr.profile.actions.v1` | Beacon | action provider actions and requirements |
| `dev.deckr.profile.hardware_claim.v1` | Concord | controller ownership of hardware devices |
| `dev.deckr.profile.action_provider_session.v1` | Concord | live controller/provider runtime sessions |

The generic Beacon layer validates only the advertisement envelope. The generic
Concord layer validates only contract and token mechanics. Action profile
validation lives in `deckr.profiles`; hardware profile validation lives in
`deckr.hardware.profiles` and is exported from `deckr.hardware`.
The language-neutral profile rules are summarized in
[`beacon-concord.md`](beacon-concord.md#profiles).

Hardware single-owner enforcement is profile/manager policy over valid Concord
claims. Beacon capacity fields are hints; Concord validity is the authority for
whether a claim or provider session is live. A missing Beacon advertisement is
not a withdrawal of an existing claim or provider session. Python hardware
managers use the shared `deckr.hardware.runtime.HardwareManagerRuntime`
implementation to advertise hardware through managed
`BeaconService.ensure_advertisement` lifecycles, maintain claim tokens through
`ConcordParticipantManager`, and route input only for live claims.
Hardware device inventory is published through the hardware Beacon profile only.
The `hardware_messages` lane is for control input, commands, capability state,
and replies; it must not be treated as the inventory authority. If a claimed
device disappears, the hardware manager cancels the matching Concord claim or
stops maintaining its participant token.

Service components use `deckr.services.GenericService` to advertise their
package-owned service feature through managed `BeaconService.ensure_advertisement`
lifecycles and maintain service
use tokens through `ConcordParticipantLease`. After a service-use Concord
contract is negotiated, service command and view authority follows Concord, not
continued Beacon advertisement presence. A service may withdraw its Beacon
advertisement when it cannot accept new service-use contracts; existing
service-use contracts remain Concord-governed.

## Component Dependencies

Component dependencies observe Beacon features:

```toml
[deckr.components.instances.worker.dependencies.sonos_home]
kind = "feature"
mode = "required"
feature_id = "org.example.sonos.service"
endpoint = "service:sonos-home"
```

The endpoint filter is optional. A missing candidate makes required dependencies
unready and optional dependencies diagnostic-only. Dependency observation uses
`BeaconService` semantic feature events and does not use lane subscription state
or raw Beacon KV watches as an authority source. Dependency readiness is not
agreement withdrawal; existing Concord contracts must be validated through
Concord.

## Store Configuration

NATS-backed stores are opened with explicit policies:

```python
beacon_state = deckr.state(
    "deckr_beacon_advertisement_v1",
    policy=BEACON_ADVERTISEMENT_STORE_POLICY,
)
contract_state = deckr.state(
    "deckr_concord_contract_v1",
    policy=CONCORD_CONTRACT_STORE_POLICY,
)
token_state = deckr.state(
    "deckr_concord_token_v1",
    policy=CONCORD_TOKEN_STORE_POLICY,
)
```

TTL-bound stores are configured with broker-owned bucket TTL and one retained
message per subject. Persistent stores reject per-write TTL. Reopening the same
bucket with a conflicting policy is an error.

Package-owned private buckets must be owner-qualified and versioned, for
example `com_example_media_cache_v1`. They must not redefine Beacon or Concord
authority.

## Contract Artifacts

The language-neutral bundle in `contract/v1` contains:

- Beacon and Concord JSON Schemas
- Deckr profile schemas
- valid and invalid fixtures
- key-token vectors
- Beacon/Concord key vectors
- canonical Concord terms hash vectors
- NATS lane subject/header vectors

Regenerate the bundle with:

```bash
uv run python scripts/generate_contract_artifacts.py
```

JSON Schemas are structural contracts. Implementations must also enforce the
semantic rules in [`beacon-concord.md`](beacon-concord.md), such as endpoint
parsing, positive counters and TTLs, canonical participant ordering,
terms-hash validation, and profile identity checks.

## Operations

Inspect the broker:

```bash
nats kv info deckr_beacon_advertisement_v1 --server nats://127.0.0.1:4222
nats kv info deckr_concord_contract_v1 --server nats://127.0.0.1:4222
nats kv info deckr_concord_token_v1 --server nats://127.0.0.1:4222

nats kv ls deckr_beacon_advertisement_v1 'advertisements.by_feature.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_contract_v1 'contracts.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_token_v1 'contracts.>' --server nats://127.0.0.1:4222
```

Run the smoke harness:

```bash
uv run --extra nats python scripts/nats_smoke.py --url nats://127.0.0.1:4222 --check-ttl
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```

Run it with a supervised local NATS server:

```bash
uv run --extra supervised-nats python scripts/nats_smoke.py --supervised --check-ttl
```

### JetStream Consumer Hygiene

NATS KV `watch()` and list-style helpers are backed by JetStream consumers.
Beacon and Concord state reads, snapshots, and watches must not create
unbounded growth in unbound broker consumers. Temporary watch/list consumers
must be explicitly deleted or avoided once the read is complete; server-side
inactive cleanup is a fallback, not the steady-state cleanup path.

Treat watch events as wakeups and exact KV reads as authority, but remember that
every watch still consumes broker resources. Use the smoke harness and
`scripts/nats_state_report.py` for current Beacon/Concord keys and TTL behavior,
then pair them with the `/jsz` check below to confirm that steady-state unbound
consumer counts remain bounded.

## Troubleshooting

If a runtime cannot find hardware, actions, or services:

1. Check the relevant Beacon feature id.
2. Check the advertisement endpoint and session id.
3. Check whether the advertisement bucket TTL is expiring records.
4. If a live agreement is expected, validate the Concord contract and every
   participant token; do not treat Beacon disappearance as withdrawal.
5. For profile-specific behavior, validate the Beacon payload or Concord terms
   with `deckr.hardware.profiles` or `deckr.profiles`, depending on the profile.

If lane messages are not delivered:

1. Validate the `DeckrMessage` envelope and lane contract.
2. Check sender and recipient endpoint families.
3. Check direct recipient session filters when `recipientSessionId` is set.
4. Confirm NATS subject/header hints match the payload.

If broker load or JetStream consumer counts grow unexpectedly:

1. Inspect `/jsz?accounts=true&streams=true&consumers=true&config=true` on the
   NATS monitoring endpoint.
2. Separate bound consumers from unbound consumers; unbound consumers commonly
   show no active subscription or `push_bound=false`.
3. Look for stale consumers on Beacon/Concord KV bucket streams, especially
   filters such as `KV_deckr_beacon_advertisement_v1.>` or
   `advertisements.by_feature.>`.
4. Check high-frequency snapshot, reconciliation, watch, and list paths first.
5. Confirm the consumer count stabilizes and drops after the fix, deploy, or
   runtime restart.

## Alpha Rule

Deckr is pre-v1. Removed contracts are removed outright. Do not add aliases,
compatibility shims, fallback parsing, or parallel authority paths for retired
state models.
