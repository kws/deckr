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
| Concord maintenance observations | `deckr_concord_maintenance_v1` | persistent |

Beacon and Concord use explicit KV bucket policies and materialized views.
Production runtime code does not subscribe directly to Beacon or Concord
authority state. Python runtime participants use the shared `Beacon` and
`Concord` APIs; both own materialized KV views, semantic lifecycle events,
leases, heartbeats, freshness checks, recovery reconciliation, and lifecycle
logging. Non-Python implementations must follow the same protocol semantics in
[`beacon-concord.md`](beacon-concord.md).
The optional Concord reaper is the maintenance exception. It is a standalone,
low-frequency component that uses exact raw KV scans instead of long-lived
materialized views.
Retired shared coordination buckets are not part of the v1 surface. Opening a
generic state store is no longer part of the Python runtime. Beacon and Concord
open their explicit JetStream KV bucket policies directly and serve normal reads
from materialized views.
TTL-bound heartbeats are core-governed. Caller-provided refresh intervals are
requests, not guaranteed write cadences. Beacon advertisements derive
`ttlSeconds` from the Beacon KV bucket TTL and schedule managed heartbeat
refreshes with jitter between `ttlSeconds * 0.5` and `ttlSeconds * 0.75`; with
the default 300-second Beacon bucket TTL this produces 150-225 second
advertisement refreshes.
Concord participant-token writes derive `ttlSeconds` from the token KV bucket
TTL and schedule refreshes with jitter between `ttlSeconds * 0.5` and
`ttlSeconds * 0.75`; with the default 120-second token bucket TTL this produces
60-90 second token refreshes. Runtime participants may call refresh
methods more often, but no-op heartbeats are coalesced. If reconciliation
observes fresher same-session token details, the local lease adopts them without
immediately writing again.

Endpoint sessions are local runtime and message-envelope identities. Lane
publish/subscribe does not consult a KV record before delivery. Runtime evidence
for discovery lives in Beacon advertisements. Live agreement authority lives in
Concord contracts and participant tokens. Closing an endpoint session closes
local lane subscriptions opened by that session; it does not withdraw Beacon
advertisements or cancel Concord agreements.

## Lane Subjects

Deckr lane messages are canonical `DeckrMessage` envelopes. The NATS substrate
publishes them under recipient-hinted subjects:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>
deckr.msg.<lane-token>.broadcast.<scope-token>.<endpoint-family-token>
```

Tokens use the shared key-token rules in `deckr.contracts.keys`:

- safe tokens stay unchanged
- unsafe tokens are URL-safe base64 with a `b64_` prefix

The NATS subject is an optimization for subscription fan-out. The payload is
authoritative, and the substrate validates the subject and headers against the
envelope when reading from NATS. Endpoint subscriptions attach only to the
endpoint's direct subject and the matching broadcast-family subject for each
lane.

## Lane Delivery

Components acquire endpoint sessions with:

```python
async with deckr.endpoint("action_provider:clock") as endpoint:
    await endpoint.send(
        lane="actions",
        recipient="controller:main",
        subject=entity_subject("settings", contextId="ctx"),
        message_type="settingsRequest",
        body={"target": target},
    )
```

The endpoint session stamps `sender` and `senderSessionId`, validates the message
contract for the requested lane through the message bus, and publishes the
envelope. Subscribers receive messages only when the envelope recipient matches
the local endpoint/session and message contract.

Request/reply is a message-bus operation over ordinary `DeckrMessage`
envelopes. The requester publishes the request with a NATS reply inbox and
accepts the first deliverable reply whose `inReplyTo` matches the request
`messageId` and whose optional acceptance predicate passes. Rejected, invalid,
or non-deliverable replies are ignored until timeout. Replies sent through
`EndpointSession.reply_to(...)` target the original request sender and
`senderSessionId`.

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

The runtime-facing Python API is `deckr.beacon.Beacon`.

```python
beacon = deckr.beacon
advertisement = await beacon.advertise(
    BeaconAdvertisementSpec(
        feature_id="dev.deckr.hardware",
        endpoint="hardware_manager:mirabox-main",
        session_id="manager-session",
        payload=payload,
    )
)
handle = advertisement.handle
candidates = beacon.candidates("dev.deckr.hardware")
```

Beacon answers "which endpoints currently advertise this feature?" It does not
grant ownership, reserve anything, prove future success, or make a command safe.
Consumers should treat results as candidates and establish any needed Concord
agreement before relying on them. After that agreement exists, Beacon
advertisement changes do not withdraw or invalidate it. Advertisers may
withdraw Beacon advertisements when they are not accepting new Concord
negotiations; that affects only future discovery. The Python `Beacon` runtime
keeps one materialized advertisement view so `candidates(...)`, `get(...)`, and
feature watches avoid per-query full key scans.
Managed `Beacon.advertise(...)` performs best-effort same
feature/advertiser/endpoint startup cleanup by default, using revision-guarded
deletes for stale advertisements left by crashed sessions or changed
configuration. Real advertisement content changes still publish immediately;
unchanged heartbeat refreshes follow the Beacon bucket TTL jitter cadence. If a
running managed advertiser finds its own advertisement key missing, it republishes
the advertisement instead of treating the missing TTL record as permanent
failure.
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

Concord maintenance observations use:

```text
bucket: deckr_concord_maintenance_v1
key:    stale.<contract-id-token>.<generation>
schema: dev.deckr.concord.stale-observation.v1
```

The runtime-facing Python API is `deckr.concord.Concord`.

```python
concord = deckr.concord
agreement = await concord.propose(
    ConcordAgreementSpec(
        participants=("controller:main", "hardware_manager:mirabox-main"),
        local_participant="controller:main",
        local_session_id="controller-session",
        stable_contract_id="hardware-claim-main",
        profile="dev.deckr.profile.hardware_claim.v1",
        terms=terms,
    )
)
lease = await concord.attach(
    agreement.contract,
    participant="hardware_manager:mirabox-main",
    session_id="manager-session",
)
validity = await concord.validate(agreement.contract)
await lease.aclose()
```

Lower-level tests and maintenance code can still create a `Concord` instance
directly from materialized or raw KV buckets:

```python
concord = Concord(contract_bucket, token_bucket, maintenance_bucket)
agreement = await concord.propose(
    ConcordAgreementSpec(
        participants=("controller:main", "hardware_manager:mirabox-main"),
        local_participant="controller:main",
        local_session_id="controller-session",
        profile="dev.deckr.profile.hardware_claim.v1",
        terms=terms,
    )
)
lease = await concord.attach(
    agreement.contract,
    participant="hardware_manager:mirabox-main",
    session_id="manager-session",
)
validity = await concord.validate(agreement.contract)
await lease.aclose()
```

A Concord contract is valid only while the contract is open and every named
participant maintains an acceptable token for the same contract id, generation,
participant, session, and terms hash. Any participant may cancel the contract.
Explicit participant-lease and agreement close perform best-effort owned-token
withdrawal after exact ownership validation. Reconciliation may still release a
locally managed lease without deleting its token when it is only changing local
selection state.
The full Concord semantic contract is specified in
[`beacon-concord.md`](beacon-concord.md#concord).

The optional lane-less component `dev.deckr.concord.reaper` runs
`ConcordReaperService`. It uses only Concord contract/token validity. Beacon
advertisements are TTL-bound and are not reaped. The reaper persists first stale
observations, cancels stale open contracts after the configured grace period,
logs deletion context without full `terms`, deletes cancelled records after
retention, and cleans any remaining participant-token keys for deleted contract
generations. Its `scan_once()` path intentionally lists `contracts.` and `stale.`
KV keys and exact-reads participant-token keys; it does not start Concord
materialized watches.

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
`Beacon.advertise` leases, maintain claim tokens through
`ConcordParticipant`, and route input only for live claims.
Hardware device inventory is published through the hardware Beacon profile only.
The `hardware_messages` lane is for control input, commands, capability state,
and replies; it must not be treated as the inventory authority. If a claimed
device disappears, the hardware manager cancels the matching Concord claim or
stops maintaining its participant token.

The service API is intentionally layered instead of one broad runtime helper.
`deckr.services` exports service profile and message contracts such as
`ServiceProtocol`, `ServiceAdvertisementPayload`, `ServiceDescriptor`,
`ServiceUseTerms`, `ServiceViewFamily`, `ServiceViewRef`, service command
schemas, descriptor parsing, terms construction, and service-view key helpers.
Services advertise descriptors through Beacon, negotiate service-use authority
through Concord, and, when the host explicitly enables the optional `services`
lane, carry service command/reply messages as ordinary lane traffic. A service
keeps its own advertisement fresh, skips unchanged refresh writes when possible,
best-effort withdraws on clean shutdown, and removes stale same-endpoint
advertisements left by an earlier crashed session or changed configuration.

Protected service views are direct JetStream/KV views. Service/application code
opens them by constructing `ServiceViewStore` from an explicit KV bucket and
starting it in a caller-owned task group. `ServiceViewStore` uses the same
materialized KV recovery helper as Beacon and Concord, exposes direct `get`,
`put`, `create`, `update`, `delete`, and `watch` style behavior, and authorizes
protected reads and watches with an active Concord service-use lease.
Local view state is updated immediately after successful writes and deletes, and
recovered watch snapshots reconcile missing keys so missed delete/expiry events
do not leave stale cached view entries. View entries are fenced by `serviceId`,
`serviceNamespace`, and `sessionId`, and writes return `entry.revision` for CAS
updates and deletes. Protected watches deliver `put` payloads only when the
entry matches the watcher's lease fence. Replacing a visible same-key entry with
another service identity or session emits a removal-style event to the original
watcher without exposing the replacement payload, and delete/expire events for
never-visible entries are suppressed for that watcher. After a service-use
Concord contract is negotiated, service command and protected view authority
follows Concord, not continued Beacon advertisement presence. A service may
withdraw its Beacon advertisement when it cannot accept new service-use
contracts; existing service-use contracts remain Concord-governed. Consumers
validate those existing service-use contracts before treating missing Beacon
discovery as service unavailability.

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
`Beacon` semantic feature events from the runtime materialized KV view and does
not use lane subscription state or per-dependency raw Beacon KV watches as an
authority source. Dependency readiness is not agreement withdrawal; existing
Concord contracts must be validated through Concord.

## Store Configuration

NATS-backed protocol stores are opened with explicit policies. Beacon is opened
by the managed runtime as `deckr.beacon`; callers should not construct a Beacon
store manually. Concord is opened by the managed runtime
as `deckr.concord`; callers should not construct Concord authority directly
from raw KV buckets.

```python
from deckr.services import ServiceViewStore
from deckr.substrates.nats_kv import KvBucketPolicy

beacon = deckr.beacon
concord = deckr.concord
views = ServiceViewStore(
    bucket=deckr.kv_bucket(
        KvBucketPolicy(
            bucket="com_example_service_view_v1",
            ttl_seconds=30,
            allow_write_ttl=True,
            description="service view KV",
        )
    )
)
views.start(task_group)
```

Component factories that need raw KV buckets, such as the Concord reaper, receive
them from `ComponentContext`:

```python
contract_bucket = context.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY)
token_bucket = context.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY)
maintenance_bucket = context.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY)
```

The reaper wraps these buckets in Concord maintenance logic for CAS
cancellation, retention deletion, token cleanup, and orphaned stale-observation
cleanup. Other components should prefer managed `deckr.beacon`, `deckr.concord`,
and explicit `ServiceViewStore` instances opened from `deckr.kv_bucket(...)` for
normal protocol and protected service-view authority.

TTL-bound buckets are configured with broker-owned bucket TTL, subject delete
markers retained for the same duration as the bucket TTL, and one retained
message per subject. The delete markers are required so long-lived materialized
watch caches observe broker-owned expiry instead of retaining keys that exact KV
reads no longer return. Client libraries may surface these marker wakeups as
delete or expire events depending on header visibility; either event must remove
the cached key. Persistent buckets reject per-write TTL. Reopening the same
bucket with a conflicting policy is an error.

Concord treats the token bucket's configured TTL as the single token TTL source.
Token records mirror that TTL; they are not independently configured by
participants. Lowering the bucket TTL while participants are already sleeping
may expire existing leases fail-closed before they wake and refresh.

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
nats kv info deckr_concord_maintenance_v1 --server nats://127.0.0.1:4222

nats kv ls deckr_beacon_advertisement_v1 'advertisements.by_feature.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_contract_v1 'contracts.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_token_v1 'contracts.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_maintenance_v1 'stale.>' --server nats://127.0.0.1:4222
```

Summarize current Beacon/Concord KV contents:

```bash
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```

### JetStream Consumer Hygiene

NATS KV `watch()` and list-style helpers are backed by JetStream consumers.
Beacon, Concord, and service views use long-lived materialized KV watches owned
by the runtime. On watch startup and recovery, the materialized helper
reconciles the full watch snapshot against the cached prefix and synthesizes
tombstones for cached keys absent from the recovered snapshot. Normal reads are
served from cached maps rather than per-query native bucket scans. Temporary
watch/list consumers must be explicitly deleted or avoided once the read is
complete; server-side inactive cleanup is a fallback, not the steady-state
cleanup path.

`ConcordReaperService` is allowed to use list-style raw scans because it runs
infrequently and does not provide immediate notification. It avoids materialized
watches entirely, so it should not leave watch consumers behind.

Treat watch events as wakeups. Managed materialized views are the normal Python
runtime authority for reads; exact KV reads are used for writes, strict
validation, recovery, and maintenance. Every watch still consumes broker
resources. Use `scripts/nats_state_report.py` for current Beacon/Concord keys
and TTL behavior, then pair it with the `/jsz` check below to confirm that
steady-state unbound consumer counts remain bounded.

## Troubleshooting

If a runtime cannot find hardware, actions, or services:

1. Check the relevant Beacon feature id.
2. Check the advertisement endpoint and session id.
3. Check whether the advertisement bucket TTL is expiring records.
4. For `services` lane traffic, check that the host registered the optional
   service lane contract and lane name.
5. If a live agreement is expected, validate the Concord contract and every
   participant token; do not treat Beacon disappearance as withdrawal.
6. For profile-specific behavior, validate the Beacon payload or Concord terms
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
