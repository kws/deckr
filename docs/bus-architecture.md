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

The common issue is reachability. Deckr needs to know which logical endpoints
are reachable through which client, connection, process, or bridge. When that
client disappears, Deckr should model route loss once and let domain services
react to it.

This is not a payload-shape cleanup. Payload models should be revised after the
bus routing model is clear.

## Scope

Deckr has three separate concerns that must not be collapsed together:

- Deckr message protocol
  - logical messages between Deckr architectural endpoints
  - examples: controller to plugin host, plugin host to controller, hardware
    manager to controller, controller to hardware manager
- Transport protocol
  - how Deckr messages cross a process, host, or network boundary
  - examples: local in-memory bus, MQTT, WebSocket, future Redis
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

This route state is not the same as a message payload.

Route state may be represented by bus metadata, by explicit route events, or by a
dedicated routing service, but it must be part of the Deckr architecture rather
than a one-off behavior in each transport or lane handler.

## Deckr Message Envelope

A Deckr message needs transport-neutral routing metadata.

The logical envelope should include:

- message id
  - unique identity for tracing, dedupe, and request/reply correlation
- optional in-reply-to id
  - correlation to an earlier message
- lane or protocol family
  - the logical Deckr lane contract being carried
- sender endpoint
  - a Deckr endpoint address
- recipient endpoint or broadcast scope
  - a Deckr endpoint address or a well-known broadcast target
- message body
  - the typed Deckr message for that lane

Transport-local fields such as MQTT topic, WebSocket path, socket id,
connection id, transport id, retry counters, and loop-prevention hop metadata do
not belong in the Deckr message body.

## Broadcast

Broadcast is a Deckr routing concept, not a transport feature.

Known broadcast scopes include:

- all plugin hosts in a controller domain
- all controllers in a deployment domain
- potentially all hardware managers in a controller domain

Broadcast scope must be explicit. Do not rely on MQTT topic fan-out, WebSocket
server fan-out, or local subscriber fan-out as the application-level meaning of
broadcast.

## Lanes

A lane is a logical Deckr contract.

Core lanes currently include:

- `hardware_events`
- `plugin_messages`

Lane meaning must be identical whether the lane is local or transported.

A transport may carry multiple lanes. A lane may be carried by multiple
transports. Neither fact may change the shape or routing semantics of messages
on that lane.

## Hardware Routing

Hardware routing has three identities that should remain distinct:

- hardware manager endpoint
  - the logical owner/router for one or more devices
- device id
  - the controller-visible identity of a device
- slot/control id
  - the identity of a control within a device

The current encoded remote device id shape, such as
`manager=<manager_id>|device=<device_id>`, is a symptom of missing first-class
hardware manager routing. It works as an implementation technique, but it should
not be the long-term architecture boundary.

Controller-to-hardware commands should route to the hardware manager responsible
for a device without leaking transport identity into the application layer.

Hardware-manager disconnect should be modeled as loss of reachability to that
manager and to devices only reachable through that manager.

## Plugin Host Routing

Plugin host routing has these identities:

- controller endpoint
- plugin host endpoint
- action id
- resolved action address
- context id

The controller owns contexts. A context id must identify the controller domain,
device, and slot/control well enough that plugin commands can be routed without
guessing.

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
- timeout or missing-reply behavior owned by the requester

Settings requests are the current core example, but the mechanism is generic.

## Transport Responsibilities

A transport component is responsible for:

- connecting to its transport substrate
- mapping configured transport bindings to Deckr lanes
- carrying Deckr message envelopes unchanged across the boundary
- maintaining client/session identity for route bookkeeping
- preventing transport-level echo loops
- reporting client reachability changes
- preserving Deckr routing semantics across process or network boundaries

A transport component must not:

- redefine a Deckr lane's message shape
- expose transport ids as application routing ids
- make application code depend on MQTT topic, WebSocket path, or connection id
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

Different lanes may choose different forwarding policies, but the policy must be
explicit. It must not be an accidental difference between lane handlers.

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

## Current Implementation Gaps

The current implementation does not fully match this architecture.

Known gaps:

- plugin host messages use a protocol envelope as the in-process bus object
- transport metadata and bus metadata are both called envelopes
- hardware manager routing is encoded into device ids
- plugin host disconnect cleanup is not modeled through generic reachability
- hardware and plugin lanes have different implicit cross-transport forwarding
  policies
- local-only controller events share lanes with transported protocol messages
- transports do not yet expose one common route/reachability model

These are design bugs to fix, not behavior to preserve.

## Hard Rules

- The Deckr message protocol is distinct from transport protocol.
- Adapter-private protocols are outside the core bus architecture.
- Local and transported lane semantics must be identical.
- Endpoint identity is distinct from client/session/transport identity.
- Reachability is first-class.
- Client disconnect means route loss, not directly domain deletion.
- Domain cleanup is a reaction to lost reachability.
- Transport-local metadata must not leak into application routing.
- Broadcast must be explicit at the Deckr routing layer.
- Loop prevention must be explicit at the Deckr routing layer.
- Local-only events must be explicit and must not accidentally leak onto
  transported lanes.
