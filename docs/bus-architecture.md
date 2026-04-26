# Bus Architecture

This document is normative architecture for Deckr's message bus and routing
model.

It describes the Deckr message layer. It does not describe adapter-private
protocols such as the Elgato plugin WebSocket protocol, Python plugin runtime
control-plane messages, concrete hardware HID protocols, or any other protocol
used behind an adapter.

Deckr is in ALPHA. If the implementation disagrees with this document, fix the
implementation.

## Motivation

This document exists because the current implementation half-introduced a shared
transport layer without first defining Deckr's own routing requirements.

That caused the same design problem to appear in several places under different
names:

- remote hardware manager disconnects require local device cleanup
- plugin host disconnects require local action cleanup
- transport loops require origin and forwarding policy
- hardware routing currently smuggles manager identity through device ids
- plugin routing currently smuggles bus routing through plugin-host protocol
  messages
- local-only controller events currently share lanes with transported messages
- endpoint, runtime, transport, device, action, and context identities currently
  overlap in ways that make routing rules harder to reason about

The common issue is reachability. Deckr needs to know which logical endpoints
are reachable through which client, connection, process, or bridge. When that
client disappears, Deckr should model route loss once and let domain services
react to it.

This is not a payload-shape cleanup. Payload models should be revised after the
bus routing and identity model is clear.

## Scope

Deckr has three separate concerns that must not be collapsed together:

- Deckr message protocol
  - logical messages between Deckr architectural endpoints
  - examples: controller to plugin host, plugin host to controller, hardware
    manager to controller, controller to hardware manager
- Transport protocol or messaging substrate
  - how Deckr messages cross a process, host, or network boundary
  - examples: local in-memory bus, MQTT, WebSocket, NATS, Dapr-backed pub/sub,
    future Redis
- Adapter-private protocol
  - protocols that exist behind a Deckr adapter
  - examples: Elgato plugin runtime messages, Python plugin worker messages,
    concrete device protocols

Only the first two concerns belong in core Deckr bus architecture. Adapter-private
protocols must adapt into Deckr messages at their boundary.

## Out Of Scope

This document does not define:

- Elgato's plugin protocol
- Python plugin runtime WebSocket messages
- Python plugin host control-plane HTTP APIs
- concrete hardware protocols such as HID reports
- plugin SDK method names or callback shapes
- final per-message payload schemas for `plugin_messages`
- controller rendering, navigation, settings storage, or profile policy

Those systems matter only at the point where they adapt into or out of Deckr
messages.

## Core Principle

All transports that carry a Deckr lane must expose the same Deckr messaging
semantics to the application layer.

An application component should not need to know whether a message arrived:

- in process
- over WebSocket
- over MQTT
- through a future transport

Transport-specific details must be hidden below the Deckr message layer.

This does not mean every transport component must implement a complete bus. The
application-facing lane API should be shared infrastructure. Transports attach to
that infrastructure at the boundary.

## Substrate Boundary

Deckr defines its domain contracts. It should not invent generic messaging
infrastructure unless a concrete Deckr requirement cannot be satisfied by a
well-known substrate.

Terms:

- Deckr message protocol
  - the Deckr-owned envelope, lane contracts, endpoint addresses, subjects,
    route/control events, and lane delivery expectations
- messaging substrate
  - generic delivery infrastructure below Deckr, such as NATS, Dapr-backed
    pub/sub, MQTT, WebSocket, Redis, or an in-process queue
- transport adapter
  - a Deckr component that maps a concrete substrate into Deckr lanes without
    changing lane semantics
- bridge
  - an adapter/topology role that forwards Deckr messages across process, host,
    network, or trust-domain boundaries
- substrate delegation
  - an explicit decision to let the substrate provide generic mechanics such as
    broker fan-out, request/reply inboxes, queue groups, replay, redelivery,
    message TTL, dead-letter handling, persistence, or edge/leaf topology

Application components speak Deckr lanes and Deckr envelopes. They must not use
NATS subjects, Dapr component names, MQTT topics, WebSocket sessions, Redis stream
ids, or any other substrate locator as Deckr application routing concepts.

Adopting a substrate must be represented as an explicit Deckr component with
explicit per-lane bindings, route policy, and trust policy. A substrate may be a
broker, embedded server, sidecar, external service, or local in-process
implementation, but it must not become a second Deckr component model, discovery
model, endpoint identity model, or domain API.

## Substrate Evaluation Gate

Before Deckr implements or promotes generic infrastructure for remote routing,
bridge topology, durable delivery, replay, redelivery, duplicate windows,
request/reply plumbing, queue groups, dead-letter handling, TTL handling, or
cross-domain forwarding, the architecture must evaluate existing substrates
first.

At minimum, the evaluation should consider:

- NATS for pub/sub, request/reply, queue groups, leaf-node or edge topology, and
  JetStream-backed persistence, replay, and duplicate detection when durability
  is needed
- Dapr for a component/building-block boundary, platform-agnostic pub/sub,
  pluggable brokers, CloudEvents wrapping, consumer groups, TTL, and dead-letter
  behavior where supported by the selected component
- CloudEvents and AsyncAPI for envelope, schema, binding, and interoperability
  discipline
- Apache Camel and Enterprise Integration Patterns for vocabulary and design
  patterns, without assuming the Camel runtime belongs inside Deckr
- MQTT and WebSocket custom bridges as useful alpha adapters, not as the default
  reason to build durable broker features ourselves

The evaluation must record which semantics Deckr adopts, which mechanics are
delegated to the substrate, and which substrate concepts are deliberately kept
below the Deckr lane API.

## Architectural Posture

Deckr should keep the proven parts of message bus and service bus architecture:

- explicit message contracts
- adapters at protocol boundaries
- routing metadata outside payload bodies
- correlation for request/reply
- idempotency and duplicate detection
- observability through trace and message history
- clear handling of malformed, expired, or undeliverable messages

Deckr must not recreate the old "smart ESB" failure mode where the bus becomes
the place for product workflow, domain decisions, transformation policy, or
cleanup behavior that belongs to a domain service.

The bus is a routing, reachability, and protocol layer above any concrete
messaging substrate. Endpoints are where domain behavior lives.

The design priorities are:

- smart endpoints, simple pipes
- explicit contracts over implicit conventions
- stable logical identities over topology-derived names
- structured identity over delimiter-smuggled strings
- transport-neutral semantics over transport-specific behavior
- observable failure over silent loss
- local and transported behavior that follows the same lane contract
- shared routing and fan-out machinery over duplicated per-transport bus logic

## Identity And Addressing

Identity is a first-class architectural concern. Deckr IDs must not carry
routing, lifecycle, transport, and domain meaning in the same string.

Deckr uses these separate identity concepts:

- stable identifier
  - a durable name for an entity within a stated scope
  - examples: `controller_id`, `host_id`, `manager_id`, manager-local
    `device_id`, host-local `action_id`
- endpoint address
  - a routable Deckr protocol address for a logical participant
  - examples: `controller:<controller_id>`, `host:<host_id>`,
    `hardware_manager:<manager_id>`
- entity reference or subject
  - the domain entity a message is about, separate from where the message is
    routed
  - examples: device, slot/control, action, context, profile, page
- client or session id
  - an ephemeral routing attachment used by the routing layer
  - examples: WebSocket connection, MQTT peer, bridge session, in-process
    attachment
- transport address
  - a concrete locator in a transport substrate
  - examples: MQTT topic, WebSocket path or URI, Redis stream
- component identity
  - the runtime-host/discovery identity of a component type
  - examples: `deckr.controller`, `deckr.transports.mqtt`
- component runtime identity
  - the runtime-host-scoped lifecycle identity of one configured component
    instance
  - examples: `deckr.transports.mqtt:main`

These concepts must remain separate even when one current implementation happens
to derive one from another.

### Address Rules

Deckr protocol addresses are application data. They must be stable enough to
appear in configuration, persisted state, logs, traces, and remote messages.

A Deckr endpoint address has this conceptual shape:

```text
<endpoint_family>:<endpoint_id>
```

Core endpoint families are reserved by Deckr. Third-party endpoint families must
use globally namespaced family names owned by the extending system.

Address rules:

- an endpoint address identifies a logical endpoint, not a socket, process,
  topic, component instance, or transport binding
- address strings are opaque outside typed constructors, validators, and parsers
- endpoint ids must be non-empty, normalized, and safe to carry through JSON,
  TOML, logs, URLs, MQTT topics, and WebSocket messages without ambiguous
  parsing
- reserved delimiters used by the address grammar must not appear unescaped
  inside endpoint ids
- case-sensitivity must be defined once and then treated consistently
- address aliases are migration tools, not permanent routing semantics
- topology-derived names are allowed only for ephemeral clients and sessions, not
  for durable endpoint addresses

Do not parse arbitrary strings in application code to discover routing meaning.
If an address or entity reference has internal structure, it needs one canonical
constructor and one canonical parser in the core protocol package.

### Scopes

Every identifier must have an explicit scope.

Recommended scopes:

- `controller_id`
  - unique within a Deckr deployment domain
- `host_id`
  - unique within the deployment domain where it advertises actions
- `manager_id`
  - unique within the deployment domain where it advertises hardware
- `device_id`
  - unique only within a hardware manager unless explicitly promoted to a
    controller-visible device reference
- `slot_id` or `control_id`
  - unique only within one device
- `action_id`
  - unique only within one plugin host or plugin namespace, depending on the
    action contract
- `context_id`
  - owned by a controller and scoped to that controller's interaction domain
- `message_id`
  - unique within the sender/source; the sender/source plus message id is the
    duplicate-detection key

If a message needs a globally meaningful reference to a scoped entity, it must
carry a structured reference or explicit envelope subject. It must not widen the
scope by concatenating scoped ids into a different id field.

### Subjects

Routing answers "who should receive this message?" Subject answers "what is this
message about?"

The envelope recipient should usually be an endpoint address or explicit
broadcast target. The envelope subject should identify the domain entity being
affected.

Examples:

- a hardware command is routed to `hardware_manager:<manager_id>` and is about a
  device/control subject
- a key event is sent by `hardware_manager:<manager_id>` and is about a
  device/control subject
- a plugin command is sent by `host:<host_id>` to a controller endpoint and is
  about a controller-owned context subject
- an action advertisement is sent by `host:<host_id>` and is about one or more
  action subjects

Subjects may be represented as structured fields or canonical subject strings,
but they must not be confused with endpoint addresses.

## Endpoints

A Deckr endpoint is a logical participant in the Deckr architecture.

Core endpoint families include:

- `controller:<controller_id>`
- `host:<host_id>`
- `hardware_manager:<manager_id>`

Endpoint identity is not the same as:

- component lifecycle identity
- Python object identity
- transport instance identity
- WebSocket connection identity
- MQTT client identity
- process identity

A component may expose one endpoint, multiple endpoints, or no routable endpoint.
A transport client may provide reachability to one endpoint, many endpoints, or a
route to endpoints behind it.

An endpoint address is claimed by a component through local configuration or by a
remote client through a reachability announcement. A client must not be trusted
to claim arbitrary endpoint addresses merely because it is connected to a
transport.

The route manager must be able to reject, replace, or quarantine endpoint claims
that conflict with existing ownership, configured trust policy, or endpoint
family rules.

## Clients And Reachability

Deckr must model reachability separately from endpoint identity.

A client is a routing attachment. It is more like a network router or session
than a Deckr architectural component.

Examples of clients:

- an in-process component attachment
- a WebSocket connection
- an MQTT transport peer
- a future Redis subscriber
- a process that hosts one or more Deckr endpoints
- a bridge that forwards traffic to endpoints behind it

When a client disappears, Deckr must not interpret that as "one specific domain
object was deleted". It means routes through that client are no longer available.
Domain services then clean up domain state that depended on those routes.

Examples:

- if a hardware manager endpoint becomes unreachable, devices known only through
  that endpoint become unreachable
- if a plugin host endpoint becomes unreachable, actions known only through that
  host become unavailable
- if a bridge endpoint becomes unreachable, every endpoint reachable only through
  that bridge becomes unreachable

This is the common model behind hardware disconnect cleanup, plugin host
disconnect cleanup, and future bridge/router cleanup.

## Route State

The bus layer should be able to represent:

- client connected
- client disconnected
- endpoint reachable through client
- endpoint no longer reachable through client
- message originated from client
- message should be sent toward a specific client, endpoint, or broadcast scope
- endpoint claim accepted, rejected, or superseded
- route lease renewed or expired
- endpoint capabilities advertised or withdrawn

This route state is not the same as a message payload.

Route state may be represented by bus metadata, by explicit route events, or by a
dedicated routing service, but it must be part of the Deckr architecture rather
than a one-off behavior in each transport or lane handler.

A route record should include at least:

- endpoint address
- lane scope
- client/session id through which the endpoint is reachable
- client kind
- whether the endpoint is direct or behind a bridge
- transport kind and transport instance for diagnostics
- route lease or last-seen timestamp
- capabilities relevant to routing decisions
- claim source and trust status

Alpha route claims are lane-scoped. A claim for `host:python` on
`plugin_messages` does not make that endpoint reachable on `hardware_messages`
or on an extension lane. If a participant needs the same logical endpoint
reachable on multiple lanes, it must claim the endpoint separately on each lane.

Local in-process claims are authoritative for their lane. A local claim may
replace a remote claim for the same lane and endpoint, and a remote claim may not
replace a local claim.

Remote claims require an explicit claim source and trust status. The current
implementation supports:

- `message_sender`
  - a direct untrusted route inferred from a valid Deckr envelope arriving from a
    transport client
- `transport_route`
  - an explicit route assertion made by a transport or bridge

Remote claims are accepted only when the lane policy allows the claimed endpoint
family. For core lanes:

- `plugin_messages` may learn `host` and `controller` endpoint routes
- `hardware_messages` may learn `hardware_manager` and `controller` endpoint
  routes

Untrusted remote direct claims are accepted only within the lane policy. Bridged
claims require trusted route authority. Conflicts are exclusive per lane and
endpoint: local wins over remote, higher trust may replace lower trust, and equal
authority conflicts are rejected until the owning route is withdrawn or the
client disconnects. Rejections surface as `endpointClaimRejected` control-plane
events.

## Deckr Message Envelope

A Deckr message needs transport-neutral routing metadata.

The logical envelope should include:

- message id
  - unique identity within the sender/source for tracing, dedupe, and
    request/reply correlation
- protocol version
  - the Deckr envelope version
- schema or message contract version
  - the version of the lane message contract being carried
- optional in-reply-to id
  - correlation to an earlier message
- optional causation id
  - the message that caused this message to be emitted
- optional trace context
  - distributed tracing context that can cross transports
- lane or protocol family
  - the logical Deckr lane contract being carried
- message type
  - the typed operation, event, command, or reply within the lane
- sender endpoint
  - a Deckr endpoint address
- recipient endpoint or broadcast scope
  - a Deckr endpoint address or explicit broadcast target
- subject
  - the entity the message is about, such as a device, slot, context, or action
- created-at timestamp
  - when the sender created the message
- optional expiry or ttl
  - when the message should no longer be considered useful
- message body
  - the typed Deckr message for that lane

Transport-local fields such as MQTT topic, WebSocket path, socket id,
connection id, transport id, retry counters, and loop-prevention hop metadata do
not belong in the Deckr message body.

Forwarding metadata such as origin client, current client, hop count, route
history, and bridge policy may live in route metadata associated with the
envelope. It must remain separate from the lane body.

The envelope should be boring and explicit. It is the place where generic
middleware can make routing, tracing, logging, validation, and duplicate
detection decisions without understanding lane payloads.

## Broadcast

Broadcast is a Deckr routing concept, not a transport feature.

Known broadcast scopes include:

- all plugin hosts in a controller domain
- all controllers in a deployment domain
- potentially all hardware managers in a controller domain

Broadcast scope must be explicit. Do not rely on MQTT topic fan-out, WebSocket
server fan-out, or local subscriber fan-out as the application-level meaning of
broadcast.

Broadcast targets are not endpoint addresses. Names such as `all_hosts` and
`all_controllers` are current protocol conveniences, but the long-term
architecture should represent broadcast as an explicit target with:

- scope
- domain
- intended endpoint family
- forwarding policy
- optional hop limit

Receivers should be able to distinguish "this was addressed directly to me" from
"this reached me through a broadcast scope" without interpreting transport
fan-out behavior.

## Lanes

A lane is a logical Deckr contract.

Core lanes currently include:

- `hardware_messages`
- `plugin_messages`

Lane meaning must be identical whether the lane is local or transported.

A transport may carry multiple lanes. A lane may be carried by multiple
transports. Neither fact may change the shape or routing semantics of messages
on that lane.

A lane contract must define:

- valid message types
- envelope requirements
- message body schemas and schema versions
- allowed sender endpoint families
- allowed recipient endpoint families or broadcast targets
- delivery expectations
- ordering key, if any
- idempotency and duplicate-handling expectations
- local-only message types, if any
- whether messages may be bridged beyond the first transport hop

Transports bind to lanes. They do not define lane semantics.

The device, control, and capability contracts carried by `hardware_messages` are
defined in [device-capabilities-architecture.md](device-capabilities-architecture.md).

## Shared Bus Responsibilities

The local lane bus is the application-facing messaging surface for one Deckr
runtime. It is not merely a transport option alongside MQTT or WebSocket.

The shared bus or routing layer should own the Deckr-level semantics for
reusable mechanics that every component would otherwise need to duplicate,
including:

- the application-facing `send` and `subscribe` API for lane participants
- local fan-out to multiple in-process subscribers
- subscriber lifecycle and local backpressure policy
- envelope validation and metadata normalization
- recipient selection from endpoint addresses or broadcast targets
- expansion of broadcast scopes into concrete delivery decisions
- route table lookup and endpoint reachability state
- route lease expiry and lease-loss control-plane reporting
- duplicate detection and forwarding policy where those are generic to the lane
- control-plane route events such as endpoint reachable or unreachable
- common observability hooks for message ids, traces, and route history

Owning the Deckr-level semantics does not require hand-building every storage,
broker, or network mechanic. When a standard substrate is adopted, the adapter
maps Deckr semantics onto substrate features and keeps substrate-specific details
below the lane API.

Transport components should use the shared lane API rather than expose a second
application-facing bus API.

A transport may still need transport-local fan-out, buffering, retry, or
subscription mechanics to talk to its substrate. For example, a WebSocket server
may write to multiple connected sockets, and an MQTT adapter may subscribe to a
topic. Those are transport delivery details. They must not become separate Deckr
lane semantics.

The responsibility split is:

- lane bus
  - owns local send/subscribe/fan-out and the common lane contract seen by
    application components
- routing service
  - owns Deckr endpoint reachability, route selection, broadcast expansion,
    route claims, leases, duplicate policy, loop policy, and trust decisions
- managed lane runtime
  - starts required generic bus infrastructure exactly once per shared route table,
    including route lease expiry
- transport component
  - owns connection/session management, concrete transport framing, substrate
    publish/subscribe calls, and translating remote frames into Deckr messages
    on the shared lane
- messaging substrate
  - may own generic delivery mechanics such as physical pub/sub fan-out, reply
    subjects or inboxes, consumer or queue groups, durable streams, replay,
    redelivery, message TTL, dead-letter movement, and edge/leaf routing
    topology
- domain service
  - owns domain behavior and cleanup reactions after route state changes

The implementation may combine the lane bus and routing service in one module at
first, but the responsibilities must remain conceptually separate.

Route lease expiry is active runtime behavior. The routing service defines the
expiry operation and emits the resulting control-plane events. The managed lane
runtime drives that operation on time. Expiry must not be implemented as a
transport-local cleanup hook, a controller responsibility, or a lazy side effect
of the next message.

## Hardware Routing

Hardware routing has three identities that should remain distinct:

- hardware manager endpoint
  - the logical owner/router for one or more devices
- device id
  - the hardware-manager-local identity of a device unless explicitly wrapped in
    a controller-visible device reference
- slot/control id
  - the identity of a control within a device

The current encoded remote device id shape, such as
`manager=<manager_id>|device=<device_id>`, is a symptom of missing first-class
hardware manager routing. It works as an implementation technique, but it should
not be the long-term architecture boundary.

Long-term hardware messages should route to the hardware manager endpoint
responsible for the device. The affected device and slot/control should be
carried as structured subject data or as an explicit device/control reference.

Controller-to-hardware commands should route to `hardware_manager:<manager_id>`
without leaking transport identity into the application layer and without
encoding manager identity into `device_id`.

Hardware-manager disconnect should be modeled as loss of reachability to that
manager and to devices only reachable through that manager.

If hardware does not provide a stable physical id, the hardware manager owns the
policy for minting and persisting a stable manager-local device id. The bus must
not infer physical identity from transport topology.

## Plugin Host Routing

Plugin host routing has these identities:

- controller endpoint
- plugin host endpoint
- action id
- resolved action address
- context id

Action ids are scoped to a plugin host or plugin namespace. A controller-visible
action address must include that scope explicitly. The current
`host_id::action_uuid` form is an implementation shape, not a bus routing
primitive.

The controller owns contexts. A context id is a controller-owned interaction
handle. Plugins should treat it as opaque. If the controller needs the context to
resolve to a controller domain, device, slot/control, profile, or page, that
mapping belongs to the controller. It must not become a substitute for envelope
sender, recipient, or subject metadata.

Plugin host disconnect should be modeled as loss of reachability to the plugin
host endpoint. The action registry should react by marking actions from that
host unavailable.

This must work the same way for local and transported plugin hosts.

## Request And Reply

Request/reply is a Deckr message concern.

The bus architecture must support:

- a unique message id on the request
- an in-reply-to value on the reply
- the original logical sender and recipient
- application-visible reply routing and correlation based on endpoint addresses
  and message ids, not transport-local reply topics or session ids
- timeout or missing-reply behavior owned by the requester
- duplicate replies and late replies that can be recognized and ignored

Settings requests are the current core example, but the mechanism is generic.

Request/reply must not imply synchronous behavior. It is a correlation pattern
over messages.

A substrate adapter may use substrate-native request/reply mechanics internally,
such as reply subjects, inboxes, connection-scoped callbacks, or temporary
subscriptions. Those are delivery details. They must not replace Deckr envelope
message ids, `in-reply-to`, logical sender and recipient addresses, or requester
timeout behavior.

## Delivery Semantics

Delivery semantics are part of each lane contract and must not be accidental
side effects of the current local bus or one transport implementation.

Each core lane must specify:

- whether messages are ephemeral or durable
- whether delivery is best-effort, at-most-once, or at-least-once
- whether a message type is idempotent by definition
- how duplicate delivery is detected
- whether ordering matters and which subject or key defines the ordering scope
- how slow consumers are handled
- how malformed or unsupported messages are reported
- whether expired messages are dropped, reported, or moved to a dead-letter lane
- whether transport reconnect should replay, resync, or only resume new traffic

Deckr should not promise exactly-once delivery. Message handlers that cross
process or network boundaries should be safe under duplicate delivery unless the
lane contract explicitly says otherwise and explains how that guarantee is
provided.

For local runtime notifications that are intentionally ephemeral, the lane should
say so explicitly. Ephemeral local behavior must not silently define the
transported protocol.

If a lane chooses durable, replayable, at-least-once, TTL, or dead-letter
behavior, the design should first decide whether that behavior is delegated to a
substrate or implemented in Deckr. The lane contract must describe the observable
Deckr semantics either way.

## Transport Responsibilities

A transport component is responsible for:

- connecting to its transport substrate
- mapping configured transport bindings to Deckr lanes
- attaching to the shared lane bus for application-facing send/subscribe
- carrying Deckr message envelopes unchanged across the boundary
- mapping Deckr envelope metadata to substrate headers, subjects, routes, or
  component metadata when needed, without changing Deckr message meaning
- maintaining client/session identity for route bookkeeping
- authenticating or otherwise establishing trust for remote endpoint claims when
  the transport supports it
- preventing transport-level echo loops
- reporting client reachability changes
- reporting endpoint reachability announcements and withdrawals
- preserving Deckr routing semantics across process or network boundaries

A transport component must not:

- expose a second application-facing bus API for the same lane
- duplicate local subscriber fan-out that belongs to the shared lane bus
- redefine a Deckr lane's message shape
- expose transport ids as application routing ids
- assign durable endpoint identities from connection ids, topic names, paths, or
  process ids
- make application code depend on MQTT topic, WebSocket path, or connection id
- make application code depend on NATS subjects, Dapr component names, Redis
  stream ids, broker consumer groups, or other substrate-specific locators
- synthesize domain cleanup directly when generic reachability events would do
- treat local, MQTT, and WebSocket lanes as different application protocols

## Loop Prevention

Loop prevention is a transport and routing concern, not a domain message
concern.

The architecture needs an explicit forwarding policy for messages that arrive
from one client and may be visible to another client.

At minimum, routing must define:

- origin client
- current client
- whether re-forwarding across another client is permitted
- how duplicate delivery is detected
- whether broadcast messages have a hop limit or route history
- whether message history is recorded for diagnostics
- whether bridges may forward direct messages, broadcast messages, or both

Different lanes may choose different forwarding policies, but the policy must be
explicit. It must not be an accidental difference between lane handlers.

Loop prevention must use message identity, route metadata, and forwarding policy.
It must not depend on payload-specific heuristics such as encoded device ids or
plugin command shapes.

## Local-Only Events

Some events are local runtime facts, not Deckr protocol messages.

For example, a controller may emit an internal "actions changed" event to tell
its device managers to re-evaluate bindings.

Local-only events must be explicit. They should not rely on transports ignoring
unknown Python object types.

If an event is not part of a Deckr lane protocol, it should either:

- live on a private local lane
- be represented as an internal service callback
- be marked with route metadata that prevents transport

Local-only messages must still be explicit in the lane contract. The system
should never depend on a transport ignoring an unknown Python object type as the
mechanism that keeps a local runtime fact from crossing a boundary.

## Control Plane And Data Plane

Deckr has both data-plane messages and control-plane messages.

Data-plane messages are ordinary lane messages between endpoints, such as
hardware events, hardware commands, plugin lifecycle messages, plugin commands,
and replies.

Control-plane messages describe the messaging system itself, such as:

- client connected
- client disconnected
- endpoint reachable
- endpoint unreachable
- endpoint claim rejected
- route lease renewed
- route expired
- endpoint capabilities changed
- transport binding healthy or unhealthy

Control-plane messages may use a dedicated core lane, a routing service API, or
bus metadata events. The representation is an implementation decision, but the
concept must be first-class.

Domain cleanup is triggered by control-plane route loss and then performed by
domain services. Transports report reachability; they do not decide that devices,
actions, contexts, or settings records should be deleted.

## Security And Trust

Endpoint reachability is a claim. A claim needs a trust model.

The bus architecture must eventually define:

- which endpoint families a client is allowed to claim
- whether endpoint ownership is exclusive or shared
- how conflicting claims are resolved
- whether a bridge may claim endpoints behind it
- how endpoint claims are revoked when a client disconnects or a lease expires
- which metadata is safe to trust from a remote peer
- which messages are allowed from each sender endpoint family

This does not require a heavyweight security system in the first implementation,
but the identity model must leave room for one. A remote transport peer should
not be treated as authoritative for arbitrary endpoint addresses merely because
it sent syntactically valid messages.

## Architecture Vocabulary

This document deliberately uses established messaging vocabulary where it helps:

- message bus
- messaging substrate
- transport adapter
- substrate adapter
- channel adapter
- messaging bridge
- queue group or consumer group
- command message
- event message
- request/reply
- correlation identifier
- idempotent receiver
- dead-letter channel
- durable stream
- message history
- control bus

These patterns are vocabulary and candidate substrate features, not a mandate to
buy, embed, or recreate a traditional Enterprise Service Bus.

Modern event systems also reinforce the same split Deckr needs:

- protocol-neutral message metadata is distinct from message data
- event identity is scoped by source
- subjects identify what an event is about
- transport bindings are separate from message contracts
- trace context should be propagated across process and network boundaries

Deckr may borrow these ideas without adopting any one external specification
wholesale.

## Current Implementation Gaps

The current implementation does not fully match this architecture.

Known gaps:

- plugin host messages use a protocol envelope as the in-process bus object
- transport metadata and bus metadata are both called envelopes
- hardware manager routing is encoded into device ids
- device id currently does more than one job in remote hardware scenarios
- hardware manager identity and transport identity are currently conflated in
  remote hardware transports
- plugin action addresses currently use delimiter-composed strings
- context ids currently expose routing-relevant structure to plugin messages
- broadcast targets are currently represented as pseudo-address strings such as
  `all_hosts` and `all_controllers`
- endpoint ids and runtime ids currently share normalization assumptions
- broadcast and forwarding policy is still incomplete
- local-only controller events share lanes with transported protocol messages
- route leases and bridge authority are modeled in the route table, but the
  managed lane runtime still needs to drive lease expiry in normal embedded
  runtime hosts
- capability-aware route selection is not yet modeled
- endpoint claim policy is currently lane/family/trust based; full
  authentication is not yet modeled
- there is not yet an architecture spike recording which generic messaging
  mechanics should be delegated to NATS, Dapr, or another standard substrate
  before Deckr builds them itself

These are design bugs to fix, not behavior to preserve.

## Hard Rules

- The Deckr message protocol is distinct from transport protocol.
- The Deckr message protocol is distinct from any chosen messaging substrate.
- Adapter-private protocols are outside the core bus architecture.
- Local and transported lane semantics must be identical.
- External substrates must sit behind explicit transport adapter or bridge
  boundaries.
- No external substrate may become the application-facing Deckr lane API,
  endpoint identity model, discovery model, or domain model.
- Before Deckr builds generic remote routing, bridging, durable delivery, replay,
  duplicate windows, request/reply plumbing, queue groups, TTL, or dead-letter
  behavior, it must evaluate standard substrates first.
- Endpoint identity is distinct from client/session/transport identity.
- Endpoint addresses are distinct from domain entity subjects.
- Component/runtime identity is distinct from protocol endpoint identity.
- Scoped domain ids must not be widened by delimiter-encoding other scoped ids
  into them.
- Address parsing and construction must be centralized and canonical.
- Reachability is first-class.
- Endpoint claims require an explicit ownership and trust model.
- Client disconnect means route loss, not directly domain deletion.
- Domain cleanup is a reaction to lost reachability.
- Transport-local metadata must not leak into application routing.
- Broadcast must be explicit at the Deckr routing layer.
- Loop prevention must be explicit at the Deckr routing layer.
- Delivery semantics must be explicit per lane.
- Local-only events must be explicit and must not accidentally leak onto
  transported lanes.
