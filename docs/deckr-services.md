# Deckr services v1 specification

## Purpose and scope

This specification defines Deckr service-use agreements, protected views, and managed service lifecycles.
Generic Concord validates contract and token mechanics.
Profile owners validate the meaning of terms and other profile data.
Profile policy does not replace Concord authority.

The following documents give related requirements:

| Document | Purpose |
| --- | --- |
| [Schema guide](schema-guide.md) | Schema selection and validation. |
| [Deckr message catalog](deckr-message-catalog.md#services-lane) | Service lane messages. |
| [Semantic validation rules](semantic-rules.md#services) | Service checks that supplement structural validation. |
| [Deckr Action Runtime](deckr-action-runtime.md) | The core Action Runtime service protocol. |

## Technical terms

The terms below use the meanings in [Concord](concord.md#2-technical-terms).
In this document, a client is a service consumer.
Field names and protocol values keep their exact spelling.

| Term | Meaning |
| --- | --- |
| Contract pointer | The pair `contractId` and `generation` that identifies one contract instance. |
| Fence | Identity information that prevents use of authority from a different agreement or session. |
| Lease | Local runtime state for participation in an agreement. |
| Negotiation race | Attempts to make a service-use agreement with different service candidates at the same time. |
| Service candidate | A service endpoint and endpoint session that the client can try in a service-use negotiation. |

## Service package ownership

Service packages can use generic Beacon and Concord with their own identifiers and profiles:

- Feature IDs
- Advertisement payload profiles
- Service-use profiles.

Service packages can use service views directly backed by KV stores.
Package owners define service-specific data, behavior, and view schemas.
Deckr core does not define those rules, such as the rules for Sonos zones or OpenHAB items.

## Service use

### Contract requirements

A service-use contract has an opaque runtime ID.
The service protocol declares its service-use profile, such as `*.service_use.v1`.
The contract must meet these requirements:

| Contract item | Requirement |
| --- | --- |
| Profile | The contract contains the service-use profile. |
| `terms` | The field must be absent. |
| `termsHash` | The field must be absent. |
| Participants | The participants must be the service endpoint and the client endpoint only. |

### Authority and lease lifecycle

After negotiation, the held lease and participant tokens control authority for service messages and protected views.
Continued Beacon advertisement presence is not necessary for that authority.

The following conditions apply to agreements and leases:

| Condition | Requirement or permitted action |
| --- | --- |
| A consumer holds a lease that passes validation. | The consumer can refresh the held lease. |
| Service-use authority is new or has been lost. | A new Concord contract is necessary. |
| Lease state is new or has been lost. | Current discovery and a new opaque Concord contract are necessary. |

## Service messages

Services carry consumer-to-service messages on the core `services` lane.
Each such message must meet these requirements:

- The message contains the exact contract pointer.
- The message names a protocol operation.
- The sender is the peer named in the contract.

Authorization must also enforce any retained resource scope that the service protocol manages.
The [Deckr message catalog](deckr-message-catalog.md#services-lane) describes the service message envelope and body.

### Deferred operations and retained resources

Request admission does not grant authority for later work. The provider must
check the same agreement, request expiry, and retained resource scope before
each backend operation. This includes an operation that waits for a connection,
a lock, or worker-thread capacity. Authority loss must stop pending work.
An operation already sent to an external system cannot always be cancelled.
The provider must not replay that operation under a successor agreement.

A retained resource scope belongs to the agreement pointer, consumer endpoint,
consumer session, and subscriber ID. A scope replacement replaces the complete
resource set for that subscriber. Removal must invalidate earlier pending work,
including when a later replacement adds the same resource again.

The managed provider boundary must reconcile its retained scopes against current
agreement authority. A state watch can coalesce changes. Cleanup must not depend
on observing the addition of each agreement. Failed cleanup must remain owned
and be retried. Cleanup failure must not restore local operation authority.

Scope replacement and view publication must preserve ordering. After a resource
is removed, an earlier publication must not restore its view. A failed or
cancelled scope mutation must revoke that scope and terminate its agreement.
Backend connection attempts can run after scope acceptance. A missing backend
resource does not, by itself, terminate the agreement. Its view must show no
current value, and the provider can retry acquisition while the scope is retained.

## Protected service views

### Storage and fence fields

Protected service views use explicit JetStream KV buckets.
Each entry must carry all the following fence values:

| Fence subject | Fields |
| --- | --- |
| View | `viewKey` |
| Service identity | `serviceId`, `serviceNamespace`, `serviceEndpoint` |
| Service session | `serviceSessionId` |
| Consumer identity | `consumerEndpoint` |
| Consumer session | `consumerSessionId` |
| Writer | `writer` |
| Contract pointer | `contractId`, `generation` |

### Access checks

Protected view access uses the advertised view-family prefixes.
The service identity, session, and contract pointer act as fences.
Protected reads and watches need an active Concord service-use lease and the matching contract pointer.
A namespace mismatch must not expose a payload.

### Watch visibility

A protected view watch must send payloads only when the entry agrees with its service-use fence.
The following requirements apply to changes:

| Condition | Required watch behavior |
| --- | --- |
| A same-key entry changes to a different service identity or session. | Send only a removal-style event. Do not send the replacement payload. |
| An entry that the watcher did not see is deleted or expires. | Do not send a delete or expire event. |

### Cached reads, writes, and recovery

Usual reads use the cached view.
Service views follow the shared requirements in the Deckr NATS binding:

- [Materialized sources and recovery](deckr-nats-binding.md#8-materialized-sources-and-recovery)
- [Public state watches](deckr-nats-binding.md#9-public-state-watches).

A started view store must meet these requirements when a write or delete completes:

- Its local view reflects the committed revision or a newer observation.
- The store waits for ordered watch observations, as specified in [Exact writes and observations](deckr-nats-binding.md#83-exact-writes-and-observations).

Each write must return the committed store revision.
Use that revision for later updates and deletes that need a revision guard.
Recovered snapshots must remove cached keys that are absent from the store.
This includes keys whose delete or expiry events were missed.

## Managed service consumers

### Client responsibilities

A service consumer implementation must have one managed client boundary.
That boundary owns these operations:

- Service candidate discovery through Beacon
- Service descriptor parsing
- Local predicate filtering
- Negotiation races and successor negotiation through Concord
- Request authorization
- Service-view access with the required fences.

Each negotiation attempt must use a new opaque contract ID.

### Service discovery

For each protocol feature ID, the client must use one feature watch.
It must not use a separate feature watch for each service ID.
The client gets the service ID and concrete view key prefixes from each validated service descriptor.
Discovery gives only a candidate for a new service-use negotiation.

Ordinary service consumers must not do these operations themselves:

- Scan raw Beacon KV records.
- Make an exact NATS round trip for discovery.
- Scan all Beacon candidates and parse service descriptors with their own code.
- Maintain a duplicate service-specific Beacon index.
- Classify Concord terminal statuses.
- Query Concord to find available services.

### Matching-service negotiation

A matching-service negotiation admits a maximum of 64 unique service candidates that match the local predicate.
Each unique endpoint and session pair counts as one service candidate.

The client must meet these requirements during negotiation:

- The client negotiates with all admitted candidates at the same time.
- The client includes later candidates while negotiation stays pending. The same limit of 64 unique candidates applies.
- Before the client accepts the winner, all other attempts give up local authority.

### Exact-candidate negotiation

An exact-candidate negotiation uses the exact candidate from the supplied service descriptor.

### Accepted agreements and recovery

After acceptance, Beacon withdrawal or replacement must not change the winner's lifecycle.
If the winner loses authority, the client must do these actions:

- Publish `reconnecting`.
- Start a new negotiation race.

The client must not replay published commands under a different contract.

The client must keep failures from cleanup of earlier agreements available as
diagnostics. A failure from an earlier recovery must not, by itself, make a
later successful client shutdown fail. Shutdown must report failures from
cleanup that is still pending or starts during that shutdown.
This reporting rule does not restore earlier authority or remove the
[Concord cleanup requirements](concord.md#92-actions-after-a-heartbeat-failure).

## Managed service providers

Service providers must follow the [managed lifecycle requirements](deckr-messaging.md#managed-lifecycle-boundaries).
These requirements assign protocol operations to the managed lifecycle boundary.
Application code supplies backend behavior and domain policy.

### Startup with unresolved ownership

Before advertisement publication, a managed provider must complete the
[Concord owner bootstrap checks](concord.md#11-owner-bootstrap-and-competing-sessions).
If ownership is unresolved, the provider must stay unavailable and retry those
checks automatically. It must not publish its advertisement or accept new
service-use agreements during this wait.

The wait must not have a retry-count limit or an overall timeout. Each attempt
must have a finite time limit. The runtime must wait between failed attempts or
wait for a relevant source change. Cancellation or provider shutdown must stop
the wait and prevent later publication.

A token from an earlier session must not be treated as expired because the new
provider started. After ownership is resolved, the provider must complete
bootstrap before publication. It must not restore authority from the earlier
session. These requirements also apply after a provider process restarts.

## Implementation guidance

Use the reusable service schemas:

| Schema | Purpose |
| --- | --- |
| [Protocol definition](../contract/v1/schemas/deckr/services/protocol-definition.v1.schema.json) | A service protocol declaration. |
| [Advertisement payload](../contract/v1/schemas/deckr/services/service-advertisement-payload.v1.schema.json) | Service discovery data. |
| [Message body](../contract/v1/schemas/deckr/services/service-message-body.v1.schema.json) | A service message body. |
| [View record](../contract/v1/schemas/deckr/services/view-record.v1.schema.json) | A protected service view record. |

Publish a protocol catalog under the owner's namespace.
Include the following information:

- Operations
- Message directions
- Payload schemas
- View families.

The [Action Runtime catalog](../contract/v1/catalogs/action-runtime-protocol.v1.json) is a complete example.
The catalog is a reference declaration.
It is not a new lane message.

## openHAB and Sonos service profiles

The [openHAB protocol catalog](../contract/v1/catalogs/openhab-service-protocol.v1.json)
and [Sonos protocol catalog](../contract/v1/catalogs/sonos-service-protocol.v1.json)
define the application operation names and payload references.
Use the [openHAB message entry point](../contract/v1/schemas/deckr/openhab/messages.v1.schema.json)
or [Sonos message entry point](../contract/v1/schemas/deckr/sonos/messages.v1.schema.json)
for a complete request or reply. The generic service schema does not validate
these application payloads.

`setItemScope` and `setZoneScope` replace the complete resource set for one
subscriber in one live service-use contract. An empty array removes the set.
Each acknowledgement has an empty object result. The acknowledgement does not
assert that the external resource is available. An unavailable resource has no
current view. The service retains the desired set and can retry acquisition while
the contract is valid.

`sendCommand` requires the item in the same subscriber scope. Sonos operations
with a `zone` require that zone in the same subscriber scope. `listZones` can omit
both `zone` and `subscriberId`; if it has one, it must have both. Global music
queries use the live service-use contract without a resource scope.
Parameters reject unknown fields. Identifiers must not have leading or trailing
whitespace. Integer fields must not contain booleans. Volume, delta, step, result
limit and offset bounds are in the payload schemas. A playback reference must
supply the metadata field for its format. Additional external music fields are
permitted only in the result objects that the schema marks as open.

The [openHAB view payload](../contract/v1/schemas/deckr/openhab/view-payload.v1.schema.json)
and [Sonos view payload](../contract/v1/schemas/deckr/sonos/view-payload.v1.schema.json)
are application data. Stored views must also satisfy the protected service-view
record schema and its authority rules. Payload validation does not grant authority.
