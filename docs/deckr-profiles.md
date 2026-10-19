# Deckr application profiles

These requirements apply to Deckr, not to application-independent Beacon or Concord.
See [schemas and composition](schemas.md), [message catalog](message-catalog.md), and
[semantic rules](semantic-rules.md).

## Hardware discovery


Deckr core owns the Beacon profile `dev.deckr.profile.hardware.v1`.
This profile gives information about hardware devices, controls, and capabilities.
Its payload must agree with these identity conditions:

| Payload field | Required relationship |
| --- | --- |
| `sessionId` | The value must be the same as the advertisement `sessionId`. |
| `managerEndpoint` | The value must be the same as the advertisement `endpoint`. |
| `managerId` | The value must be the same as the hardware-manager endpoint ID. |

The hardware Beacon profile is the only public source for device inventory.
The hardware lane is named `hardware_messages`.
Hardware lane messages do not announce inventory lifecycle changes.
Hardware lane messages do not replace the device inventory published through
Beacon.
They can carry input, commands, capability state, and replies. These messages
must not replace Beacon discovery or Concord authority.

Beacon can identify a device before an endpoint makes a claim. Beacon does not
give device ownership. A missing hardware advertisement alone does not withdraw
an existing device claim. The [hardware claim profile](#hardware-claims)
gives the claim requirements.


## Agreement and service profiles


Generic Concord validates contract and token mechanics. Profile owners validate the meaning of terms and other profile data. Profile policy does not replace Concord authority.
Deckr core owns the hardware claim profile and the action runtime service protocol below.

### Hardware claims

The hardware claim profile is `dev.deckr.profile.hardware_claim.v1`.

Its terms identify a controller endpoint, a hardware-manager endpoint, and one or more claimed devices. Device ids in one claim must be unique. `instanceCount` must be greater than zero.

Participants validate ownership through the contract, tokens, endpoint, session, and device refs. Hardware-manager and profile policy control single ownership and capacity over valid Concord claims.

If a claimed device disconnects, the hardware manager must cancel or end the claim, or stop token maintenance. A missing hardware advertisement alone does not withdraw the claim.

The v1 protocol expects claimed device descriptors to stay stable. A material descriptor change uses claim cancellation or token loss, followed by a new discovery candidate. Hardware lane lifecycle messages do not specify this change.

### Service use

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
requirements from the [NATS specification](nats.md#8-materialized-sources-and-recovery).
Usual reads use the cached view. After a started view store completes a write or
delete, its local view must reflect the committed revision or a newer observation.
The store waits for ordered watch observations as specified in
[NATS section 8.3](nats.md#83-exact-writes-and-observations).
Recovered snapshots must remove cached keys that are absent from the store.
This includes keys whose delete or expiry events were missed.
Writes return the committed store revision for revision-guarded updates and deletes.

A protected view watch must send payloads only when the entry agrees with its service-use fence. If a same-key entry changes to a different service identity or session, the watcher receives only a removal-style event. It must not receive the replacement payload. Do not send delete or expire events for entries that the watcher did not see.

### Action runtime service

The action runtime service protocol is `dev.deckr.action_runtime.provider`.

The service id must be `action-runtime.<providerInstanceId>`. The endpoint must be `service:<serviceId>`.
The provider publishes action availability through the contract-fenced
`action_availability` view.

A controller gets authority through the service-use contract. It then reads the contract-fenced `action_availability` view and sends runtime lifecycle messages on the `services` lane. Individual control bindings are controller-owned routing state.

After negotiation, Beacon is not part of the service-use contract lifecycle or validity. Package owners specify service-specific domain semantics and view schemas.

### Service package ownership

Service packages can use generic Beacon and Concord with their own feature IDs,
advertisement payload profiles, and use profiles. They can use service views
directly backed by KV stores.

Deckr core does not define service-specific data or behavior, such as Sonos zones
or OpenHAB items. Package owners define those rules and their view schemas.

### Managed service consumers

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

### Managed providers and hardware managers

Service providers and hardware managers require managed lifecycle boundaries.
These boundaries own these operations:

- Owner bootstrap.
- Participant selection.
- Private worker control.
- Authorization.
- Advertisement publication after bootstrap.
- Cleanup within the required time limits.

Application code supplies protocol intent, backend or device behavior, and domain
policy. The managed lifecycle boundaries own token refresh, reconciliation,
contract retries, background workers, and raw contract pointers. Application code does not manage those
operations itself.

Application code supplies domain facts, such as device connection changes and
backend availability. Profile implementations must validate profile data and
apply the specified consequences of those facts. Generic Concord must validate
contract and token identity, session assertions, and authority.

Application policy must not replace these checks. It must not maintain accepted
contract sets in response to Concord validity transitions. Managed contexts must
enforce local authority before protected operations. They must initiate required
protocol termination and cleanup without an application or operator request.
Local authority loss must not wait for a domain callback.
