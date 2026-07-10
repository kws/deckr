# deckr

`deckr` is the shared core for the Deckr ecosystem.

It owns the reusable runtime model, lane contracts, message contracts, and
wire-safe schemas that other Deckr components build on without pulling in
controller-specific policy.
That includes:

- the `Component` runtime abstraction
- named event lanes such as `actions` and `hardware_messages`
- core Deckr message specifications and identity rules
- shared runtime utilities such as component lifecycle support and lane/substrate
  helpers
- hardware-facing and action-provider-facing shared models

The normative architecture and protocol references now live in:

- [docs/runtime-architecture.md](docs/runtime-architecture.md)
- [docs/runtime-modes.md](docs/runtime-modes.md)
- [docs/migration-guide.md](docs/migration-guide.md)
- [docs/beacon-concord.md](docs/beacon-concord.md)
- [docs/nats-bus.md](docs/nats-bus.md)

Those documents are the source of truth for the current architecture and
protocol contract. They are explicitly normative, alpha-stage, and
intentionally non-backward-compatible.
The distributed bus replacement has closed on NATS as the Deckr distributed
substrate: Core NATS carries lane traffic and JetStream KV carries current
state. The supported NATS/KV contract now lives in
[docs/nats-bus.md](docs/nats-bus.md). Future bus ideas live outside this package
until they become implementor- or user-relevant specification.

The controller now lives in its own sibling repository:

- `https://github.com/kws/deckr-controller`

## Repository Layout

```text
src/deckr/
  components/  Public component model, lifecycle manager, and component host
  contracts/   Wire-safe message, envelope, endpoint, and lane contracts
  core/        Generic config, logging, and runtime utility helpers
  actions/     Runtime-neutral actions lane body and endpoint contracts
  beacon.py    Generic feature advertisement discovery protocol
  concord.py   Generic participant-token contract protocol
  profiles/    Deckr hardware/action Beacon payloads and Concord terms
  hardware/    Hardware-facing shared contracts and wire models
  substrates/  Message bus implementations, currently NATS
  lanes.py     Endpoint sessions, lane views, and message validation
  runtime.py   Managed Deckr runtime context for lanes and endpoint lifecycle
docs/
  beacon-concord.md
  migration-guide.md
  nats-bus.md
  runtime-architecture.md
  runtime-modes.md
contract/v1/
  index.html
  manifest.json
  schemas/
  fixtures/
  vectors/
rust/deckr/
  src/        Rust implementation of shared contract primitives
  tests/      Rust conformance tests against contract/v1 artifacts
typescript/deckr/
  src/        TypeScript implementation of shared contract primitives
  tests/      TypeScript conformance and lifecycle tests
tests/
```

The generated `contract/v1/` bundle is the checked, language-neutral v1
contract artifact set. Open `contract/v1/index.html` locally to browse the
manifest, schemas, fixtures, and vectors. Regenerate it with:

```bash
uv run python scripts/generate_contract_artifacts.py
```

## Requirements

- Python 3.11+
- `uv`

## Quick Start

Install the project and development tooling:

```bash
uv sync
```

Run the default validation suite:

```bash
uv run ruff check .
uv run lint-imports
uv run pytest
```

Focused protocol tests should import `MemoryJsonKvBucket`,
`ConcordRuntimeHarness`, or `ConcordMaintenanceHarness` from `deckr.testing`.
Use the runtime harness for ordinary Concord tests and the maintenance harness
only for reaper/maintenance coverage; workspace tests must not construct
`Concord` directly from three stores. See the
[migration guide](docs/migration-guide.md#test-rewrites) for examples.

Run the TypeScript core conformance checks:

```bash
cd typescript/deckr
npm install
npm test
npm run typecheck
```

Build distributables:

```bash
uv build
```

## Architecture

Deckr’s target architecture is:

- one runtime abstraction: `Component`
- one shared discovery/agreement model: Beacon advertisements for weak
  feature discovery and Concord contracts for live agreements
- named event lanes as the only generic wiring primitive
- shared lane infrastructure for application-facing send/subscribe/fan-out
- NATS as the distributed message bus rather than Deckr-specific
  WebSocket/MQTT lane transports
- core message and endpoint identity contracts defined in `deckr`
- standard messaging substrates used before Deckr builds generic broker features
  itself
- stable endpoint addresses and explicit subjects that are separate from
  component runtime ids, transport ids, sessions, topics, and paths

Controllers, hardware managers, action provider runtimes, and protocol adapters
are semantic roles, not different architectural kinds.

Beacon is only candidate discovery. After participants negotiate a Concord
contract, that contract's validity and withdrawal are governed by Concord
contracts and participant tokens, not by continued Beacon advertisement
presence.
The Python runtime owns Beacon/Concord materialized KV views, reconciles watch
recovery snapshots, and coalesces no-op heartbeat writes behind TTL-derived
cadence rules. The optional Concord reaper is the maintenance exception: it runs
infrequently and uses exact raw KV scans to clear orphaned stale observations and
bound the cancelled-contract archive.

If you are looking for the design rules around discovery, endpoint sessions,
message bus configuration, wire-safe schemas, component planning, and alpha
policy, read [docs/runtime-architecture.md](docs/runtime-architecture.md).
Public contract identifier ownership and collision-avoidance rules live in
[docs/namespaces.md](docs/namespaces.md).
Beacon and Concord protocol semantics for non-Python implementors live in
[docs/beacon-concord.md](docs/beacon-concord.md).

The Deckr distributed message bus is NATS. Read
[docs/nats-bus.md](docs/nats-bus.md) for endpoint sessions, recipient filtering,
Beacon/Concord KV stores, and broker diagnostics.

The old home-grown WebSocket/MQTT lane transports, route table, route leases,
route metadata, and remote-endpoint hint architecture are removal targets. This
does not apply to adapter-private protocols such as external runtime attach,
third-party plugin protocol adaptation, or concrete device protocols.

The NATS substrate surface is available behind the optional `deckr[nats]` extra.
Use `Deckr.endpoint(...)` for endpoint-session lane messages, `Deckr.beacon` for
managed Beacon discovery, `Deckr.concord` for managed agreement state, and
`Deckr.kv_bucket(...)` for explicit NATS KV buckets. Service consumers use the
managed `Deckr.services(endpoint)` context and `DeckrServices.use_matching(...)`
from `deckr.services`; that managed client owns service discovery, service-use
negotiation, request authority, and protected view reads/watches. Consumers must
not scan Beacon KV, duplicate descriptor parsing loops, classify Concord
terminal statuses, or query Concord as a catalog. Direct Beacon/Concord
primitives and direct `ServiceViewStore` construction are for core runtime,
service infrastructure, hardware infrastructure, and conformance tests.
The optional
`deckr[supervised-nats]` extra also installs the first-party
`deckr-nats-server-bin` binary package so embedded hosts and the `deckr` launcher
can supervise a private local `nats-server` process. This is still the same
NATS/KV runtime contract, not an in-memory or no-NATS product mode.

`scripts/nats_state_report.py` summarizes the broker's current Deckr
Beacon/Concord communication state. The NATS bus docs also cover JetStream
consumer hygiene checks for watch paths.

```bash
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```

## Package Boundaries

The core architectural rule is that `deckr` stays reusable and controller-free.
If code is specific to orchestration, rendering policy, device lifecycle
management, controller configuration, or controller-owned state, it belongs in
`deckr-controller`, not here.

Internal boundaries are enforced with `.importlinter`:

- `deckr.core` must not import `deckr.hardware`
- `deckr.core` must not import `deckr.actions`
- `deckr.contracts` must not import `deckr.actions` or `deckr.hardware`
- `deckr.hardware` must not import `deckr.actions`

Run the contract checks with:

```bash
uv run lint-imports
```

## Deckr Message Protocols

Deckr's core message protocols are the contracts spoken between Deckr
architectural endpoints such as controllers, action providers, and hardware
managers. They are separate from transport protocols such as MQTT and WebSocket,
and separate from adapter-private third-party protocols.

The supported message bus and protocol-store model is defined in
[docs/nats-bus.md](docs/nats-bus.md). `deckr.action_runtime`,
`deckr.actions.messages`, and `deckr.actions.endpoints` contain the shared
Action Runtime service contracts used by controllers, action provider runtimes,
service bus adapters, and non-Python implementations. The v1 action contract is
capability-native: action descriptors may declare capability requirements,
lifecycle messages carry structured action-instance, binding, and page-session
metadata, input is represented as capability input, and output requests target
matched capabilities through generation-scoped binding output.

Dynamic page commands carry one target per child binding. A child may target
`self`, meaning the page opener's current action instance, or it may target an
explicit action selector that the controller resolves to an action provider
instance and page-scoped child action instance. Providers do not send
`actionInstanceId` values for dynamic children; those live identities remain
controller-owned.

In particular, endpoint addresses such as `controller:<controller_id>`,
`service:<service_id>`, and `hardware_manager:<manager_id>` are protocol
addressing identities. They are not launcher runtime names, action provider
runtime ids, WebSocket connection ids, MQTT topics, or concrete hardware ids.
Device, control, capability, action, context, profile, and page references are
subjects carried by lane messages, not transport locators.

The key output rule is:

- core action output targets a matched capability through binding-scoped
  commands such as raster `set_frame` and `clear`; third-party command names
  belong only at adapter boundaries.

## Hardware Package

The shared hardware package lives at `deckr.hardware`.

Import `deckr.hardware` directly in all code and docs.

## Releases

This repository now releases a single distribution: `deckr`.

- The source of truth for the published version is the root `pyproject.toml`.
- Use package tags in the form `deckr-vX.Y.Z`.
- Stable releases use normal PEP 440 versions such as `0.3.0`.
- After each stable release, bump immediately to the next development line,
  e.g. `0.4.0.dev0`, in a separate follow-up commit.

### Release Flow

1. Update `version` in `pyproject.toml` to the stable release number.
2. Run the validation suite:

   ```bash
   uv run ruff check .
   uv run lint-imports
   uv run pytest
   ```

3. Refresh the lockfile:

   ```bash
   uv lock --refresh
   ```

4. Commit the release, for example:

   ```bash
   git commit -am "chore(deckr): release v0.3.0"
   ```

5. Tag the release commit:

   ```bash
   git tag deckr-v0.3.0
   ```

6. Build from the tag so the artifacts match the stable version exactly:

   ```bash
   git checkout deckr-v0.3.0
   uv build
   git checkout -
   ```

7. Publish the wheel and sdist using your usual PyPI workflow.
8. Immediately bump `pyproject.toml` to the next development version, refresh
   the lockfile, and commit that separately:

   ```bash
   uv lock --refresh
   git commit -am "chore(deckr): bump to development release 0.4.0.dev0"
   ```

The stable tag should always point at the stable release commit, not the later
`.dev0` commit.
