# Runtime Architecture

> Live implementation reference: this document describes behavior currently
> implemented in `deckr`. It should stay in sync with code, tests, and generated
> schemas. If it differs from the implementation, treat that as a bug: either
> update the document to match current behavior or make an intentional
> code/schema/test change to match the intended v1 contract.

Deckr is in ALPHA. We are still deciding what the architecture is. Because of
that, backwards compatibility is not a goal. Compatibility shims, aliases,
compatibility adapter layers, dual APIs, transitional discovery paths, and legacy
fallback behavior are strictly forbidden. If something is wrong, remove it and
replace it with the right design. Do not preserve a mistaken abstraction just
because some code already exists.

## Core Goal

Deckr aims to be a service-driven, distributed architecture for connecting:

- hardware devices
- action provider runtimes
- controllers
- any future runtime participant that fits the same model

without rewriting the core libraries for each new device class, host type, or
deployment shape.

The two external realities shaping this architecture are:

- Hardware devices with addressable controls, displays, and input gestures.
- Action provider runtimes that bind actions to those controls and react to
  lifecycle and input events.

The shared APIs and runtime primitives for this architecture belong in `deckr`.
That includes the core message specifications, endpoint identity rules,
lane-level delivery metadata, and wire-safe contracts that move across event
lanes and across the distributed lane substrate.

## Architectural Model

There is exactly one runtime participant abstraction in Deckr: `Component`.

There is exactly one discovery mechanism in Deckr: components are discovered
uniformly through a single entry-point based mechanism.

There are not separate architectural discovery systems for:

- controllers
- hardware managers
- action provider runtimes
- transports
- "special" runtime services

Those are semantic roles, not different runtime kinds.

A controller is a component. A hardware manager is a component. An action
provider runtime is a component. Any future third-party participant is also just
a component.

If a design introduces a second generic discovery abstraction because one role
"feels special", that design is wrong.

There is also one runtime host contract.

A runtime host is the process or application that embeds Deckr's runtime
infrastructure. The bundled Deckr launcher is the reference runtime host, but it
is not the only valid host. A web application, service framework, test harness, or
larger product may embed Deckr directly.

The runtime host owns:

- constructing the managed lane runtime
- starting and stopping core bus infrastructure
- optionally discovering and instantiating components
- wiring components against one shared runtime context

Core bus infrastructure is not an auto-discovered component. It must not be made
optional by component discovery, duplicated by multiple discovered services, or
hidden inside one transport. It is part of the Deckr runtime contract that every
host must satisfy.

## Event Lanes

Components are wired together through named event bus lanes.

A component declares which lanes it consumes and which lanes it publishes to.
That declaration is part of the component's manifest or metadata contract.

A runtime host that uses Deckr components is responsible for:

- discovering components
- constructing the available named lanes
- instantiating components
- wiring components against one shared runtime context

A runtime host must resolve the full set of required lanes and construct their
handles before starting any component. No component may rely on another
component having already started in order for a core lane to exist.

The runtime host does not care whether a component is "really" a controller,
hardware manager, or action provider runtime. The only generic concern is the
lane contract it declares.

This means the architecture is defined by lane interaction, not by role-specific
loader types.

### Core Lane Registry

Core lane names are part of the architecture and belong in `deckr`, just like
core message contracts.

A runtime host must treat lane names as stable logical contract identifiers, not
as incidental local variable names.

The current core lane set includes:

- `actions`
- `hardware_messages`
- `services`

The distributed lane substrate is NATS. This document owns the generic component
and lane model. Beacon/Concord protocol semantics are specified in
[`beacon-concord.md`](beacon-concord.md). The NATS bus specification owns
endpoint-bound lane handles, recipient filtering, Beacon/Concord KV stores,
broker-resource ownership, and broker diagnostics in [`nats-bus.md`](nats-bus.md).
The v1 device, control, and capability descriptor contracts are implemented in
`deckr.hardware.descriptors`.
Canonical core capability value contracts and helpers live in
`deckr.hardware.capabilities`; the BAU contract is documented in
[`capabilities.md`](capabilities.md), with the architecture background in
[`../../notes/device-capability-model.md`](../../notes/device-capability-model.md).
Hardware discovery, input, output, device-level commands, state, and command
reply placeholders use capability-targeted contracts in
`deckr.hardware.messages`.

If Deckr needs another core lane, it must be added deliberately in `deckr`. Do
not create new core lanes ad hoc inside a controller, action provider runtime, hardware
manager, or transport package.

Third-party extension lanes are allowed, but they must use globally namespaced
identifiers owned by the extending system. Extension lanes must not squat on
short unqualified names that look like Deckr core contracts. The full namespace
rule is in [`namespaces.md`](namespaces.md).

### Event Lane Transport

Named event lanes are logical buses, not process-local implementation details.

A lane may exist:

- locally and in-memory within one process
- across process boundaries via the distributed lane substrate
- across host or network boundaries via the distributed lane substrate

The v1 distributed lane substrate is NATS. The old home-grown WebSocket and
MQTT lane transports are removed architecture, not parallel runtime paths.

The shared lane implementation is the application-facing bus for one Deckr
runtime. Components use `register_endpoint(...)` to acquire endpoint-session lane
handles so the bus layer can stamp envelope senders, fence sessions, and filter
recipients centrally.

NATS may provide broker fan-out, request/reply inboxes, queue groups, ops-only
Services, JetStream, KV watches, TTL, duplicate windows, WebSocket/MQTT-facing
network edges, and leaf topology below Deckr. Application components still use
Deckr lane handles, Deckr envelopes, Deckr endpoint addresses, and Deckr
subjects.

Adapter-private WebSocket or MQTT protocols may still exist behind action
provider, hardware, or third-party protocol boundaries. They are not Deckr lane
transports.

### Managed Lane Runtime

The managed lane runtime is the host-facing Deckr primitive that makes named
lanes usable.

It includes:

- the lane registry
- one application-facing event bus per lane
- endpoint-bound lane handles
- recipient filtering for local endpoints
- explicit StateStore access for protocols such as Beacon and Concord

The managed lane runtime belongs in `deckr`. It is not a transport, controller,
hardware manager, action provider runtime, or special discovered component.

Every runtime host must either use the Deckr-provided managed lane runtime or
explicitly provide equivalent behavior. Discovery and live agreement authority
belong to Beacon and Concord, not to home-grown WebSocket/MQTT route metadata.

The Deckr launcher should use the same managed lane runtime API that embedding
hosts use. It may add configuration loading, component discovery, signal
handling, and process lifecycle conveniences, but it must not be the only place
where required lane infrastructure exists.

### Deckr Instance API

The simplest supported embedding shape should be one managed Deckr instance.

The public Python API should make this the normal path:

```python
async with Deckr() as deckr:
    actions = deckr.lane("actions")
    hardware_messages = deckr.lane("hardware_messages")
    services = deckr.lane("services")
```

That object is a runtime host helper around the managed lane runtime. It should:

- create the core lane set by default
- accept extension lane contracts explicitly
- expose lane handles through one obvious API such as `lane(name)` or
  `lanes.require(name)`
- expose endpoint-bound lane handles, recipient filtering diagnostics, and
  explicit protocol stores
- start required generic bus infrastructure exactly once
- stop that infrastructure through normal async context-manager cancellation

Component discovery and component lifecycle should sit on top of that instance,
not inside the lane bus itself. A host that wants Deckr components should be able
to call a component-host API such as `start_components(...)` against an existing
Deckr instance. A host that only wants lane messaging should not need component
discovery at all.

The bundled launcher should be a thin convenience wrapper around this public
runtime API. It may load configuration, install signal handlers, and start
configured components, but it should not assemble required Deckr bus
infrastructure through a private path unavailable to embedded hosts.

### Supported Hosting Modes

Deckr should support several hosting modes through the same runtime primitives.
These modes are deployment shapes, not different architectures.

- full-stack runtime
  - runs a managed Deckr instance plus component hosting
  - may start controller, action provider runtimes, hardware managers, transports, and other
    local services in one process
- embedded application runtime
  - creates a managed Deckr instance inside another application such as a web
    service, desktop app, or test harness
  - may use lane messaging directly, start components manually, or use component
    discovery
- skinny action provider runtime
  - runs only the managed lane runtime, an action provider runtime, and the lane
    substrate
    needed to reach a controller domain
  - does not require a local controller or hardware manager
- remote hardware manager runtime
  - runs only the managed lane runtime, one or more hardware manager components,
    and the lane substrate needed to reach a controller domain
  - does not require a local controller or action provider runtime

Every mode must use the same managed lane runtime, lane contracts,
endpoint-bound send/subscribe API, recipient filtering, protocol-store behavior,
component model, and substrate binding rules. A "skinny" runtime omits
components; it does not get a thinner protocol, a different bus, or a
role-specific discovery path.

Component hosting must likewise have one mechanism. A runtime host may obtain
components through entry-point discovery, direct application registration, tests,
or explicit construction, but once a component definition or instance is resolved
it must pass through the same lane-contract validation, generic instance
configuration binding, `RunContext`, `ComponentManager`, and lifecycle
supervision.

### Component Shutdown

`ComponentManager.stop()` is a shutdown barrier. Once it returns, every child
component owned by that manager has completed its graceful `stop()`, timed out
and been force-stopped, or otherwise been removed from the manager. This remains
true when shutdown starts from a cancelled host scope such as SIGINT handling.

`RunContext.stopping` is the cooperative stop signal for component-owned tasks.
`Component.stop()` must be idempotent and bounded; it may perform final protocol
cleanup, publish lifecycle messages, withdraw Beacon advertisements, or cancel
Concord contracts while the component's borrowed runtime resources are still
valid.

Parent runtimes that register resources and pass them into hosted children must
keep those resources alive until all children using them have stopped. This is
especially important for registered endpoint lane handles: a parent must close
or withdraw an endpoint only after hosted child cleanup that may publish on that
endpoint has completed. Do not hide shutdown ordering bugs by catching and
ignoring "endpoint closed" failures during cleanup; fix the ownership order.

The bundled launcher may expose convenient presets or examples for these modes,
but presets must be expressed as ordinary component composition and explicit
transport bindings. They must not introduce hidden lane inference such as
"action provider runtime mode means action traffic" or
"hardware manager mode means hardware traffic."

### AnyIO Runtime Boundary

Deckr's Python runtime is AnyIO-native.

AnyIO is part of the Python hosting contract for:

- component lifecycle and `RunContext`
- task groups, cancellation, and stop signals
- in-process lane fan-out
- endpoint-bound lane subscription streams
- local backpressure and timeout behavior

Python hosts should either run Deckr inside an existing AnyIO-compatible async
context or use `anyio.run(...)`. Frameworks built on asyncio can host Deckr
through AnyIO's asyncio backend.

The Deckr protocol is not AnyIO-specific. Message envelopes, lane names,
delivery metadata, JSON Schema, endpoint addresses, subjects, and wire payloads
must remain runtime-agnostic and usable by non-Python implementations.

Implementation code may temporarily use backend-specific libraries behind an
adapter boundary, but backend-specific objects must not leak into Deckr's public
runtime API. Do not expose asyncio tasks, futures, streams, event loops, or
subprocess handles as Deckr protocol or component contracts.

The v1 target is that reusable Deckr Python runtime services are AnyIO-native
where practical. Any remaining asyncio-specific implementation dependencies must
be tracked explicitly and either replaced with AnyIO equivalents or isolated
behind private adapters with documented backend limits.

### Message Contract Rule

Messages on a core lane must use standardized, wire-safe Deckr contracts whether
they are delivered locally or across a transport boundary.

All core message contracts belong in `deckr`.

These core contracts include:

- the Deckr logical message envelope
- endpoint and delivery metadata
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
must not leak into application-level addressing or delivery.

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

The shared runtime context may provide generic runtime-host metadata such as:

- `component_id`
- `instance_id`
- resolved configuration
- `base_dir`
- named lane handles

That runtime context must not become a second general-purpose dependency
injection system.

In particular, the runtime context must not expose the full configuration
document to components. Components receive only their own resolved private
config mapping.

If a cross-component interaction is dynamic, it belongs on a lane.

If a shared runtime artifact is truly generic and static, it must be defined
explicitly in `deckr` as part of the runtime contract. It must not appear as an
ad hoc role-specific context key invented by one implementation.

## Reference Roles

The reference architecture includes a controller component that brokers commands
from action-provider-facing lanes to device-facing lanes.

The controller's job is to:

- connect actions to actual device controls
- manage settings and state around that process
- own user experience state
- mediate between action providers and devices
- own context ids and the mapping from contexts to devices, controls, profiles,
  and pages

All experience state belongs in controllers.

Devices should know nothing about action providers.

Action providers should know nothing about devices beyond what they learn
through the controller-mediated protocol.

Action provider runtimes are also just components. Their job is to own action
provider lifecycle and translate between action-provider-facing APIs and Deckr's
message lanes.

Action provider runtime process identities, session tokens, claim URLs, and
runtime WebSocket connections are runtime-private control-plane details. They
must not become Deckr endpoint addresses.

Drivers are also just components. Their job is to translate between concrete
hardware and Deckr's message lanes.

Hardware managers own manager-local device identity and the mapping from concrete
hardware discovery facts to Deckr hardware subjects. Concrete hardware paths,
HID paths, process ids, WebSocket sessions, MQTT topics, and transport ids must
not become durable device identity.

## Process Entrypoints And Logging

Process-wide concerns belong at process entrypoints, not in reusable components.

The standard `deckr` launcher may configure process logging because it is an
entrypoint. By default it should install plain console logging suitable for local
development and container logs. It may also accept an explicit logging
configuration file from the CLI for deployments that need richer handlers,
formatters, or file output.

Components, component factories, embedded runtime helpers, and SDK surfaces must
not configure root logging or mutate process-global logging policy. Embedded
applications own their own logging setup. The exception is a true subprocess
entrypoint because that process has its own process boundary and needs its own
entrypoint logging setup.

## Configuration

Deckr should support configuration documents such as TOML, but the architecture
does not require one concrete file format.

What matters is that configuration creates explicit component instances. Runtime
activation is not inferred from installed packages, component roles, endpoint
families, or old role-shaped table paths.

### Component Manifest Contract

Every discoverable component must declare, in its manifest or equivalent
metadata:

- `component_id`
- `consumes`
- `publishes`
- `cardinality`
- `endpoint_slots`

It may also declare extension `lane_contracts` when it owns non-core lanes.
Role metadata is descriptive only.

The intended meanings are:

- `component_id`
  - the globally unique stable identity of the component type
  - also the discovery key
- `consumes`
  - the named event bus lanes this component may read from
- `publishes`
  - the named event bus lanes this component may write to
- `cardinality`
  - whether the component type allows at most one planned instance or multiple
    planned instances
- `endpoint_slots`
  - required endpoint id slots keyed by endpoint family or component-declared
    endpoint role
- `lane_contracts`
  - extension lane contracts owned by this component type
  - must never override Deckr core lane contracts

Lane declarations describe logical lane contracts. Those contracts may be hosted
locally, or they may be transported across transport boundaries by other
components.

For most component types, `consumes` and `publishes` are fixed lists declared by
the component type itself.

For configurable components whose lane participation is instance-specific, the
component definition may provide a lane resolver, and instance configuration may
then provide the exact lane bindings for that specific instance. In the Python
host API this is `ComponentDefinition.resolve_lanes(...)`, optionally paired
with `validate_lane_bindings(...)`. In that case the runtime host must resolve
the instance's actual `consumes` and `publishes` from those explicit bindings,
not infer them from semantic role, component type, or path naming.

Current first-party component ids include:

- `dev.deckr.controller`
- `dev.deckr.action_provider_runtime.python`
- `dev.deckr.hardware.elgato`
- `dev.deckr.hardware.mirabox`
- `dev.deckr.hardware.mqtt`

Component ids are public contract identifiers and follow
[`namespaces.md`](namespaces.md).

The runtime host must use the `component` value in
`deckr.components.instances.<name>`. It must not infer meaning from path
segments such as `action_providers`, `drivers`, `services`, or `controller`.

### Generic Instance Binding

Component instances are configured under one generic namespace:

```toml
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "controller-main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "../devices"
```

The table path is launcher-local configuration address. It is not the component
type id, endpoint id, service id, provider id, hardware manager id, or runtime
protocol identity.

The generic instance wrapper fields are:

- `component`
- `instance_id`
- optional `runtime_name`
- optional `endpoints`
- optional `dependencies`
- optional `config`

Other fields at this level are invalid. Labels, annotations, provider ids,
manager ids, service namespaces, controller ids, and similar domain settings
belong inside the component-private `config` table unless they are part of a
declared generic dependency.

The runtime host must:

- discover or receive component definitions
- read explicit component instance definitions
- run explicitly configured component instance sources
- validate component ids, cardinality, endpoint slots, runtime names, endpoint
  ids, dependency declarations, lane contracts, and component config hooks
- pass only the instance's resolved private `config` mapping and generic runtime
  metadata to the component

The runtime host must not inspect sibling component sections on behalf of a
component, merge role-shaped parent namespaces implicitly, or interpret domain
settings such as provider ids or manager ids as generic runtime identity.

### Dependencies

Component dependencies are optional generic instance metadata. They are
readiness predicates, not activation rules.

```toml
[deckr.components.instances.media_actions.dependencies.media_home]
kind = "feature"
mode = "required"
endpoint = "service:media-home"
feature_id = "org.example.media.service"

[deckr.components.instances.worker.dependencies.controller_main]
kind = "feature"
mode = "observed"
feature_id = "dev.deckr.controller"
endpoint = "controller:controller-main"
```

Supported dependency kinds are:

- `feature`

Supported modes are:

- `required`
- `optional`
- `preferred`
- `observed`

Feature dependencies require a Beacon `feature_id`. They may include an
endpoint filter when the component needs a specific configured endpoint.

Dependencies never create component instances, start services, import local
objects, block `start(ctx)`, block endpoint registration, or stop a component.
The component host observes dependencies continuously through `BeaconService`
feature queries and semantic feature events. A running component can therefore
be ready, unready, or unknown while its local lifecycle remains `running`.
Dependency observations are readiness evidence only. They do not withdraw,
invalidate, or cancel existing Concord agreements.

Required dependencies with `unknown`, `degraded`, or `unsatisfied` conditions
make the effective component readiness unready. Optional, preferred, and
observed dependencies are reported in diagnostics without forcing effective
readiness unready by themselves.

Endpoint-filtered feature dependency cycles are reported as diagnostics in the
planning report, but they are not plan errors. They are rendezvous predicates,
not startup ordering.

### Activation

Component type identity and component instance identity are different concepts.

An installed component definition never starts by itself. A component starts only
when an explicit instance exists under `deckr.components.instances` or an
explicitly configured component instance source produces an instance.

`singleton` cardinality means at most one planned instance. It does not mean
"start when installed".

There is no generic runtime-host `enabled` flag. The existence of an instance
definition is the activation signal. If a component has a domain-specific
sometimes-on mode, that behavior belongs in the component and should be exposed
through readiness, Beacon features, Concord agreements, or domain state as
appropriate.

Every instantiated component has a runtime-host-scoped identity used for
lifecycle management. That runtime identity must be:

- unique within one runtime host
- derived deterministically from `component_id` and `instance_id`
- separate from protocol-level addresses carried on event lanes

Protocol addresses such as `controller:<controller_id>`,
`action_provider:<provider_instance_id>`, `hardware_manager:<manager_id>`, and
`service:<service_id>` are derived from endpoint family plus the configured
endpoint id in the instance `endpoints` map. They are not the generic lifecycle
identity of a component instance.

Deckr protocol endpoint addresses, entity subjects, client/session ids,
transport addresses, component type ids, and runtime-host component identities
are all separate concepts. The architecture must not depend on deriving one of
those identities from another by convention.

### Config Sources And Instance Sources

Config sources run before component instance planning. They can load or
preprocess configuration fragments, including optional environment-template
substitution for loaded TOML fragments. Config source declarations use ordered
arrays:

```toml
[[deckr.config.sources]]
id = "local_fragments"
source = "dev.deckr.config.files"
paths = ["./config.d/*.toml"]
env_template = true
```

The built-in file config source deep-merges map values. Later fragments replace
scalars, arrays, and map/scalar type changes and record those replacements in
the config resolution report. Config sources must not contribute
`deckr.config.sources` or `deckr.components.instance_sources`.

Component instance sources run after resolved configuration is frozen. They may
produce ordinary component instance definitions only. They must not mutate
resolved config, create config sources, create more instance sources, or trigger
recursive replanning.

```toml
[[deckr.components.instance_sources]]
id = "example_workers"
source = "com.example.deckr.workers"
```

Instance source declaration ids are host-local diagnostics identities and must
be unique within the resolved document. The `source` value is the globally
unique source definition id contributed through `deckr.component_instance_sources`
or supplied directly by an embedded host.

The `deckr` core owns the generic source protocols, planner hook, strict
duplicate checks, and planning diagnostics. Source loaders receive a reporting
callback for non-error conditions such as selected, skipped, or blocked
candidates. Domain packages own their own source definitions and private source
configuration.

For example, the Python action provider runtime package contributes
`dev.deckr.action_provider_runtime.python.installed_providers`, which
discovers selected `deckr.plugins` entry points and expands them into ordinary
`dev.deckr.action_provider_runtime.python` component instances. The Python
entry point name is discovery identity only; generated Deckr provider ids,
endpoint ids, and component instance ids are resolved by that source's
configuration. Component ids, source ids, provider ids, and provider entry point
names follow [`namespaces.md`](namespaces.md).

The common local-development shape is intentionally small:

```toml
[[deckr.components.instance_sources]]
id = "python_actions"
source = "dev.deckr.action_provider_runtime.python.installed_providers"
allow = ["dev.deckr.clock", "dev.deckr.sonos", "dev.deckr.openhab", "com.k-si.deckr.kaj"]
```

For that first-party source, omitted `block` defaults to an empty list,
`instance_id_template` defaults to `{provider_id}-main`, and
`endpoint_id_templates.action_provider` defaults to `python-{provider_id}`.

Each Python action-provider runtime instance registers
`action_provider:<provider-instance-id>` on both `actions` and `services`.
The `actions` endpoint carries controller/action traffic. The
`services` endpoint lets hosted action instances use service command/reply and
service view helpers without becoming service components themselves.

### Runtime-Local Component Status

Component lifecycle status is runtime-host local. `ComponentManager` exposes
`ComponentStatus` snapshots keyed by runtime component name. A status contains
the component lifecycle state, readiness state, readiness reasons, and
diagnostics.

Readiness is local operator visibility, not a Deckr protocol fact. A ready
component may expose no endpoints, and a running component with reachable
endpoints may still report unready for its own local reasons or because a
declared dependency is unavailable. Device, action, binding, page, service, and
settings availability remain domain state. Shared discovery and agreement
evidence is represented by Beacon and Concord: Beacon supplies discovery
candidates, while Concord supplies live agreement authority.

Python hardware managers use the shared `deckr.hardware.runtime`
implementation for the manager side of that protocol. A manager advertises its
current devices through the `dev.deckr.hardware` Beacon feature using managed
`BeaconService.ensure_advertisement` lifecycles, accepts controller ownership
only by maintaining a
`ConcordParticipantLease` on a matching
`dev.deckr.profile.hardware_claim.v1` Concord contract, and routes hardware
input or controller commands only while that contract remains valid. The removed
inventory, endpoint-presence, and unilateral device-claim current-state records
are not part of the v1 hardware-manager path.

Components may report readiness through `RunContext.status` or the convenience
reporting helpers. The component manager combines component-reported local
readiness with dependency observations to publish effective `ComponentStatus`
snapshots. Components that never report local readiness remain in `unknown`
readiness unless a required dependency is currently unready.

### Lane Substrate Replacement

The old generic transport-component model for Deckr lanes has been removed in
favor of the NATS substrate design specified in [`nats-bus.md`](nats-bus.md).

Removed targets include the home-grown WebSocket/MQTT lane transports,
`remote_endpoints`, route-table route claims, route leases, route metadata, and
trusted-bridge configuration. Those concepts should not be kept alive as a
parallel lane transport architecture.

The built-in NATS lane substrate is configured as runtime infrastructure, for
example through the bundled launcher's `[deckr.runtime.substrate]` table. It is
not discovered, instantiated, or supervised as a Deckr component. The runtime
host may either connect to an external broker or supervise a private local
`nats-server` child process through `SupervisedNatsSubstrate`; both forms expose
the same NATS-backed lane and protocol-store contract. A component or external
adapter may still use WebSocket, MQTT, USB, HID, HTTP, vendor framing, or even a
substrate-like package name at a real protocol boundary, but that does not make
it the generic Deckr lane substrate.

The live design is:

- Deckr lanes remain logical contracts.
- NATS carries distributed lane traffic.
- KV carries Beacon advertisements, Concord contracts, Concord participant
  tokens, and owner-qualified private state where appropriate.
- Lane listeners register with their Deckr endpoint address.
- The lane layer stamps envelope senders and filters received envelopes for the
  local endpoint before application code sees them.
- NATS subject, reply inbox, queue group, ops-only Service, JetStream, and KV
  concepts remain substrate mechanics below the Deckr lane contract.

This does not remove adapter-private WebSocket/MQTT protocols at action provider,
hardware, or third-party integration boundaries.

### Shared Defaults

Family-wide defaults such as "all action provider runtimes inherit from
`deckr.action_providers`" are not part of the architecture.

That kind of implicit parent lookup is an unnecessary complication and is
forbidden.

If shared defaults are ever needed, they must be introduced as an explicit,
separate configuration mechanism and resolved by the runtime host before the
component is instantiated.

The component must still receive one final resolved mapping for itself.

Even if such a defaults system is added later, it must obey these rules:

- defaults are explicit, never inferred from parent prefixes
- defaults resolution happens before handoff to the component
- the component still parses only one resolved mapping
- defaults do not create a second discovery or role-specific configuration
  mechanism

Custom runtime hosts may choose their own configuration objects, but they must
still honor the same component id, instance, endpoint slot, lane model, and
managed lane runtime contract.

### Environment Substitution

Environment substitution is a config-loading processor, not private CLI
behavior and not a component feature. It is enabled per loaded file source with
`env_template = true`. A plain bootstrap `deckr.toml` does not silently expand
environment variables, because literal strings containing `${...}` remain valid
configuration values unless a config source opts into substitution.

Substitution happens on raw TOML fragment text before TOML parsing:

```toml
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["runtime.toml"]
env_template = true
```

```toml
[deckr.components.instances.clock_actions.config.runtime]
bind_host = "${DECKR_ACTION_PROVIDER_BIND_HOST:-0.0.0.0}"
bind_port = ${DECKR_ACTION_PROVIDER_BIND_PORT:-9000}
```

The replacement text is TOML source. Operators are responsible for quoting
string values or providing full TOML literals for arrays, numbers, booleans, and
inline tables.

The runtime host must preserve the normal configuration boundary after
substitution:

- substitution is generic text rendering, not role-specific or
  component-specific interpretation
- substitution happens before TOML parsing for the opted-in fragment and before
  component instance planning
- components still receive only their final resolved configuration mapping
- missing variables without defaults fail visibly before components start
- environment values must not become transport, endpoint, action provider, hardware, or
  controller identity by accident; they are only configuration input

This feature is intended for deployment systems such as Docker, Compose,
Kubernetes, systemd, and secrets/config managers that commonly inject environment
variables. It must not become a second defaults system or a way for the launcher
to understand component-specific settings.

## Hard Rules

- There is one component model.
- There is one runtime participant model.
- Beacon is the shared weak feature discovery protocol specified in
  [`beacon-concord.md`](beacon-concord.md).
- Concord is the shared live agreement protocol specified in
  [`beacon-concord.md`](beacon-concord.md).
- After a Concord agreement is negotiated, Beacon no longer participates in that
  agreement's lifecycle, validity, or withdrawal.
- Runtime components use `BeaconService` and `ConcordService`; raw
  Beacon/Concord authority state watches belong only inside those core services
  and substrate internals.
- Lane contracts are the only generic wiring primitive.
- The runtime host creates the full core lane set before component startup.
- Core lane names belong in `deckr`.
- Lanes are logical runtime contracts and may be transported across transport
  boundaries.
- The v1 distributed lane substrate is NATS.
- Home-grown WebSocket/MQTT Deckr lane transports are removed runtime paths.
- Shared lane infrastructure owns the application-facing endpoint-bound
  send/subscribe/fan-out API.
- Required lane infrastructure must not depend on the bundled Deckr launcher.
- Core bus infrastructure is not an auto-discovered component.
- The public Python runtime API is AnyIO-native; backend-specific async objects
  must not leak into Deckr contracts.
- Core Deckr message envelopes and payloads live in `deckr`.
- Pydantic models in `deckr` are the canonical Python authoring format for core
  message contracts.
- JSON Schema generated from those contracts is the interoperability artifact
  for non-Python implementations.
- Transport-local framing may exist, but it must not redefine the carried Deckr
  message contract.
- NATS must not replace Deckr lanes, envelopes, endpoint addresses, subjects,
  Beacon, Concord, or component lifecycle semantics.
- Substrate-local identity must not leak into application-level addressing or
  delivery.
- Endpoint identity is distinct from component lifecycle identity.
- Endpoint addresses are distinct from the domain entity subjects carried by
  lane messages.
- Client/session identity, transport addresses, component runtime identity, and
  protocol endpoint identity are separate.
- Configuration creates explicit generic component instances.
- Components parse only their own resolved configuration mapping.
- Installed component definitions do not activate components.
- The runtime host does not provide a generic component `enabled` flag.
- Runtime context may carry only generic runtime-host metadata and lane handles
  as a generic primitive.
- External protocol adapters may be components, but they must not preserve the
  old generic transport-route model.
- Distributed lane substrate configuration must be explicit runtime
  infrastructure, not component discovery.
- The runtime host must not infer lane bindings from substrate kind, role name, or
  config path.
- Implicit parent-prefix inheritance is forbidden.
- Type identity and instance identity are separate.
- Lifecycle identity and protocol address identity are separate.
- Role names such as controller, hardware manager, and action provider runtime are
  descriptive only.
- Replaceability alone is not a reason to introduce a new generic runtime layer.
- Do not add shims, aliases, compatibility wrappers, or dual abstractions.
- Do not preserve broken abstractions for migration purposes.

Cross-runtime note:
`deckr-adapter-elgato-node` and the Rust hardware managers are still behind on
the new managed Beacon/Concord lifecycle model. They are tracked as follow-up work
and should be updated to consume `BeaconService`/`ConcordService` APIs in the
same ownership style as the Python reference before claiming Beacon/Concord parity.

If the implementation drifts from this model, fix the implementation. Do not
soften the architecture to accommodate accidental complexity.
