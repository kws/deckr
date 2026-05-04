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
subjects, queue groups, connection ids, and NATS Service API ids remain
substrate details.

## Runtime Surface

The default distributed runtime uses `NatsSubstrate` through `Deckr`:

```python
from deckr.runtime import Deckr

async with Deckr() as deckr:
    hardware = deckr.lane("hardware_messages")
    services = deckr.lane("services")
    lease_state = deckr.state("deckr_lease_v1")
    discovery_state = deckr.state("deckr_discovery_v1")

    async with hardware.register_endpoint("hardware_manager:main") as manager:
        async with manager.subscribe() as messages:
            async for message in messages:
                ...
```

The NATS implementation is installed with the `deckr[nats]` extra. A host that
constructs its own substrate must provide the same lane and state semantics.

For local full-stack or embedded runtimes that should not require Docker or a
separately managed broker, Deckr also provides `SupervisedNatsSubstrate` through
the `deckr[supervised-nats]` extra:

```python
from deckr.runtime import Deckr
from deckr.substrates.supervised_nats import SupervisedNatsSubstrate

async with Deckr(
    substrate=SupervisedNatsSubstrate(lane_contracts=...),
) as deckr:
    ...
```

The supervised substrate starts a real `nats-server` child process, waits for a
real JetStream readiness call to succeed, and then delegates lane and state
behavior to `NatsSubstrate`. It is a local infrastructure convenience for the
same NATS contract, not a separate in-memory or no-NATS runtime mode.

## Core Rules

- The v1 distributed lane substrate is NATS.
- A supervised local broker is still the NATS substrate; it is process
  ownership around the broker, not a different Deckr bus contract.
- `actions`, `hardware_messages`, and `services` lane traffic use Core NATS,
  not JetStream persistence.
- Retained communication state uses JetStream KV split by semantics:
  `deckr_lease_v1` for TTL-bound leases and `deckr_discovery_v1` for non-TTL
  discovery.
- Exact-key lease state is authoritative for endpoint presence and device claims.
- Discovery state describes current action provider catalogs, hardware
  inventory, service catalogs, service status, and service-owned views.
  Discovery records are usable only when matching endpoint presence exists in
  the lease bucket with the same session id.
- KV watches are wakeups. `get(key)` is the exact-key repair path for local
  projections. `items(prefix)` is a prefix observation and is not an
  absence-authoritative snapshot.
- Local caches are disposable projections rebuilt from broker current state plus
  local durable/config/runtime state.
- `StateUnavailable` means unknown and retry. It is not absence.
- `StateConflict` means a real first-writer or revision race.
- Payload `timestamp` fields are diagnostics. Payload `ttlSeconds` appears only
  on lease documents and mirrors the broker-owned lease TTL.
- No production Deckr WebSocket or MQTT lane substrate, route table,
  route-policy, `remote_endpoints`, or route lease model is supported.

Adapter-private WebSocket, MQTT, USB, HID, HTTP, or vendor protocols may exist at
real external protocol boundaries. They must translate into canonical Deckr lane
messages and KV current-state documents at that boundary.

## Vocabulary

- **Deckr lane:** a logical Deckr message contract, such as `actions`,
  `hardware_messages`, or `services`.
- **Deckr endpoint address:** a protocol address such as `controller:main`,
  `action_provider:python`, `hardware_manager:mirabox`, or
  `service:sonos-home`.
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
deckr.lane.actions.controller.main
deckr.lane.actions.action_provider.python
deckr.lane.services.service.sonos-home
deckr.lane.services.action_provider.python-sonos
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
Deckr-Sender-Session
Deckr-Recipient
Deckr-Recipient-Session
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
- confirms that `senderSessionId` matches current endpoint presence
- delivers direct messages only to the addressed endpoint handle
- delivers direct messages with `recipientSessionId` only to the matching local
  endpoint session
- delivers broadcasts only to endpoint handles included by the broadcast target
- drops malformed, expired, unauthorized, stale, or undeliverable messages

NATS subject filtering may reduce delivered traffic and enforce coarse
permissions, but it is not the authoritative Deckr recipient check.

## Services Lane

The `services` lane is the core command/reply lane for endpoint-addressed Deckr
services. It is used when a component needs to call a service endpoint without
receiving a local Python object.

Supported endpoint families on `services` are:

- `controller`
- `action_provider`
- `service`

`hardware_manager` is not a supported `services` lane participant in the current
contract.

The initial message types are:

- `serviceCommand`
- `serviceCommandReply`

A `serviceCommand` body carries:

```json
{
  "serviceNamespace": "com.k-si.deckr.sonos.service",
  "operation": "play",
  "params": {
    "zone": "Kitchen"
  }
}
```

A `serviceCommandReply` body carries:

```json
{
  "serviceNamespace": "com.k-si.deckr.sonos.service",
  "operation": "play",
  "status": "ok",
  "result": {
    "accepted": true
  }
}
```

Reply status values are `ok`, `rejected`, `unavailable`, and `error`. Error
replies may include a structured `error` object with `code`, `message`, and
`diagnostics`.

The Deckr envelope remains the authority for sender endpoint, sender session,
recipient endpoint, recipient session, message id, `inReplyTo`, expiry, and
subject. Service bodies must not duplicate sender or session authority fields.

## State Store API

Application code uses `deckr.state.StateStore`, not raw `nats-py` KV calls:

```python
lease_state = deckr.state("deckr_lease_v1")
discovery_state = deckr.state("deckr_discovery_v1")

entry = await lease_state.get("presence.endpoint.actions.action_provider.python")
entries = await discovery_state.items("catalog.actions.providers.")
service = await discovery_state.get("catalog.services.sonos-home")

written = await lease_state.put(key, value, ttl=30.0)
created = await lease_state.create(key, value, ttl=30.0)
updated = await lease_state.update(
    key,
    value,
    revision=written.revision,
    ttl=30.0,
)
await lease_state.delete(key, revision=updated.revision)

catalog = await discovery_state.put(catalog_key, catalog_value)

async with discovery_state.watch("catalog.actions.providers.") as changes:
    async for change in changes:
        ...
```

Operations mean:

- `get(key)` returns the current entry or `None` if the broker says it is
  missing or deleted.
- `items(prefix)` observes current entries whose keys begin with `prefix`.
  Consumers may add or refresh entries from this observation, but must not remove
  known facts solely because they are omitted from one prefix result.
- `put(key, value)` writes the current value.
- `create(key, value)` writes only if the key is absent.
- `update(key, value, revision=...)` writes only if the current revision still
  matches.
- `delete(key, revision=...)` removes the key, optionally guarded by revision.
- `watch(prefix)` streams `StateChange("put" | "delete" | "expire", ...)`.

`StateEntry` contains `key`, JSON-safe `value`, and broker `revision`.
`StateChange.entry` is present only for `put` operations.

Use `deckr.state.observe_prefix_current(state, prefix, known_keys=...)` when a
projection needs to reconcile possible removals. The helper calls `items(prefix)`,
then exact-gets omitted known keys. It returns observed entries and
`confirmed_missing` keys. If the broker cannot answer exactly it raises
`StateUnavailable` and the caller must keep its prior projection.

## State Buckets

The default Deckr current-state buckets are:

```text
deckr_lease_v1
deckr_discovery_v1
```

Lease bucket requirements for `deckr_lease_v1`:

- `history = 1`
- `max_msgs_per_subject = 1`
- broker TTL / max age = `30s`
- message TTL / limit markers enabled where supported

Lease cadence:

```text
renew every 5s
broker TTL after 30s
```

Lease writes are used only for endpoint presence and device claims. Per-write TTL
arguments must be omitted or equal to the lease TTL.

Discovery bucket requirements for `deckr_discovery_v1`:

- `history = 1`
- `max_msgs_per_subject = 1`
- no broker TTL / max age
- no per-write TTL arguments from Deckr

Discovery writes are used for hardware inventory, action provider catalogs,
service catalogs, service status, and service-owned views. They are rewritten on
start and content change and deleted on graceful stop.
The NATS stream may still report message-TTL support enabled, especially if an
existing stream was created with that flag. Deckr does not rely on disabling that
flag because NATS does not allow it to be turned off after stream creation; the
Deckr state API rejects TTL writes to discovery stores instead.

The NATS substrate creates or updates the development bucket configuration when
possible. If an older development bucket cannot be updated safely, delete the
affected `KV_deckr_lease_v1` or `KV_deckr_discovery_v1` stream and restart the
runtime.

## Key Tokens

Raw ids are encoded before they become NATS key or subject tokens:

- If the raw id matches `[A-Za-z0-9][A-Za-z0-9_-]*` and does not start with
  `b64_`, use it unchanged.
- Otherwise encode UTF-8 bytes with unpadded base64url and prefix the result with
  `b64_`.
- Tokens beginning with `b64_` are always decoded as base64url fallback tokens,
  so raw ids that naturally start with `b64_` must use the fallback form.

Generic current-state helpers live in `deckr.state`:

```python
presence_endpoint_key(lane="actions", endpoint="action_provider:python")
hardware_inventory_key("mirabox")
device_claim_key(manager_id="mirabox", device_id="device-1")
```

Action provider catalog helpers live in `deckr.actions.state`:

```python
action_provider_catalog_key("python")
```

Service helpers live in `deckr.services.state`:

```python
service_catalog_key("sonos-home")
service_status_key("sonos-home")
service_view_key("sonos-home", "com.k-si.deckr.sonos.service", "zones", "Kitchen")
```

Matching parsers live alongside the corresponding key helpers.

## Current-State Keys

Endpoint presence in `deckr_lease_v1`:

```text
presence.endpoint.<lane>.<endpoint-family>.<endpoint-id>
```

Hardware inventory in `deckr_discovery_v1`:

```text
inventory.hardware.<manager-id>
```

Action provider catalog in `deckr_discovery_v1`:

```text
catalog.actions.providers.<provider-instance-id>
```

Service discovery state in `deckr_discovery_v1`:

```text
catalog.services.<service-id>
status.services.<service-id>
view.services.<service-id>.<service-namespace>.<tokens...>
```

Device claim in `deckr_lease_v1`:

```text
claim.device.<manager-id>.<device-id>
```

Consumers must validate both key identity and payload identity. A catalog key for
`catalog.actions.providers.a` with payload `providerInstanceId = b` is invalid.
A presence key for `action_provider:a` with payload endpoint `action_provider:b`
is invalid.

Consumers may update projections from prefix observations in either bucket. They
must exact-confirm missing known keys with `get(key) is None` before treating a
prefix omission as deletion, claim revocation, provider loss, manager loss, or
device removal.

## Endpoint Presence

Endpoint presence says a Deckr endpoint is currently participating on a lane.
It is stored in `deckr_lease_v1`.

Example:

```json
{
  "endpoint": "action_provider:python",
  "lane": "actions",
  "sessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 30,
  "metadata": {
    "runtime": "deckr-action-provider-runtime-python"
  }
}
```

Presence is create-if-absent. `register_endpoint(...)` writes a fresh random
`sessionId`; an existing live presence key for the same lane and endpoint rejects
the duplicate registration. Registration is not an implicit takeover.

Endpoint sessions are renewed with a revision guard and the same `sessionId`.
Missing presence, session mismatch, revision conflict, expiry, or broker
uncertainty makes the session lost. Session loss is terminal for the registered
handle; reacquiring the address requires an outer runtime/supervisor policy and a
new registration with a new session id.

Normal context exit performs best-effort, session-guarded withdrawal. If
withdrawal cannot complete, broker TTL and receiver-side session fencing remain
the correctness mechanism.

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

Inventory is stored in `deckr_discovery_v1` without broker TTL. Managers rewrite
their aggregate inventory on start and whenever the device set or descriptors
change, and delete it on graceful stop. Failed dirty inventory publishes are
retried until the current content is written.

Inventory is usable only while matching manager endpoint presence exists with the
same `sessionId`. If manager presence disappears or changes session, dependent
live device state becomes unavailable.

The canonical v1 descriptor contracts are implemented in
`deckr.hardware.descriptors`, with generated JSON Schema artifacts in
`schemas/hardware`. Deckr-owned capability semantics are documented in
[`capabilities.md`](capabilities.md). Inventory records use manager-scoped
`DeviceRef` values and
carry the same `DeviceDescriptor` shape published by `deviceAvailable` and
`deviceDescriptorChanged` messages on the `hardware_messages` lane.

## Device Claims

Device claims coordinate controller ownership of devices exposed by a hardware
manager. They are stored in `deckr_lease_v1`.

Example:

```json
{
  "claimedByEndpoint": "controller:main",
  "claimedBySessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "ttlSeconds": 30
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

## Action Provider Catalogs

Action provider catalogs advertise action types provided by one action provider
instance.

Example:

```json
{
  "providerInstanceId": "clock-office",
  "providerEndpoint": "action_provider:clock-office",
  "providerId": "com.example.clock",
  "sessionId": "uuid-v4-string",
  "timestamp": "2026-04-29T10:30:00Z",
  "labels": {
    "location": "office"
  },
  "annotations": {
    "runtime": "python"
  },
  "actions": {
    "com.example.clock.digital": {
      "actionId": "com.example.clock.digital",
      "name": "Digital Clock",
      "controllers": []
    }
  }
}
```

The action map is keyed by `actionId`; each map key must match the descriptor's
`actionId`, `providerInstanceId` must match the catalog key suffix, and
`providerEndpoint` must equal `action_provider:<providerInstanceId>`.

Catalogs are stored in `deckr_discovery_v1` without broker TTL. They are
descriptive state, not live leases. Actions from a catalog are live only while
matching action-provider endpoint presence exists in `deckr_lease_v1` with the
same `sessionId`.

Exact-confirmed catalog loss or catalog incompatibility removes the descriptors.
Exact-confirmed provider presence loss makes affected actions unavailable and
causes the controller to revoke dependent live bindings. Catalog session changes
refresh dependent bindings only after matching provider presence moves to the
same session. The action provider instance does not broadcast
`actionsUnregistered`; broker current state plus lease presence is the source of
truth.

## Service Catalogs, Status, And Views

Service discovery state describes endpoint-addressed service instances and the
service-owned views they publish. A service id is a configured service instance,
such as `sonos-home`; a service namespace is the globally named API/state
contract, such as `com.k-si.deckr.sonos.service`.

Service catalog example:

```json
{
  "serviceId": "sonos-home",
  "serviceEndpoint": "service:sonos-home",
  "serviceNamespace": "com.k-si.deckr.sonos.service",
  "sessionId": "uuid-v4-string",
  "supportedOperations": ["play", "pause", "setVolume"],
  "viewPrefixes": ["view.services.sonos-home"],
  "timestamp": "2026-04-29T10:30:00Z",
  "labels": {
    "location": "home"
  },
  "annotations": {
    "runtime": "python"
  },
  "diagnostics": {}
}
```

Service status example:

```json
{
  "serviceId": "sonos-home",
  "serviceEndpoint": "service:sonos-home",
  "serviceNamespace": "com.k-si.deckr.sonos.service",
  "sessionId": "uuid-v4-string",
  "status": "available",
  "timestamp": "2026-04-29T10:30:00Z",
  "diagnostics": {}
}
```

Status values are:

- `available`
- `degraded`
- `unavailable`

Service catalogs, status, and views are stored in `deckr_discovery_v1` without
broker TTL. They are descriptive state, not live leases. A service is live only
while matching service endpoint presence exists in `deckr_lease_v1` on the
`services` lane with the same `sessionId`.

Service view keys are service-specific JSON documents under the service
namespace:

```text
view.services.<service-id>.<service-namespace>.<tokens...>
```

For example, a Sonos service may publish zone state under:

```text
view.services.sonos-home.b64_Y29tLmstc2kuZGVja3Iuc29ub3Muc2VydmljZQ.zones.Kitchen
```

The generic Deckr contract owns key shape, token encoding, and session gating.
The service namespace owns the view payload schema and operation semantics.

Current first-party service namespaces are:

- `com.k-si.deckr.sonos.service`
- `com.k-si.deckr.openhab.service`

First-party service components are ordinary configured components. Installing a
package that contributes a service component definition does not start the
service. Service ids such as `sonos-home` and `openhab-home` are configured
endpoint ids; they are not package names, component ids, service namespaces, or
runtime names.

The Sonos service owns SoCo discovery, speaker connections, subscriptions,
favourite lookup, music-service search/browse, queueing, and playback. Blocking
SoCo calls run off-thread inside the service component. Actions and other
clients communicate through `serviceCommand` request/reply and Sonos-owned
views; they do not receive or cache local SoCo speaker objects.

The Sonos operations are:

- `ensureZone`, with `zone`; ensures a zone is connected and publishes a zone
  view.
- `setVolume`, with `zone` and integer `volume` from `0` through `100`.
- `play` and `pause`, each with `zone`.
- `resolveFavourite`, with `zone`, `query`, and optional `limit`.
- `playFavourite`, with `zone` and `query`.
- `playMusicItem`, with `zone` and JSON-safe `playRef`.
- `listMusicServices`, with no params.
- `searchMusic`, with `musicService`, `category`, optional `term`, optional
  `offset`, and optional `limit`.
- `browseMusic`, with `musicService`, optional `browseRef`, optional `offset`,
  and optional `limit`.

Sonos zone views are stored below:

```text
view.services.<service-id>.<namespace>.zones.<zone-name>
```

Zone view payloads include `serviceId`, `serviceNamespace`, `sessionId`, `zone`,
`ipAddress`, `playerName`, `transportState`, `isPlaying`, `volume`, optional
`current`, and `timestamp`. The optional `current` object may include `title`,
`creator`, `source`, and `albumArtUri`.

Sonos favourite lookup and music-service browse/search results are ephemeral RPC
data and are not written as durable views. RPC media items are JSON-safe
documents with `mediaType`, `title`, optional `subtitle`, optional `imageUri`,
optional `metadata`, optional `playRef`, and optional `browseRef`.

`playMusicItem` is the canonical playback operation for returned Sonos media
items. Supported `playRef.format` values are:

- `soco-didl-lite-v1`, with `uri` and `didl`; the service queues the URI with
  the supplied DIDL-Lite metadata and plays from the queue.
- `sonos-uri-meta-v1`, with `uri` and `metadata`; the service first attempts
  direct URI playback and falls back to queue playback.

`browseRef.format` for service-side music browsing is
`soco-music-service-item-v1`, with `musicService` and `itemId`.

The OpenHAB service owns HTTP, SSE, auth, command sending, retries, and item
views. Its component config requires `url`; an empty or omitted `token` means no
bearer auth. Actions and other clients communicate through service commands and
OpenHAB-owned views rather than direct HTTP/SSE access.

The OpenHAB operations are:

- `ensureItems`, with `items` and optional boolean `refresh`; returns known item
  documents keyed by item name plus a `missing` list.
- `refreshItem`, with `item`.
- `sendCommand`, with `item` and `command`.

OpenHAB item views are stored below:

```text
view.services.<service-id>.<namespace>.items.<item-name>
```

Item view payloads include `serviceId`, `serviceNamespace`, `sessionId`, `item`,
`state`, optional `oldState`, `source`, and `timestamp`.

First-party service view payloads include `serviceId`, `serviceNamespace`,
`sessionId`, and `timestamp`. Consumers must reject a view whose `sessionId`
does not match exact live service presence/catalog/status.

Exact-confirmed service presence loss, session mismatch, missing catalog,
missing status, or `unavailable` status makes required service dependencies
unsatisfied. `degraded` status is a degraded dependency condition. Consumers must
treat `StateUnavailable` as unknown/retry, not absence.

## Dynamic Page Action Targets

Action providers request dynamic pages with `openPage`, `updatePage`, and
`replacePage` messages on the `actions` lane. The command body carries a
`DynamicPageCommand` with a controller-visible `pageId`, optional `templateId`,
and concrete child `bindings`.

Each child binding carries a `controlId` plus an explicit `target`:

- `{"kind": "self"}` routes the child to the action instance that opened the
  page. Role, item, and handler metadata remain binding metadata, not durable
  routing state.
- `{"kind": "action", "actionId": "..."}` asks the controller to resolve the
  child through normal action-provider selection. The target may include
  `providerInstanceId` or `providerLabels` to narrow selection. `instanceKey`
  is a page-scoped key the controller can use to share or split live child
  action instances for that target.

Action providers never send `actionInstanceId` for dynamic page children. The
controller owns action instance ids, binding ids, context ids, page session ids,
capability matching, child action resolution, page replacement/stack policy, and
cleanup. Child `settings` are the effective runtime settings for that child
binding or child action instance; they are not a hidden routing channel.

## Producer Pattern

A participant that owns current state should:

1. Register its Deckr endpoint with `register_endpoint(...)` and use the
   resulting endpoint `sessionId`.
2. Publish lease state, such as endpoint presence or device claims, only to
   `deckr_lease_v1` with the 30s lease TTL. Endpoint presence renewal is owned
   by the registered endpoint handle.
3. Publish discovery state, such as inventory or catalog, to
   `deckr_discovery_v1` without TTL using the endpoint session id.
4. Treat endpoint session loss as terminal for the registered handle.
5. Rewrite aggregate discovery state immediately when the underlying facts
   change. Retry failed dirty discovery publishes until the current content is
   written.
6. On graceful stop, delete owned discovery keys and release owned claims with
   revision checks; endpoint presence withdrawal is owned by the registered
   endpoint handle.
7. On `StateUnavailable`, treat the affected state as unknown and retry or let an
   outer supervisor create a fresh endpoint registration.

Lease refresh failure means unknown/retry. Only graceful stop withdraws owned
discovery state. If a lease owner is truly gone, broker TTL removes its lease
keys. Discovery can be stale safely because live use is gated by exact lease
presence and session checks.

## Consumer Pattern

A consumer should:

1. Watch relevant prefixes for low-latency wakeups.
2. Reconcile from prefix observations with `items()` on start and whenever a
   local projection could otherwise go stale.
3. Treat `put` as "validate and update local projection".
4. Treat `delete` and `expire` watch markers as removal wakeups, then reconcile.
5. Exact-get known keys omitted from prefix observations before removing them, or
   use `observe_prefix_current(...)`.
6. Treat `StateUnavailable` as unknown/retry and keep prior live projection.
7. Treat stale delete/expire markers as harmless when an exact `get(key)` still
   returns a current entry.
8. Never derive availability from local payload timestamps.
9. Reject mismatched key/payload identity.

Every component owns its own internal state machine. Remote observations can
arrive in any order:

```text
missing -> present
present -> missing
present -> different session
claim present -> controller presence missing
inventory present -> manager presence missing
service status present -> service presence missing
```

The component's job is:

```text
broker current state + local durable/config state + local runtime state
  -> local operational state
```

If a local projection cannot be rebuilt from `get()` plus `items()` observations
and local durable/config/runtime state, it is probably treating watches as an
event log.

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
  updates only its own lease presence key and discovery inventory key.
- An action provider instance with endpoint `action_provider:python` publishes
  lane traffic only to `deckr.lane.actions.action_provider.python` and updates
  only its own lease presence key and discovery catalog key.
- A service with endpoint `service:sonos-home` publishes lane traffic only to
  `deckr.lane.services.service.sonos-home` and updates only its own lease
  presence key, service catalog key, service status key, and owned service view
  keys.
- A controller with endpoint `controller:main` publishes controller-originated
  messages on `hardware_messages`, `actions`, and `services`, reads and watches
  lease endpoint/claim keyspaces and discovery inventory/catalog/status/view
  keyspaces, and creates or refreshes claim keys according to controller policy.

Request/reply permissions must allow the relevant `_INBOX` subjects or use NATS
`allow_responses` where that better fits responder behavior.

## Troubleshooting

Run the smoke harness with a supervised local NATS server:

```bash
uv run --extra supervised-nats python scripts/nats_smoke.py --supervised --check-ttl
```

Run the `deckr` smoke broker from the `deckr` repository:

```bash
docker compose -f docker/compose.nats-smoke.yaml up -d nats
uv run --extra nats python scripts/nats_smoke.py --url nats://127.0.0.1:4222 --check-ttl
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
docker compose -f docker/compose.nats-smoke.yaml down -v
```

Run the workspace runtime broker from the `streamdock` workspace root:

```bash
docker compose -f docker/compose.yaml up nats
```

Check JetStream and the buckets:

```bash
nats server check jetstream --server nats://127.0.0.1:4222
nats kv info deckr_lease_v1 --server nats://127.0.0.1:4222
nats kv info deckr_discovery_v1 --server nats://127.0.0.1:4222
nats kv ls deckr_lease_v1 --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 --server nats://127.0.0.1:4222
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
nats kv ls deckr_lease_v1 'presence.endpoint.>' --server nats://127.0.0.1:4222
nats kv ls deckr_lease_v1 'claim.device.>' --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 'inventory.hardware.>' --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 'catalog.actions.providers.>' --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 'catalog.services.>' --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 'status.services.>' --server nats://127.0.0.1:4222
nats kv ls deckr_discovery_v1 'view.services.>' --server nats://127.0.0.1:4222
```

When a component appears unavailable, check in this order:

1. Is its endpoint presence key present and carrying the expected endpoint, lane,
   and session id?
2. Is its discovery state present and session-matched, such as inventory for a
   hardware manager, catalog for an action provider instance, or catalog/status
   for a service instance?
3. If a device is claimed, does the claim's controller endpoint/session match
   current controller presence?
4. Did the lease key expire after the 30s TTL because the component stopped
   refreshing it?
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
