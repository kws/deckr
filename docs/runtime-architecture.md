# Runtime Architecture

This document is normative architecture, not a description of whatever the code
happens to do today.

Deckr is in ALPHA. We are still deciding what the architecture is. Because of
that, backwards compatibility is not a goal. Compatibility shims, aliases,
adapter layers, dual APIs, transitional discovery paths, and legacy fallback
behavior are strictly forbidden. If something is wrong, remove it and replace it
with the right design. Do not preserve a mistaken abstraction just because some
code already exists.

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
That includes the core message specifications that move across event lanes and
across any bridges attached to those lanes.

## Architectural Model

There is exactly one runtime abstraction in Deckr: `Component`.

There is exactly one discovery mechanism in Deckr: components are discovered
uniformly through a single entry-point based mechanism.

There are not separate architectural discovery systems for:

- controllers
- drivers
- plugin hosts
- bridges
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

If Deckr needs another core lane, it must be added deliberately in `deckr`. Do
not create new core lanes ad hoc inside a controller, plugin host, driver, or
bridge package.

Third-party extension lanes are allowed, but they must use globally namespaced
identifiers owned by the extending system. Extension lanes must not squat on
short unqualified names that look like Deckr core contracts.

The recommended convention for extension lane identifiers is a dotted
owner-qualified name such as `acme.metrics.events`.

### Event Lane Bridging

Named event lanes are logical buses, not process-local implementation details.

A lane may exist:

- locally and in-memory within one process
- across process boundaries via a bridge component
- across host or network boundaries via a bridge component

Bridge transports such as WebSocket, MQTT, Redis, or future mechanisms are not
different architectural classes. They are all just transport adapters carrying
the same lane semantics.

No transport is primary or second-class in the architecture.

A bridge is also just a component.

A bridge component:

- consumes one or more named lanes
- republishes those same lane semantics across a transport boundary
- preserves lane meaning
- translates only framing, delivery, and transport concerns

A bridge must not redefine the meaning of a lane just because it is crossing a
process or network boundary.

### Wire Contract Rule

Once a lane is bridged, messages on that lane must use standardized,
wire-friendly contracts.

All core wire contracts belong in `deckr`.

These core wire contracts include:

- core lane message envelope types
- core payload types
- core event types
- core command types

Envelopes such as `HostMessage`-style protocol wrappers are part of the core
wire contract. They are not transport-specific implementation details.

Python is expected to author these core contracts using Pydantic models.

The interoperability artifact for non-Python implementations is the JSON Schema
generated from those same core contracts.

Node, Rust, and other implementations should consume the JSON Schema
descriptions, not reverse-engineer Python implementation details.

Transport adapters must not invent alternate payload shapes for core lane
traffic.

Transport adapters may add minimal transport-local framing metadata outside the
standardized Deckr message payload when required for delivery, session
management, deduplication, loop prevention, fragmentation, or similar
transport-local concerns.

That outer framing is not the lane message contract. It must never change the
meaning or shape of the standardized Deckr message carried inside it.

### Core and Extension Messages

Deckr defines a core set of wire message types that the platform expects.

Systems may extend these with additional message types, but those extensions:

- must not change the meaning of core message types
- must not change the shape of core message types
- must remain wire-safe if they cross a bridge

Extension messages are allowed. Competing definitions of core messages are not.

### Transport Boundary Rule

In-process event objects and bridged wire objects may differ internally if that
is useful for implementation.

However, the architectural contract is always the wire-safe core schema owned by
`deckr`.

No bridge may depend on:

- Python object identity
- pickle-style serialization
- transport-specific ad hoc payloads
- undocumented envelope variants

If a core message contract is wrong, replace it.

Do not preserve:

- dataclass-vs-Pydantic duality
- alias fields for compatibility
- legacy envelope forms
- alternate bridge payload shapes

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

All experience state belongs in controllers.

Devices should know nothing about plugins.

Plugins should know nothing about devices beyond what they learn through the
controller-mediated protocol.

Plugin hosts are also just components. Their job is to own plugin lifecycle and
translate between plugin-facing APIs and Deckr's message lanes.

Drivers are also just components. Their job is to translate between concrete
hardware and Deckr's message lanes.

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
  - the stable runtime identity of the component type
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
locally, or they may be bridged across transport boundaries by other
components.

For most component types, `consumes` and `publishes` are fixed lists declared by
the component type itself.

For configurable adapter components such as bridges, the manifest may instead
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
- `deckr.bridges.mqtt`
- `deckr.bridges.websocket`
- `deckr.drivers.mqtt`

The launcher must use the manifest's declared `config_prefix`. It must not try
to infer meaning from path segments such as `plugin_hosts`, `drivers`, or
`controller`.

`deckr.plugin_hosts.python` and `deckr.bridges.mqtt` are therefore two separate
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
- `deckr.bridges.mqtt` does not automatically receive configuration from
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

`deckr.bridges.mqtt` is not an instance of `deckr.plugin_hosts.python`; it is a
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

Protocol addresses such as `controller:<controller_id>` and `host:<host_id>`
are lane-level messaging identities. They are not the generic lifecycle identity
of a component instance.

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

### Bridge Components

Bus bridges are not declared on lanes. Bus bridges are declared as normal
components.

There is no separate bridge registry, no bridge-only discovery mechanism, and
no launcher rule that says a transport automatically belongs to one semantic
role.

A bridge is modeled in three layers:

- bridge component type
- bridge component instance
- per-lane binding

Those layers must not be collapsed together.

The meanings are:

- bridge component type
  - the reusable implementation such as `deckr.bridges.mqtt` or
    `deckr.bridges.websocket`
  - discovered through the normal component mechanism
  - declares its manifest, transport family, and whether its bindings are fixed
    or configurable per instance
- bridge component instance
  - one configured deployment of that bridge type
  - owns transport/session configuration such as hostname, port, credentials,
    server/client mode, reconnect policy, and similar concerns
- per-lane binding
  - one explicit mapping between a logical Deckr lane and a concrete remote
    transport address
  - owns direction and remote addressing

The recommended naming convention for reusable bridge component types is:

- `deckr.bridges.websocket`
- `deckr.bridges.mqtt`
- `deckr.bridges.redis`

This is only a naming convention for normal components. It is not a second
architectural discovery model.

Bridge configuration has two parts:

- transport/session configuration
- explicit `bindings`

Each binding must declare:

- `lane`
  - the exact logical Deckr lane being bridged
- `direction`
  - one of `ingress`, `egress`, or `bidirectional`
- one transport-specific remote address
  - for example `topic`, `path`, `channel`, `stream`, or similar
- `schema_id` for extension lanes when the lane is not defined as a core Deckr
  lane

For core Deckr lanes, the lane name implies the core wire contract from
`deckr`. That contract must not be redefined in bridge-local configuration.

For extension lanes, the binding must identify the extension wire contract
explicitly. Do not rely on convention or out-of-band knowledge.

Bridge bindings are explicit because hidden transport-to-lane assumptions are a
major source of architectural drift.

The launcher must never infer:

- that MQTT implies `plugin_messages`
- that WebSocket implies `hardware_events`
- that a component under `plugin_hosts` must bridge plugin traffic
- that a component under `drivers` must bridge hardware traffic

All such mappings must be explicit in the bridge instance configuration.

A recommended configuration shape for multi-instance bridges is:

- `[deckr.bridges.<transport>.instances.<instance_id>]`
  - transport/session configuration
- `[deckr.bridges.<transport>.instances.<instance_id>.bindings.<binding_id>]`
  - one explicit lane binding

For example:

- `[deckr.bridges.mqtt.instances.main]`
- `[deckr.bridges.mqtt.instances.main.bindings.plugin_messages]`
- `[deckr.bridges.mqtt.instances.main.bindings.hardware_events]`

In this model:

- `instance_id` identifies the bridge runtime instance
- `binding_id` identifies one binding within that instance
- `lane` identifies the Deckr logical lane contract

The binding identifier is local to the bridge instance. It is not the lane
name, not the component name, and not a discovery key.

This separation matters:

- the same bridge instance may carry multiple lanes
- multiple bindings may use the same transport session
- multiple bridge instances may carry the same lane to different remote peers
- transport topology must not leak back into component discovery

The component itself remains responsible for parsing its own bridge config,
validating transport options, and deciding whether it should currently activate
each configured binding.

If a bridge cannot establish its transport session, it may remain temporarily
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
- Lanes are logical runtime contracts and may be bridged across transport
  boundaries.
- Bridge transports are peers architecturally; none are special-cased.
- Core bridged message envelopes and payloads live in `deckr`.
- Pydantic models in `deckr` are the canonical Python authoring format for core
  wire contracts.
- JSON Schema generated from those contracts is the interoperability artifact
  for non-Python implementations.
- Transport-local framing may exist, but it must not redefine the carried Deckr
  message contract.
- Configuration is bound by exact manifest-declared prefix.
- Components parse only their own resolved configuration mapping.
- The launcher does not interpret "enabled" or "disabled" for components.
- Runtime context may carry only generic launcher metadata and lane handles as a
  generic primitive.
- Bridges are declared as normal components, not as lane-local special cases.
- Bridge instances own explicit per-lane bindings.
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
