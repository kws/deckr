# Common Core Surfaces

This document names the functional groupings in the current Python `deckr` core
library, with notes where Python runtime support now lives in
`deckr-python-runtime`, that should be represented in the cross-platform Deckr
core libraries.

The word "component" is already a runtime concept in Deckr: a `Component` is a
participant lifecycle unit hosted by a runtime. To avoid overloading that word,
this document uses **core surface** for a functional area that each first-party
language library should support.

A core surface is not a mandate to copy Python's module layout, class names, or
object model. Python, Rust, and TypeScript should expose language-native APIs
that support the same use cases, validate the same contracts, produce the same
wire/state artifacts, and pass the same conformance groups.

The authoring rules for those contract artifacts live in
[contract-authoring.md](contract-authoring.md). The current schema, fixture,
binding, vector, and conformance coverage matrix is summarized in
[contract-coverage.md](contract-coverage.md).

## Parity Rule

Parity means:

- same Deckr contract version support
- same accepted and rejected JSON shapes
- same key, subject, header, and envelope behavior
- same participant safety semantics where a runtime helper is provided
- same conformance report role/group results

Parity does not mean:

- identical package layout
- identical class hierarchy
- identical async/runtime model
- identical discovery or build tooling
- generated code in every language

For example, Python may expose Pydantic models, TypeScript may expose generated
types plus Zod/AJV validators, and Rust may expose Serde structs plus typed
constructors. Those can all be equivalent if their observable Deckr behavior is
the same.

## Required Core Surfaces

### 1. Contract Artifact Access

Current Python source:

- `deckr.contracts.artifacts`
- `deckr.contracts.models`

Use cases:

- locate and load the bundled `contract/v1` artifact set
- read the manifest and contract/spec version
- expose language-neutral schemas, fixtures, and vectors to tests and tooling
- preserve canonical JSON model behavior such as camelCase field names and
  JSON-safe values

Parity expectation: all language libraries can report which Deckr contract they
support and can validate themselves against the same checked artifact bundle.

### 2. Message Envelope And Identity

Current Python source:

- `deckr.contracts.messages`
- `deckr.actions.endpoints`

Use cases:

- construct and parse endpoint addresses such as `controller:<id>`,
  `hardware_manager:<id>`, `action_provider:<id>`, and `service:<id>`
- represent direct and broadcast targets
- represent entity subjects
- parse, validate, and serialize the `DeckrMessage` envelope
- evaluate direct-message targeting, expiry, and lane schema identifiers

Parity expectation: all language libraries produce the same envelope JSON and
make the same identity distinctions. Endpoint identity, device identity, action
identity, context identity, and substrate identity must remain separate.

### 3. Lane Contracts

Current Python source:

- `deckr.contracts.lanes`

Use cases:

- expose the core lane names and lane contract registry
- describe delivery semantics, message families, and unsupported delivery modes
- let participants and runtime helpers reason about lane capabilities

Parity expectation: every language can reason about the same lane contract
metadata, even if runtime host configuration is represented differently.

### 4. Current-State And KV Contracts

Current Python source:

- `deckr.state`
- `deckr.actions.state`
- `deckr.services.state`

Use cases:

- encode and decode key tokens
- build and parse endpoint presence keys
- build and parse hardware inventory and device claim keys
- build and parse action provider catalog keys
- build and parse service catalog, status, and namespaced view keys
- validate current-state payloads such as endpoint presence, hardware inventory,
  device claims, action catalogs, service catalogs, and service status

Parity expectation: keys and payload identity checks must be identical across
languages. A Rust hardware manager, TypeScript adapter, and Python controller
must agree on exact keys before they can share the same broker state.

### 5. Hardware Descriptors And Capabilities

Current Python source:

- `deckr.hardware.descriptors`
- `deckr.hardware.capabilities`

Use cases:

- represent `DeviceRef`, `ControlRef`, and `CapabilityRef`
- represent device, control, and capability descriptors
- validate control geometry, descriptor references, constraints, units,
  projections, source metadata, and connection metadata
- expose core capability families and their JSON-safe value or command schemas

Parity expectation: hardware managers in any language can publish descriptors
that controllers and action runtimes in any other language can validate and
consume.

### 6. Hardware Lane Messages

Current Python source:

- `deckr.hardware.messages`

Use cases:

- represent hardware availability and descriptor-change messages
- represent control input messages
- represent capability state changes, state requests, and state replies
- represent capability-targeted command messages and command
  accepted/rejected/reply messages
- derive hardware subjects for devices and capabilities

Parity expectation: a non-Python hardware manager and a Python or TypeScript
controller must agree on message types, subjects, bodies, and command reply
semantics.

### 7. Action Lane Messages

Current Python source:

- `deckr.actions.messages`

Use cases:

- represent action instance lifecycle messages
- represent binding attach/detach and page-session lifecycle messages
- represent capability-native input from controllers to providers
- represent binding output and overlays from providers to controllers
- represent settings targets, snapshots, requests, patches, and replacements
- represent action descriptors and action provider catalogs
- represent dynamic page commands and child binding descriptors
- build and parse action subjects

Parity expectation: action provider runtimes, controllers, SDKs, and adapters
can be written in different languages without changing the action protocol.

### 8. Service Messages And State

Current Python source:

- `deckr.services.messages`
- `deckr.services.state`

Use cases:

- represent service command and command-reply lane messages
- represent service errors and command status
- represent service catalogs and service status
- build and parse service state keys, including namespaced service views

Parity expectation: services are endpoint-addressed participants, not injected
Python objects. A service endpoint in one language should be usable by
controllers or providers in another.

### 9. Lane Runtime Semantics

Current Python source:

- `deckr.lanes`
- `deckr_python_runtime.lanes` for the Python endpoint-session helper

Use cases:

- validate lane messages against lane contracts
- filter direct and broadcast recipient targets
- reject malformed, expired, or recipient-session-mismatched messages
- validate reply acceptance and recipient-session fencing
- where a runtime helper is provided, register endpoint sessions and validate
  sender endpoint/session authority

Parity expectation: runtime helper APIs should be language-native, but their
observable behavior must match. The same message should be accepted, rejected,
dropped, or fenced for the same reason in every implementation.

### 10. NATS And KV Substrate Binding

Current Python source:

- `deckr.contracts.nats`
- `deckr_python_runtime.substrates.nats` for the concrete Python NATS client

Use cases:

- load the generated `bindings/nats.v1.json` artifact
- map Deckr lane messages to NATS subjects
- build lane subscription wildcard subjects
- encode Deckr headers
- encode canonical JSON payload bytes
- encode canonical current-state JSON payload bytes
- map JetStream KV entries and watches into Deckr state entries and changes
- implement Deckr bucket/key conventions
- handle missing keys and revision conflicts consistently

Parity expectation: NATS/KV is part of the v1 distributed contract. Libraries
may wrap different NATS clients, but the generated binding artifact's subject
templates, subscription wildcards, headers, payload rules, bucket names, lease
policy, and renewal cadence must match.

## Optional Or Language-Native Runtime Surfaces

These Python areas are useful but should not automatically become identical
cross-platform APIs:

- `deckr_python_runtime.runtime`
  - Python's `Deckr` helper is the current managed runtime facade. Other
    languages should offer an equivalent ergonomic entry point if useful, but
    the public shape should fit the language runtime.
- `deckr_python_runtime.components`
  - Python component manifests, readiness, dependency evaluation, entry-point
    discovery, and host internals are Python runtime concepts. Rust and
    TypeScript core libraries do not need matching component APIs to implement
    Deckr protocol behavior.
- `deckr_python_runtime.config`
  - configuration source behavior is useful in Python today, but config loading
    and plugin discovery may reasonably differ by language and host.

## Not Cross-Platform Core Surfaces

These current Python areas are not common Deckr core surfaces:

- `deckr_python_runtime.cli`
- `deckr_python_runtime.launcher`
- `deckr_python_runtime.logging`
- `deckr_python_runtime.util.anyio`
- `deckr_python_runtime.util.runtime_id`, except where a value format becomes an
  explicit contract fixture or vector
- `deckr_python_runtime.substrates.supervised_nats`
- Python package entry-point discovery
- Python component manifests, readiness, and dependency evaluation
- local smoke scripts under `libraries/python-runtime/scripts`
- schema generation internals in `scripts/generate_contract_artifacts.py`

They can remain valuable Python implementation or development tooling, but they
should not define what the Rust or TypeScript core libraries must look like.

## Conformance Direction

The interop suite should certify these surfaces in layers:

1. Static validator conformance: artifacts, schemas, fixtures, vectors, key
   algorithms, subjects, and headers.
2. Runtime client conformance: endpoint registration, sender authority,
   recipient filtering, reply fencing, and current-state operations against a
   controlled NATS/KV testbed.
3. Role conformance: hardware manager, action provider runtime, service
   endpoint, controller, or adapter behavior observed through the shared
   broker/testbed.

Each implementation should claim the roles and surfaces it supports. The suite
judges observable Deckr behavior, not whether an implementation is organized
like the Python package.

## Placement Test For New Python Code

When new Python code is proposed for `deckr`, ask:

- Does a Rust or TypeScript participant need the same behavior to speak Deckr
  correctly?
- Is this behavior visible in wire JSON, state keys, NATS subjects/headers,
  current-state payloads, or endpoint/session semantics?
- Could a conformance fixture, vector, or live scenario prove the behavior?
- Is the code Python component hosting, controller policy, device protocol
  handling, SDK ergonomics, or launcher convenience instead?

If the behavior is contract-visible and should be testable across languages, it
belongs in a common core surface. If it is a Python convenience around that
surface, keep it language-native and do not require other libraries to copy its
shape.
