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
participants must treat the Python models and generated `contract/v1` artifacts
as the contract boundary.

## Current Contract

The supported shared stores are:

| Store | Default Bucket | Policy |
| --- | --- | --- |
| Beacon advertisements | `deckr_beacon_advertisement_v1` | TTL-bound |
| Concord contracts | `deckr_concord_contract_v1` | persistent |
| Concord participant tokens | `deckr_concord_token_v1` | TTL-bound |

`StateStore` remains the generic CAS/watch abstraction used by these protocols,
but retired shared coordination buckets are not part of the v1 surface.
Opening a store without an explicit policy creates a persistent generic store.
Beacon and Concord coordinators pass their own `StateStorePolicy` values.

Endpoint sessions are local runtime and message-envelope identities. Lane
publish/subscribe does not consult a KV record before delivery. Runtime evidence
for discovery and agreements lives in Beacon advertisements and Concord tokens.

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

The Python API is `deckr.beacon.BeaconDiscovery`.

```python
beacon = BeaconDiscovery(
    deckr.state(
        DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
        policy=BEACON_ADVERTISEMENT_STORE_POLICY,
    )
)
handle = await beacon.advertise(
    "dev.deckr.hardware",
    "hardware_manager:mirabox-main",
    "manager-session",
    payload=payload,
)
candidates = await beacon.find("dev.deckr.hardware")
```

Beacon answers "which endpoints currently advertise this feature?" It does not
grant ownership, reserve anything, prove future success, or make a command safe.
Consumers should treat results as candidates and establish any needed Concord
agreement before relying on them.

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

The Python API is `deckr.concord.ConcordCoordinator`.

```python
concord = ConcordCoordinator(contract_state, token_state)
contract = await concord.create_contract(
    ("controller:main", "hardware_manager:mirabox-main"),
    profile="dev.deckr.profile.hardware_claim.v1",
    terms=terms,
)
await concord.attach(contract, "controller:main", "controller-session")
validity = await concord.validate(contract)
```

A Concord contract is valid only while the contract is open and every named
participant maintains an acceptable token for the same contract id, generation,
participant, session, and terms hash. Any participant may cancel the contract.

## Deckr Profiles

Deckr core ships these profile contracts:

| Profile | Protocol | Purpose |
| --- | --- | --- |
| `dev.deckr.profile.hardware.v1` | Beacon | hardware devices, controls, capabilities |
| `dev.deckr.profile.actions.v1` | Beacon | action provider actions and requirements |
| `dev.deckr.profile.hardware_claim.v1` | Concord | controller ownership of hardware devices |
| `dev.deckr.profile.action_binding.v1` | Concord | live controller/provider action bindings |

The generic Beacon layer validates only the advertisement envelope. The generic
Concord layer validates only contract and token mechanics. Profile validation
lives in `deckr.profiles`.

Hardware single-owner enforcement is profile/manager policy over valid Concord
claims. Beacon capacity fields are hints; Concord validity is the authority for
whether a claim or binding is live.

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
unready and optional dependencies diagnostic-only. Dependency observation does
not use lane subscription state as an authority source.

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

## Troubleshooting

If a runtime cannot find hardware, actions, or services:

1. Check the relevant Beacon feature id.
2. Check the advertisement endpoint and session id.
3. Check whether the advertisement bucket TTL is expiring records.
4. If a live agreement is expected, validate the Concord contract and every
   participant token.
5. For profile-specific behavior, validate the Beacon payload or Concord terms
   with `deckr.profiles`.

If lane messages are not delivered:

1. Validate the `DeckrMessage` envelope and lane contract.
2. Check sender and recipient endpoint families.
3. Check direct recipient session filters when `recipientSessionId` is set.
4. Confirm NATS subject/header hints match the payload.

## Alpha Rule

Deckr is pre-v1. Removed contracts are removed outright. Do not add aliases,
compatibility shims, fallback parsing, or parallel authority paths for retired
state models.
