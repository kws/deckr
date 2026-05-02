# NATS Bus And Current State

> Live implementation reference: this document describes behavior currently
> implemented in `deckr`. It should stay in sync with code, tests, and generated
> schemas. If it differs from the implementation, treat that as a bug: either
> update the document to match current behavior or make an intentional
> code/schema/test change to match the intended v1 contract.

This document is the normative implementor and operator reference for Deckr's
distributed bus.

Deckr uses:

- Core NATS for low-latency lane traffic.
- JetStream KV for retained current state.

Deckr still owns its protocol. NATS carries it. Application code uses Deckr lane
handles, Deckr envelopes, Deckr endpoint addresses, Deckr subjects, and Deckr
current-state models. NATS subjects, reply inboxes, JetStream streams, KV bucket
subjects, queue groups, connection ids, and service ids remain substrate details.

## Runtime Surface

The default distributed runtime uses `NatsSubstrate` through `Deckr`:

```python
from deckr.runtime import Deckr

async with Deckr() as deckr:
    hardware = deckr.lane("hardware_messages").endpoint("hardware_manager:main")
    state = deckr.state("deckr_state_v1")

    async with hardware.subscribe() as messages:
        async for message in messages:
            ...
```

The NATS implementation is installed with the `deckr[nats]` extra. A host that
constructs its own substrate must provide the same lane and state semantics.

## Core Rules

- The v1 distributed lane substrate is NATS.
- `plugin_messages` and `hardware_messages` lane traffic uses Core NATS, not
  JetStream persistence.
- Retained communication state uses JetStream KV.
- KV current state is authoritative for endpoint presence, hardware inventory,
  device claims, and plugin action catalogs.
- KV watches are wakeups. Broker snapshots from `get()` and `items()` are the
  repair path for local projections.
- Local caches are disposable projections rebuilt from broker current state plus
  local durable/config/runtime state.
- `StateUnavailable` means unknown and retry. It is not absence.
- `StateConflict` means a real first-writer or revision race.
- Payload `timestamp` and `ttlSeconds` fields are diagnostics. Broker-owned KV
  TTL is the lease authority.
- No production Deckr WebSocket or MQTT lane substrate, route table,
  route-policy, `remote_endpoints`, or route lease model is supported.

Adapter-private WebSocket, MQTT, USB, HID, HTTP, or vendor protocols may exist at
real external protocol boundaries. They must translate into canonical Deckr lane
messages and KV current-state documents at that boundary.

## Vocabulary

- **Deckr lane:** a logical Deckr message contract, such as `plugin_messages` or
  `hardware_messages`.
- **Deckr endpoint address:** a protocol address such as `controller:main`,
  `host:python`, or `hardware_manager:mirabox`.
- **Deckr subject:** the domain entity a Deckr message is about, such as a
  device, control, action, binding, context, page session, or profile.
- **NATS subject:** the substrate publish/subscribe address used by the NATS
  adapter. It is not a Deckr domain identity.
- **NATS reply subject/inbox:** a private substrate detail used underneath Deckr
  request/reply.
- **JetStream KV bucket:** retained current-state storage. Deckr treats it as
  current facts, not as an event log.
- **NATS Service:** an operations API concept for `PING`, `INFO`, and `STATS`.
  It must not be used for Deckr domain discovery, action resolution, reachability,
  or device ownership.

## Lane Traffic

Lane subjects use this shape:

```text
deckr.lane.<lane>.<sender-family>.<sender-id>
```

Examples:

```text
deckr.lane.hardware_messages.controller.main
deckr.lane.hardware_messages.hardware_manager.mirabox-main
deckr.lane.plugin_messages.controller.main
deckr.lane.plugin_messages.host.python
```

`<lane>`, `<sender-family>`, and `<sender-id>` are encoded with the same
NATS-safe token rules used by state keys. Raw endpoint addresses such as
`hardware_manager:main` are not placed into one subject token.

The NATS message payload is the canonical Deckr envelope serialized as JSON. The
payload is authoritative. NATS headers are hints for adapter behavior and
observability.

Supported Deckr-owned headers:

```text
Deckr-Message-Id
Deckr-Message-Type
Deckr-Sender
Deckr-Recipient
Deckr-In-Reply-To
```

If a NATS subject or header disagrees with the Deckr envelope, the adapter rejects
or drops the message and reports diagnostics.

Request/reply uses normal NATS reply subjects underneath Deckr's request API.
Deckr message ids, `inReplyTo`, and `causationId` remain in the envelope.
Application code matches replies with Deckr `inReplyTo`; the NATS inbox is not
application identity.

## Recipient Filtering

From Deckr's point of view, a lane is a logical broadcast domain. A participant
publishes a Deckr envelope onto a lane and does not calculate a network route.

The shared lane ingress path:

- parses and validates the Deckr envelope
- confirms that subject/header delivery hints agree with the envelope
- validates the envelope against the lane contract
- delivers direct messages only to the addressed endpoint handle
- delivers broadcasts only to endpoint handles included by the broadcast target
- drops malformed, expired, unauthorized, stale, or undeliverable messages

NATS subject filtering may reduce delivered traffic and enforce coarse
permissions, but it is not the authoritative Deckr recipient check.

## State Store API

Application code uses `deckr.state.StateStore`, not raw `nats-py` KV calls:

```python
state = deckr.state("deckr_state_v1")

entry = await state.get("presence.endpoint.plugin_messages.host.python")
entries = await state.items("catalog.plugin.")

written = await state.put(key, value, ttl=15.0)
created = await state.create(key, value, ttl=15.0)
updated = await state.update(key, value, revision=written.revision, ttl=15.0)
await state.delete(key, revision=updated.revision)

async with state.watch("catalog.plugin.") as changes:
    async for change in changes:
        ...
```

Operations mean:

- `get(key)` returns the current entry or `None` if the broker says it is
  missing or deleted.
- `items(prefix)` returns current entries whose keys begin with `prefix`.
- `put(key, value)` writes the current value.
- `create(key, value)` writes only if the key is absent.
- `update(key, value, revision=...)` writes only if the current revision still
  matches.
- `delete(key, revision=...)` removes the key, optionally guarded by revision.
- `watch(prefix)` streams `StateChange("put" | "delete" | "expire", ...)`.

`StateEntry` contains `key`, JSON-safe `value`, and broker `revision`.
`StateChange.entry` is present only for `put` operations.

## State Bucket

The default Deckr current-state bucket is:

```text
deckr_state_v1
```

Bucket requirements:

- `history = 1`
- `max_msgs_per_subject = 1`
- broker TTL / max age = `15s`
- message TTL / limit markers enabled where supported

The current cadence is:

```text
heartbeat every 5s
broker TTL after 15s
```

The NATS substrate creates or updates the development bucket configuration when
possible. If an older development bucket cannot be updated safely, delete the
`KV_deckr_state_v1` stream and restart the runtime.

Per-key TTL values other than the bucket TTL are rejected by the NATS substrate.

## Key Tokens

Raw ids are encoded before they become NATS key or subject tokens:

- If the raw id matches `[A-Za-z0-9][A-Za-z0-9_-]*` and does not start with
  `b64_`, use it unchanged.
- Otherwise encode UTF-8 bytes with unpadded base64url and prefix the result with
  `b64_`.
- Tokens beginning with `b64_` are always decoded as base64url fallback tokens,
  so raw ids that naturally start with `b64_` must use the fallback form.

Python helpers live in `deckr.state`:

```python
presence_endpoint_key(lane="plugin_messages", endpoint="host:python")
hardware_inventory_key("mirabox")
device_claim_key(manager_id="mirabox", device_id="device-1")
plugin_action_catalog_key("python")
```

Matching parsers are also in `deckr.state`.

## Current-State Keys

Endpoint presence:

```text
presence.endpoint.<lane>.<endpoint-family>.<endpoint-id>
```

Hardware inventory:

```text
inventory.hardware.<manager-id>
```

Plugin action catalog:

```text
catalog.plugin.<host-id>
```

Device claim:

```text
claim.device.<manager-id>.<device-id>
```

Consumers must validate both key identity and payload identity. A catalog key for
`catalog.plugin.a` with payload `hostId = b` is invalid. A presence key for
`host:a` with payload endpoint `host:b` is invalid.

## Endpoint Presence

Endpoint presence says a Deckr endpoint is currently participating on a lane.

Example:

```json
{
  "endpoint": "host:python",
  "lane": "plugin_messages",
  "sessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 15,
  "metadata": {
    "runtime": "deckr-pluginhost-python"
  }
}
```

Presence is last-registration-wins. A process writes a fresh random `sessionId`
at startup. A new session for the same endpoint is a restart or takeover
boundary. Consumers invalidate live state tied to the old session.

Presence is not a route table and does not replace envelope recipient filtering.

## Hardware Inventory

Hardware inventory says which devices a hardware manager currently sees.

Example:

```json
{
  "managerId": "mirabox",
  "managerEndpoint": "hardware_manager:mirabox",
  "sessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 15,
  "devices": {
    "device-1": {
      "deviceRef": {
        "managerId": "mirabox",
        "deviceId": "device-1",
        "fingerprint": "stable-device-fingerprint"
      },
      "descriptor": {
        "deviceId": "device-1",
        "fingerprint": "stable-device-fingerprint",
        "displayName": "MiraBox Stream Dock",
        "manufacturer": "MiraBox",
        "model": "Stream Dock",
        "controls": [
          {
            "controlId": "key-0-0",
            "kind": "button",
            "label": "Key 1",
            "geometry": {
              "x": 0,
              "y": 0,
              "width": 1,
              "height": 1,
              "unit": "grid"
            },
            "inputCapabilities": [
              {
                "capabilityId": "press",
                "family": "deckr.input.button",
                "type": "activation",
                "direction": "input",
                "access": ["emits"],
                "eventTypes": ["press"]
              }
            ],
            "outputCapabilities": [
              {
                "capabilityId": "raster.bitmap",
                "family": "deckr.output.raster",
                "type": "bitmap",
                "direction": "output",
                "access": ["settable"],
                "commandTypes": ["set_frame", "clear"]
              }
            ]
          }
        ],
        "capabilities": [
          {
            "capabilityId": "device.power",
            "family": "deckr.device.power",
            "type": "screen",
            "direction": "command",
            "access": ["invokable"],
            "commandTypes": ["sleep", "wake"]
          }
        ]
      }
    }
  }
}
```

Inventory is aggregate by manager. Device removal is represented by rewriting
the manager inventory without that device, not by writing a per-device tombstone.

Inventory is usable only while matching manager endpoint presence exists with the
same `sessionId`. If manager presence disappears or changes session, dependent
live device state becomes unavailable.

The canonical v1 descriptor contracts are implemented in
`deckr.hardware.descriptors`, with generated JSON Schema artifacts in
`schemas/hardware`. Inventory records use manager-scoped `DeviceRef` values and
carry the same `DeviceDescriptor` shape published by `deviceAvailable` and
`deviceDescriptorChanged` messages on the `hardware_messages` lane.

## Device Claims

Device claims coordinate controller ownership of devices exposed by a hardware
manager.

Example:

```json
{
  "claimedByEndpoint": "controller:main",
  "claimedBySessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 15
}
```

Claims are first-writer-wins:

- `create()` claims an unclaimed device.
- `StateConflict` means someone else owns or changed the claim.
- `update(revision=...)` refreshes only the revision the controller owns.
- revision-checked `delete()` releases the claim on graceful shutdown.

A claim is meaningful only while:

- the claimed device is present in current manager inventory
- the manager inventory session matches manager endpoint presence
- the claiming controller endpoint has current presence
- the claim's `claimedBySessionId` matches controller presence

A controller restart never adopts an old claim only because the endpoint address
matches. It waits for old claim deletion or expiry, then attempts a fresh atomic
create from current inventory.

For claim refresh:

- `StateConflict` revokes live ownership.
- `StateUnavailable` does not revoke live ownership immediately. The controller
  retries and lets broker TTL or a later conflict settle ownership.

## Plugin Action Catalogs

Plugin action catalogs advertise action types provided by a plugin host.

Example:

```json
{
  "hostId": "python",
  "hostEndpoint": "host:python",
  "sessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 15,
  "actions": {
    "com.example.clock.digital": {
      "actionId": "com.example.clock.digital",
      "name": "Digital Clock",
      "pluginId": "com.example.clock",
      "controllers": []
    }
  }
}
```

The action map is keyed by `actionId`; each map key must match the descriptor's
`actionId`, and `hostEndpoint` must equal `host:<hostId>`. The catalog is usable
only while matching host endpoint presence exists with the same `sessionId`.

Catalog loss, host presence loss, host session change, or catalog
incompatibility makes affected actions unavailable and causes the controller to
revoke dependent live bindings. The plugin host does not broadcast
`actionsUnregistered`; broker current state is the source of truth.

## Producer Pattern

A participant that owns current state should:

1. Generate a fresh process/session id at startup.
2. Publish endpoint presence immediately.
3. Publish its current domain state immediately, such as inventory or catalog.
4. Refresh both on the 5s heartbeat with the 15s broker TTL.
5. Rewrite aggregate state immediately when the underlying facts change.
6. On graceful stop, delete its own keys with revision checks.
7. On `StateUnavailable`, log and retry.

Failed refresh means unknown/retry. Only graceful stop withdraws owned state. If
the owner is truly gone, broker TTL removes the key.

## Consumer Pattern

A consumer should:

1. Watch relevant prefixes for low-latency wakeups.
2. Reconcile from broker snapshots with `items()` on start and whenever a local
   projection could otherwise go stale.
3. Treat `put` as "validate and update local projection".
4. Treat `delete` and `expire` as "remove that current fact".
5. Treat `StateUnavailable` as unknown/retry.
6. Never derive availability from local payload timestamps.
7. Reject mismatched key/payload identity.

Every component owns its own internal state machine. Remote observations can
arrive in any order:

```text
missing -> present
present -> missing
present -> different session
catalog present -> host presence missing
claim present -> controller presence missing
inventory present -> manager presence missing
```

The component's job is:

```text
broker current state + local durable/config state + local runtime state
  -> local operational state
```

If a local projection cannot be rebuilt from `get()` or `items()` plus local
durable/config/runtime state, it is probably treating watches as an event log.

Do not add production lane messages such as `componentAppeared`,
`componentDisappeared`, `hostOnline`, `hostOffline`, `actionsRegistered`,
`actionsUnregistered`, `requestActions`, route up, or route down as availability,
discovery, ownership, or reachability inputs.

## Testing Boundary

Deckr tests should cover Deckr behavior at the NATS adapter boundary and above:

- envelope serialization, subject mapping, header mapping, and validation
- recipient filtering and lane-contract rejection
- request/reply mapping through Deckr ids and `inReplyTo`
- key generation, token encoding, payload validation, and identity checks
- mapping KV puts, deletes, purges, and MaxAge markers into `StateChange`
- domain responses to endpoint loss, KV deletion, expiry, reconnect, no
  responder, timeout, create conflict, revision conflict, and `StateUnavailable`

Do not write Deckr tests whose purpose is to prove upstream NATS conformance.
NATS wildcard matching, queue groups, request/reply mechanics, reconnect
implementation, JetStream KV TTL, atomic create, revision checks, and
server-side permissions belong to NATS and its client libraries.

Use real NATS for integration and smoke coverage of Deckr wiring. Use fakes for
unit and domain tests where the assertion belongs to Deckr's reaction.

## Permissions

NATS subject permissions should enforce the broad shape of participant behavior.
The Deckr adapter must still validate envelope sender, recipient, message type,
and lane contract at ingress.

Examples:

- A hardware manager with endpoint `hardware_manager:mirabox` publishes lane
  traffic only to `deckr.lane.hardware_messages.hardware_manager.mirabox` and
  updates only its own presence and inventory keys.
- A plugin host with endpoint `host:python` publishes lane traffic only to
  `deckr.lane.plugin_messages.host.python` and updates only its own presence and
  catalog keys.
- A controller with endpoint `controller:main` publishes controller-originated
  messages on `hardware_messages` and `plugin_messages`, reads and watches
  endpoint, inventory, catalog, and claim keyspaces, and creates or refreshes
  claim keys according to controller policy.

Request/reply permissions must allow the relevant `_INBOX` subjects or use NATS
`allow_responses` where that better fits responder behavior.

## Troubleshooting

Run the `deckr` smoke broker from the `deckr` repository:

```bash
docker compose -f docker/compose.nats-smoke.yaml up -d nats
uv run --extra nats python scripts/nats_smoke.py --url nats://127.0.0.1:4222 --check-ttl
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
docker compose -f docker/compose.nats-smoke.yaml down -v
```

Run the workspace runtime broker from the `streamdock` workspace root:

```bash
docker compose -f docker/compose.nats-runtime.yaml up nats
```

Check JetStream and the bucket:

```bash
nats server check jetstream --server nats://127.0.0.1:4222
nats kv info deckr_state_v1 --server nats://127.0.0.1:4222
nats kv ls deckr_state_v1 --server nats://127.0.0.1:4222
```

Inspect current Deckr communication state:

```bash
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```

Watch lane traffic:

```bash
nats sub 'deckr.lane.>' --server nats://127.0.0.1:4222
```

Inspect useful keyspaces:

```bash
nats kv ls deckr_state_v1 'presence.endpoint.>' --server nats://127.0.0.1:4222
nats kv ls deckr_state_v1 'inventory.hardware.>' --server nats://127.0.0.1:4222
nats kv ls deckr_state_v1 'catalog.plugin.>' --server nats://127.0.0.1:4222
nats kv ls deckr_state_v1 'claim.device.>' --server nats://127.0.0.1:4222
```

When a component appears unavailable, check in this order:

1. Is its endpoint presence key present and carrying the expected endpoint, lane,
   and session id?
2. Is its domain state present and session-matched, such as inventory for a
   hardware manager or catalog for a plugin host?
3. If a device is claimed, does the claim's controller endpoint/session match
   current controller presence?
4. Did the key expire after the 15s TTL because the component stopped refreshing
   it?
5. Does the component log `StateUnavailable`, indicating broker uncertainty
   rather than absence?

## References

- NATS subject-based messaging:
  <https://docs.nats.io/nats-concepts/subjects>
- NATS Core pub/sub:
  <https://docs.nats.io/nats-concepts/core-nats/pubsub>
- NATS request/reply:
  <https://docs.nats.io/nats-concepts/core-nats/reqreply>
- NATS queue groups:
  <https://docs.nats.io/nats-concepts/core-nats/queue>
- NATS authorization and subject permissions:
  <https://docs.nats.io/running-a-nats-service/configuration/securing_nats/authorization>
- NATS decentralized JWT authentication/authorization:
  <https://docs.nats.io/running-a-nats-service/configuration/securing_nats/auth_intro/jwt>
- NATS Services API:
  <https://docs.nats.io/using-nats/developer/services>
- NATS JetStream:
  <https://docs.nats.io/nats-concepts/jetstream>
- NATS JetStream KV:
  <https://docs.nats.io/nats-concepts/jetstream/key-value-store>
- NATS JetStream KV developer API:
  <https://docs.nats.io/using-nats/developer/develop_jetstream/kv>
- NATS JetStream headers:
  <https://docs.nats.io/nats-concepts/jetstream/headers>
- NATS Server 2.11 per-message TTL release note:
  <https://docs.nats.io/release-notes/whats_new/whats_new_211>
- NATS ADR-48 KV TTL / limit marker design:
  <https://github.com/nats-io/nats-architecture-and-design/blob/main/adr/ADR-48.md>
- NATS leaf nodes:
  <https://docs.nats.io/running-a-nats-service/configuration/leafnodes>
