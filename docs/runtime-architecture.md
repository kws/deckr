# Runtime Architecture

This document is normative architecture, not a description of whatever the code
happens to do today.

Deckr is in ALPHA. We are still deciding what the architecture is. Because of
that, backwards compatibility is not a goal. Compatibility shims, aliases,
compatibility adapter layers, dual APIs, transitional discovery paths, and legacy
fallback behavior are strictly forbidden. If something is wrong, remove it and
replace it with the right design. Do not preserve a mistaken abstraction just
because some code already exists.

## Core Goal

Deckr aims to be a service-driven, distributed architecture for connecting:

- hardware devices
- plugin hosts
- controllers
- any future runtime participant that fits the same model

without rewriting the core libraries for each new device class, host type, or
deployment shape.

The two external realities shaping this architecture are:

- Hardware devices with addressable controls, displays, and input gestures.
- Plugin runtimes that bind actions to those controls and react to lifecycle
  and input events.

The shared APIs and runtime primitives for this architecture belong in `deckr`.
That includes the core message specifications, routing metadata, and wire-safe
contracts that move across event lanes and across any transports attached to
those lanes.

## Architectural Model

There is exactly one runtime abstraction in Deckr: `Component`.

There is exactly one discovery mechanism in Deckr: components are discovered
uniformly through a single entry-point based mechanism.

There are not separate architectural discovery systems for:

- controllers
- drivers
- plugin hosts
- transports
- "special" runtime services

Those are semantic roles, not different runtime kinds.

A controller is a component. A driver is a component. A plugin host is a
component. Any future third-party participant is also just a component.

If a design introduces a second generic discovery abstraction because one role
"feels special", that design is wrong.

## Event Lanes

Components are wired together through named event bus lanes.

A component declares which lanes it consumes and which lanes it publishes to.
That declaration is part of the component's manifest or metadata contract.

The launcher is responsible for:

- discovering components
- constructing the available named lanes
- instantiating components
- wiring components against one shared runtime context

The launcher must resolve the full set of required lanes and construct their
handles before starting any component. No component may rely on another
component having already started in order for a core lane to exist.

The launcher does not care whether a component is "really" a controller, driver,
or plugin host. The only generic concern is the lane contract it declares.

This means the architecture is defined by lane interaction, not by role-specific
loader types.

### Core Lane Registry

Core lane names are part of the architecture and belong in `deckr`, just like
core message contracts.

The launcher must treat lane names as stable logical contract identifiers, not
as incidental local variable names.

The current core lane set includes:

- `plugin_messages`
- `hardware_events`

The bus and routing requirements for these lanes are defined in
[bus-architecture.md](bus-architecture.md). This document owns the generic
component and lane model; the bus architecture document owns endpoint identity,
entity subjects, client reachability, route claims, logical Deckr envelopes,
broadcast semantics, delivery semantics, control-plane messages, and disconnect
cleanup.

If Deckr needs another core lane, it must be added deliberately in `deckr`. Do
not create new core lanes ad hoc inside a controller, plugin host, driver, or
transport package.

Third-party extension lanes are allowed, but they must use globally namespaced
identifiers owned by the extending system. Extension lanes must not squat on
short unqualified names that look like Deckr core contracts.

The recommended convention for extension lane identifiers is a dotted
owner-qualified name such as `acme.metrics.events`.

### Event Lane Transport

Named event lanes are logical buses, not process-local implementation details.

A lane may exist:

- locally and in-memory within one process
- across process boundaries via a transport component
- across host or network boundaries via a transport component

Transports and messaging substrates such as WebSocket, MQTT, NATS, Dapr-backed
pub/sub, Redis, or future mechanisms are not different architectural classes.
They are all just transport adapters or substrate integrations carrying the same
lane semantics.

No transport is primary or second-class in the architecture.

A transport is also just a component.

The shared lane implementation is the application-facing bus for one Deckr
runtime. It owns the common `send`/`subscribe` surface, in-process fan-out,
subscriber lifecycle, and local backpressure policy. Remote transports attach to
that shared lane implementation; they do not each create a separate
application-facing bus with its own copy of those mechanics.

A transport component:

- consumes one or more named lanes
- republishes those same lane semantics across a transport boundary
- preserves lane meaning
- translates only framing, delivery, and transport concerns

A transport must not redefine the meaning of a lane just because it is crossing a
process or network boundary.

Transport-local publish/subscribe mechanics are allowed when required by the
transport substrate. For example, MQTT has topics and WebSocket servers have
connected sockets. Those mechanics are delivery details below the Deckr lane
API, not a second lane API for application components.

Using a richer substrate such as NATS or Dapr does not change this rule. Those
systems may provide broker fan-out, request/reply inboxes, consumer or queue
groups, durable streams, replay, redelivery, TTL, dead-letter handling, or
edge/leaf topology below Deckr. Application components still use Deckr lane
handles, Deckr envelopes, Deckr endpoint addresses, and Deckr subjects.

### Message Contract Rule

Messages on a core lane must use standardized, wire-safe Deckr contracts whether
they are delivered locally or across a transport boundary.

All core message contracts belong in `deckr`.

These core contracts include:

- the Deckr logical message envelope
- endpoint and route metadata
- endpoint addresses and entity subjects
- core lane message body types
- core event types
- core command types

The Deckr logical message envelope is part of the Deckr message protocol. It is
not an MQTT envelope, a WebSocket envelope, a local Python object wrapper, or any
other transport-local frame.

Transport framing may exist outside the Deckr message envelope, but it is not
the lane message contract.

Python is expected to author these core contracts using Pydantic models.

The interoperability artifact for non-Python implementations is the JSON Schema
generated from those same core contracts.

Node, Rust, and other implementations should consume the JSON Schema
descriptions, not reverse-engineer Python implementation details.

Transport adapters must not invent alternate payload shapes for core lane
traffic.

Transport adapters may add minimal transport-local framing metadata outside the
standardized Deckr message envelope when required for delivery, session
management, deduplication, loop prevention, fragmentation, client reachability,
or similar transport-local concerns.

That outer framing is not the lane message contract. It must never change the
meaning or shape of the standardized Deckr message carried inside it, and it
must not leak into application routing.

### Core and Extension Messages

Deckr defines a core set of message types that the platform expects.

Systems may extend these with additional message types, but those extensions:

- must not change the meaning of core message types
- must not change the shape of core message types
- must remain wire-safe if they cross a transport

Extension messages are allowed. Competing definitions of core messages are not.

### Transport Boundary Rule

In-process event objects and serialized wire objects may differ internally if
that is useful for implementation.

However, the architectural contract is always the wire-safe Deckr message schema
owned by `deckr`.

Local and transported lane semantics must be the same. Local delivery must not
depend on a different protocol model just because no network transport is
involved.

No transport may depend on:

- Python object identity
- pickle-style serialization
- transport-specific ad hoc payloads
- undocumented envelope variants
- application-visible transport identity

If a core message contract is wrong, replace it.

Do not preserve:

- dataclass-vs-Pydantic duality
- alias fields for compatibility
- legacy envelope forms
- alternate transport payload shapes

for backwards compatibility. This project is in ALPHA. The correct protocol is
more important than preserving mistaken wire forms.

## Startup and Failure Model

The architecture must not rely on startup ordering.

Components may appear, disappear, crash, restart, or be absent entirely. The
message bus model exists specifically so that components can come and go without
requiring deep integration or fixed bootstrap sequencing.

If a component needs another component's traffic, it should express that through
lane usage and runtime behavior, not by relying on bespoke startup choreography.

The shared runtime context may provide generic launcher metadata such as:

- `component_id`
- `instance_id`
- resolved configuration
- `base_dir`
- named lane handles

That runtime context must not become a second general-purpose dependency
injection system.

In particular, the runtime context must not expose the full configuration
document to components. Components receive only their own resolved exact-prefix
mapping.

If a cross-component interaction is dynamic, it belongs on a lane.

If a shared runtime artifact is truly generic and static, it must be defined
explicitly in `deckr` as part of the runtime contract. It must not appear as an
ad hoc role-specific context key invented by one implementation.

## Reference Roles

The reference architecture includes a controller component that brokers commands
from plugin-facing lanes to device-facing lanes.

The controller's job is to:

- connect actions to actual device controls
- manage settings and state around that process
- own user experience state
- mediate between plugins and devices
- own context ids and the mapping from contexts to devices, controls, profiles,
  and pages

All experience state belongs in controllers.

Devices should know nothing about plugins.

Plugins should know nothing about devices beyond what they learn through the
controller-mediated protocol.

Plugin hosts are also just components. Their job is to own plugin lifecycle and
translate between plugin-facing APIs and Deckr's message lanes.

Plugin runtime process identities, session tokens, claim URLs, and runtime
WebSocket connections are plugin-host-private control-plane details. They must
not become Deckr endpoint addresses.

Drivers are also just components. Their job is to translate between concrete
hardware and Deckr's message lanes.

Hardware managers own manager-local device identity and the mapping from concrete
hardware discovery facts to Deckr hardware subjects. Concrete hardware paths,
HID paths, process ids, WebSocket sessions, MQTT topics, and transport ids must
not become durable device identity.

## Configuration

Deckr should support configuration documents such as TOML, but the architecture
does not require one concrete file format.

What matters is that configuration is bound by component manifest, not by
semantic role and not by one global document schema understood by the launcher.

### Component Manifest Contract

Every discoverable component must declare, in its manifest or equivalent
metadata:

- `component_id`
- `config_prefix`
- `consumes`
- `publishes`
- `cardinality`

The intended meanings are:

- `component_id`
  - the stable identity of the component type
  - also the discovery key
- `config_prefix`
  - the exact configuration namespace bound to this component type
- `consumes`
  - the named event bus lanes this component may read from
- `publishes`
  - the named event bus lanes this component may write to
- `cardinality`
  - whether the component type is `singleton` or `multi_instance`

Lane declarations describe logical lane contracts. Those contracts may be hosted
locally, or they may be transported across transport boundaries by other
components.

For most component types, `consumes` and `publishes` are fixed lists declared by
the component type itself.

For configurable adapter components such as transports, the manifest may instead
declare the component's lane binding capability, and instance configuration may
then provide the exact lane bindings for that specific instance. In that case
the launcher must resolve the instance's actual `consumes` and `publishes` from
those explicit bindings, not infer them from semantic role, transport type, or
path naming.

As a default convention, `config_prefix` should be the canonical Python import
path of the component.

Examples:

- `deckr.controller`
- `deckr.plugin_hosts.python`
- `deckr.transports.mqtt`
- `deckr.transports.websocket`
- `deckr.drivers.mqtt`

The launcher must use the manifest's declared `config_prefix`. It must not try
to infer meaning from path segments such as `plugin_hosts`, `drivers`, or
`controller`.

`deckr.plugin_hosts.python` and `deckr.transports.mqtt` are therefore two separate
component types, not a parent component and a child component.

### Exact Prefix Binding

Each component is only concerned with its own exact configuration prefix.

The launcher must:

- discover the component
- read its manifest
- resolve the exact configuration namespace for that component
- pass only that resolved configuration mapping to the component

The launcher must not:

- inspect sibling component sections on behalf of the component
- merge parent namespaces implicitly
- overlay role-based configuration tables
- interpret the overall document shape beyond generic prefix resolution

This means:

- `deckr.plugin_hosts.python` does not automatically receive configuration from
  `deckr.plugin_hosts`
- `deckr.transports.mqtt` does not automatically receive configuration from
  `deckr.plugin_hosts.python`
- dotted names are exact binding prefixes, not inheritance paths

Implicit parent-scope inheritance is forbidden.

### Component-Owned Parsing and Enablement

Each component:

- receives only its own resolved configuration mapping
- parses that mapping itself
- validates that mapping itself
- decides for itself what defaults apply
- decides for itself what "enabled" or "disabled" means

The launcher does not own component enablement policy.

A component being "disabled" is not a launcher-level state. In an event-bus
architecture it simply means the component currently chooses not to consume from
or publish to its declared lanes. That decision belongs entirely to the
component, and it may change at runtime if external circumstances change.

For example, a component may initially decide not to participate because another
service is absent, and later begin consuming or publishing when that dependency
appears.

The launcher must never short-circuit that behavior by imposing a generic
enabled/disabled convention.

### Singleton and Multi-Instance Components

Component type identity and component instance identity are different concepts.

`deckr.transports.mqtt` is not an instance of `deckr.plugin_hosts.python`; it is a
distinct component type with its own manifest and its own exact
`config_prefix`.

If a component type is `singleton`, there is at most one configured instance of
that component type, and its configuration lives exactly at its declared
`config_prefix`.

If a component type is `multi_instance`, that must be declared explicitly in the
manifest. In that case the launcher may create multiple component instances of
the same type, each with a separate `instance_id`.

Every instantiated component also has a launcher-scoped runtime identity used
for lifecycle management.

That runtime identity must be:

- unique within one launcher runtime
- derived deterministically from `component_id` and `instance_id`
- separate from protocol-level addresses carried on event lanes

Protocol addresses such as `controller:<controller_id>`, `host:<host_id>`, and
`hardware_manager:<manager_id>` are lane-level messaging identities. They are
not the generic lifecycle identity of a component instance.

Deckr protocol endpoint addresses, entity subjects, client/session ids,
transport addresses, component type ids, and launcher runtime identities are all
separate concepts. A component may currently derive one configured value from
another as a convenience, but the architecture must not depend on that
derivation.

The configuration model for multi-instance components is:

- the component type still has one exact `config_prefix`
- instances live under that prefix's explicit `instances` namespace
- each instance is addressed by `instance_id`
- each instantiated component receives only its own instance mapping, not the
  whole component family subtree

For example, a multi-instance component with `config_prefix =
deckr.plugin_hosts.python` would use:

- `[deckr.plugin_hosts.python.instances.main]`
- `[deckr.plugin_hosts.python.instances.remote]`

and the launcher would instantiate two instances with `instance_id = "main"` and
`instance_id = "remote"`.

This is the only permitted way to express multiplicity within the generic
component model.

### Transport Components

Bus transports are not declared on lanes. Bus transports are declared as normal
components.

There is no separate transport registry, no transport-only discovery mechanism, and
no launcher rule that says a transport automatically belongs to one semantic
role.

A transport is modeled in three layers:

- transport component type
- transport component instance
- per-lane binding

Those layers must not be collapsed together.

The meanings are:

- transport component type
  - the reusable implementation such as `deckr.transports.mqtt` or
    `deckr.transports.websocket`
  - discovered through the normal component mechanism
  - declares its manifest, transport family, and whether its bindings are fixed
    or configurable per instance
- transport component instance
  - one configured deployment of that transport type
  - owns transport/session configuration such as hostname, port, credentials,
    server/client mode, reconnect policy, and similar concerns
- per-lane binding
  - one explicit mapping between a logical Deckr lane and a concrete remote
    transport address
  - owns direction and remote addressing

The shared lane bus and routing layer own app-facing fan-out, local
subscription, endpoint reachability, route selection, and broadcast expansion.
Transport instances attach to those shared services instead of reimplementing
them per transport.

For generic broker features beyond the local runtime, the transport instance may
delegate mechanics to an external substrate, but only through an explicit
component and per-lane binding. Delegation must not introduce a second component
model, discovery model, endpoint identity model, or application-facing bus API.

The recommended naming convention for reusable transport component types is:

- `deckr.transports.websocket`
- `deckr.transports.mqtt`
- `deckr.transports.redis`

This is only a naming convention for normal components. It is not a second
architectural discovery model.

A transport instance id is not a Deckr endpoint address. It identifies a
transport runtime instance or session for diagnostics, loop prevention, and route
bookkeeping. Application routing must use Deckr endpoint addresses and explicit
subjects, not MQTT topics, WebSocket paths, connection ids, or transport ids.

Transport configuration has two parts:

- transport/session configuration
- explicit `bindings`

Each binding must declare:

- `lane`
  - the exact logical Deckr lane being transported
- `direction`
  - one of `ingress`, `egress`, or `bidirectional`
- one transport-specific remote address
  - for example `topic`, `path`, `channel`, `stream`, or similar
- `schema_id` for extension lanes when the lane is not defined as a core Deckr
  lane

For core Deckr lanes, the lane name implies the core Deckr message contract from
`deckr`. That contract must not be redefined in transport-local configuration.

For extension lanes, the binding must identify the extension message contract
explicitly. During normal runtime activation, that schema id must match the
resolved lane contract from the lane contract registry. Do not rely on
convention or out-of-band knowledge, and do not treat a transport binding's
schema id as the whole route policy for the lane.

Transport bindings are explicit because hidden transport-to-lane assumptions are a
major source of architectural drift.

The launcher must never infer:

- that MQTT implies `plugin_messages`
- that WebSocket implies `hardware_events`
- that a component under `plugin_hosts` must transport plugin traffic
- that a component under `drivers` must transport hardware traffic

All such mappings must be explicit in the transport instance configuration.

A recommended configuration shape for multi-instance transports is:

- `[deckr.transports.<transport>.instances.<instance_id>]`
  - transport/session configuration
- `[deckr.transports.<transport>.instances.<instance_id>.bindings.<binding_id>]`
  - one explicit lane binding

For example:

- `[deckr.transports.mqtt.instances.main]`
- `[deckr.transports.mqtt.instances.main.bindings.plugin_messages]`
- `[deckr.transports.mqtt.instances.main.bindings.hardware_events]`

In this model:

- `instance_id` identifies the transport runtime instance
- `binding_id` identifies one binding within that instance
- `lane` identifies the Deckr logical lane contract

The binding identifier is local to the transport instance. It is not the lane
name, not the component name, and not a discovery key.

This separation matters:

- the same transport instance may carry multiple lanes
- multiple bindings may use the same transport session
- multiple transport instances may carry the same lane to different remote peers
- transport topology must not leak back into component discovery

The component itself remains responsible for parsing its own transport config,
validating transport options, and deciding whether it should currently activate
each configured binding.

If a transport cannot establish its transport session, it may remain temporarily
inactive and later begin participating when the remote endpoint becomes
available. That is still component behavior, not launcher policy.

### Shared Defaults

Family-wide defaults such as "all plugin hosts inherit from
`deckr.plugin_hosts`" are not part of the architecture.

That kind of implicit parent lookup is an unnecessary complication and is
forbidden.

If shared defaults are ever needed, they must be introduced as an explicit,
separate configuration mechanism and resolved by the launcher before the
component is instantiated.

The component must still receive one final resolved mapping for itself.

Even if such a defaults system is added later, it must obey these rules:

- defaults are explicit, never inferred from parent prefixes
- defaults resolution happens before handoff to the component
- the component still parses only one resolved mapping
- defaults do not create a second discovery or role-specific configuration
  mechanism

Custom launchers may choose their own configuration objects, but they must still
honor the same manifest, prefix, and lane model.

## Hard Rules

- There is one component model.
- There is one discovery model.
- Lane contracts are the only generic wiring primitive.
- The launcher creates the full core lane set before component startup.
- Core lane names belong in `deckr`.
- Lanes are logical runtime contracts and may be transported across transport
  boundaries.
- Transports are peers architecturally; none are special-cased.
- Shared lane infrastructure owns the application-facing send/subscribe/fan-out
  API.
- Core Deckr message envelopes, route metadata, and payloads live in `deckr`.
- Pydantic models in `deckr` are the canonical Python authoring format for core
  message contracts.
- JSON Schema generated from those contracts is the interoperability artifact
  for non-Python implementations.
- Transport-local framing may exist, but it must not redefine the carried Deckr
  message contract.
- External messaging substrates must sit behind explicit transport adapter or
  bridge boundaries.
- Adopting NATS, Dapr, Redis, MQTT, WebSocket, or another substrate must not
  replace Deckr lanes, envelopes, endpoint addresses, subjects, discovery, or
  component lifecycle semantics.
- Transport-local identity must not leak into application-level routing.
- Endpoint identity and route reachability are distinct from component lifecycle
  identity.
- Endpoint addresses are distinct from the domain entity subjects carried by
  lane messages.
- Client/session identity, transport addresses, component runtime identity, and
  protocol endpoint identity are separate.
- Configuration is bound by exact manifest-declared prefix.
- Components parse only their own resolved configuration mapping.
- The launcher does not interpret "enabled" or "disabled" for components.
- Runtime context may carry only generic launcher metadata and lane handles as a
  generic primitive.
- Transports are declared as normal components, not as lane-local special cases.
- Transport instances own explicit per-lane bindings.
- The launcher must not infer lane bindings from transport kind, role name, or
  config path.
- Implicit parent-prefix inheritance is forbidden.
- Type identity and instance identity are separate.
- Lifecycle identity and protocol address identity are separate.
- Role names such as controller, driver, and plugin host are descriptive only.
- Replaceability alone is not a reason to introduce a new generic runtime layer.
- Do not add shims, aliases, compatibility wrappers, or dual abstractions.
- Do not preserve broken abstractions for migration purposes.

If the implementation drifts from this model, fix the implementation. Do not
soften the architecture to accommodate accidental complexity.
