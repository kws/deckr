# Deckr services v1 specification

This specification defines Deckr service-use agreements, protected views, and managed service lifecycles.
Generic Concord validates contract and token mechanics.
Profile owners validate the meaning of terms and other profile data.
Profile policy does not replace Concord authority.

See the [schema guide](schema-guide.md), [Deckr message catalog](deckr-message-catalog.md#services-lane),
and [semantic validation rules](semantic-rules.md#services).
The [Deckr Action Runtime specification](deckr-action-runtime.md) defines the core Action Runtime service protocol.

## Service use

Service-use contracts have opaque runtime ids. They contain the service-use profile without service-specific `terms`. The contract must not have `terms` or `termsHash`.
Service protocols declare their use profile, such as `*.service_use.v1`.

The participants must be the service endpoint and the client endpoint only. New or lost service-use authority needs a new Concord contract.

After negotiation, the held lease and participant tokens control service-message and protected-view authority. Continued Beacon advertisement presence is not necessary for that authority.

A consumer-to-service message must contain the exact contract pointer. It must name a protocol operation. The sender must be the peer named in the contract.
Authorization must also enforce any retained resource scope that the service
protocol manages. Services carry these messages on the core `services` lane.

Protected view access uses the advertised view-family prefixes and the service identity, session, and contract pointer as fences. A consumer can refresh a held lease while it passes validation. New or lost lease state needs current discovery and a new opaque Concord contract.

Protected service views use explicit JetStream KV buckets. Each entry must carry
the `viewKey`, `serviceId`, `serviceNamespace`, `serviceEndpoint`,
`serviceSessionId`, `consumerEndpoint`, `consumerSessionId`, `writer`,
`contractId`, and `generation` fence values. Protected reads
and watches need an active Concord service-use lease and the matching contract
pointer. A namespace mismatch must not expose a payload.

Service views use the shared materialized-source recovery and public state-watch
requirements from the [Deckr NATS binding specification](deckr-nats-binding.md#8-materialized-sources-and-recovery).
Usual reads use the cached view. After a started view store completes a write or
delete, its local view must reflect the committed revision or a newer observation.
The store waits for ordered watch observations as specified in
[Deckr NATS binding section 8.3](deckr-nats-binding.md#83-exact-writes-and-observations).
Recovered snapshots must remove cached keys that are absent from the store.
This includes keys whose delete or expiry events were missed.
Writes return the committed store revision for revision-guarded updates and deletes.

A protected view watch must send payloads only when the entry agrees with its service-use fence. If a same-key entry changes to a different service identity or session, the watcher receives only a removal-style event. It must not receive the replacement payload. Do not send delete or expire events for entries that the watcher did not see.

## Service package ownership

Service packages can use generic Beacon and Concord with their own feature IDs,
advertisement payload profiles, and use profiles. They can use service views
directly backed by KV stores.

Deckr core does not define service-specific data or behavior, such as Sonos zones
or OpenHAB items. Package owners define those rules and their view schemas.

## Managed service consumers

A service consumer implementation must have one managed client boundary that
owns these operations:

- Service candidate discovery through Beacon.
- Service descriptor parsing.
- Local predicate filtering.
- Negotiation races and successor negotiation through Concord.
- Request authorization.
- Service-view access with the required fences.

For each protocol feature ID, the client must use one feature watch.
It must not use a separate feature watch for each service ID.
The client gets the service ID and concrete view key prefixes from each validated
service descriptor. Discovery gives only a candidate for a new service-use
negotiation.

Ordinary service consumers must not do these operations themselves:

- Scan raw Beacon KV records.
- Make an exact NATS round trip for discovery.
- Scan all Beacon candidates and parse service descriptors with their own code.
- Maintain a duplicate service-specific Beacon index.
- Classify Concord terminal statuses.
- Query Concord to find available services.

A matching-service negotiation admits a maximum of 64 unique service candidates
that match the local predicate. Each unique endpoint and session pair counts as
one service candidate. The client must negotiate with all admitted candidates at
the same time. It must also include later candidates while negotiation stays
pending. The same limit applies.

Each attempt must use a new opaque contract ID. Before the client accepts the
winner, all other attempts must give up local authority.

If the winner loses authority, the client must publish `reconnecting`. It must
start a new negotiation race. The client must not replay published commands under
a different contract.

After acceptance, Beacon withdrawal or replacement must not change the winner's
lifecycle. An exact-candidate negotiation negotiates with the exact candidate from the supplied
service descriptor.

## Managed service providers

Service providers must follow the
[managed lifecycle requirements](deckr-messaging.md#managed-lifecycle-boundaries).
These requirements assign protocol operations to the managed lifecycle boundary.
Application code supplies backend behavior and domain policy.

## Implementation guidance

Use the reusable service schemas:

- [Protocol definition](../contract/v1/schemas/deckr/services/protocol-definition.v1.schema.json).
- [Advertisement payload](../contract/v1/schemas/deckr/services/service-advertisement-payload.v1.schema.json).
- [Message body](../contract/v1/schemas/deckr/services/service-message-body.v1.schema.json).
- [View record](../contract/v1/schemas/deckr/services/view-record.v1.schema.json).

Publish a protocol catalog under the owner's namespace.
Include operations, message directions, payload schemas, and view families.
The [Action Runtime catalog](../contract/v1/catalogs/action-runtime-protocol.v1.json) is a complete example.
The catalog is a reference declaration. It is not a new lane message.
