# deckr

`deckr` is the shared spec/core repository for the Deckr ecosystem.

It owns the lane contracts, message contracts, wire-safe schemas, and
first-party language core libraries that other Deckr components build on
without pulling in controller-specific policy or a privileged Python runtime.
That includes:

- named event lanes such as `actions` and `hardware_messages`
- core Deckr message specifications and identity rules
- deterministic lane, current-state, and NATS binding helpers
- hardware-facing and action-provider-facing shared models

The Python implementation is split into two packages:

- `deckr`: core contract/data surfaces only
- `deckr-python-runtime`: Python-specific runtime support, component hosting,
  config loading, AnyIO orchestration, CLI, and concrete NATS substrates

The normative architecture reference now lives in:

- [docs/runtime-architecture.md](docs/runtime-architecture.md)
- [docs/runtime-modes.md](docs/runtime-modes.md)
- [docs/nats-bus.md](docs/nats-bus.md)
- [docs/core-surfaces.md](docs/core-surfaces.md)
- [docs/contract-authoring.md](docs/contract-authoring.md)
- [docs/contract-coverage.md](docs/contract-coverage.md)

Those documents are the source of truth for the current architecture. They are
explicitly normative, alpha-stage, and intentionally non-backward-compatible.
The distributed bus replacement has closed on NATS as the Deckr distributed
substrate: Core NATS carries lane traffic and JetStream KV carries current
state. The supported NATS/KV contract now lives in
[docs/nats-bus.md](docs/nats-bus.md). Future bus ideas live outside this package
until they become implementor- or user-relevant specification.

The controller now lives in its own sibling repository:

- `https://github.com/kws/deckr-controller`

## Repository Layout

```text
contract/v1/
  index.html
  manifest.json
  asyncapi.json
  schemas/
  fixtures/
  vectors/
contract/authoring/v1/
  schema-metadata.json
  coverage.json
docs/
  contract-authoring.md
  contract-coverage.md
  core-surfaces.md
  nats-bus.md
  runtime-architecture.md
  runtime-modes.md
interop/
  report.schema.json
  runners/
libraries/
  python/
    pyproject.toml
    src/deckr/
    tests/
  python-runtime/
    pyproject.toml
    src/deckr_python_runtime/
    tests/
  rust/
    Cargo.toml
    src/
scripts/
  generate_contract_artifacts.py
```

The generated `contract/v1/` bundle is the checked, language-neutral v1
contract artifact set. Open `contract/v1/index.html` locally for the AsyncAPI
browser, or use `contract/v1/asyncapi.json` directly with AsyncAPI-compatible
tooling. The manifest, JSON schemas, fixtures, and vectors remain available as
plain files.

Neutral authoring inputs for schema descriptions, examples, and coverage live
under `contract/authoring/v1/`. Python/Pydantic is currently the compiler layer
for many schema shapes, but exported contract behavior wins over Python
convenience. Regenerate the bundle with:

```bash
uv run --project libraries/python python scripts/generate_contract_artifacts.py
```

## Requirements

- Python 3.11+
- `uv`
- Rust toolchain with `cargo`

## Quick Start

Install the Python core and runtime development tooling:

```bash
uv sync --project libraries/python
uv sync --project libraries/python-runtime
```

Run the default validation suite:

```bash
uv run --project libraries/python ruff check libraries/python scripts interop
uv run --project libraries/python-runtime ruff check libraries/python-runtime
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
uv run --project libraries/python pytest libraries/python/tests --rootdir libraries/python
uv run --project libraries/python-runtime pytest libraries/python-runtime/tests --rootdir libraries/python-runtime
uv run --project libraries/python python interop/runners/python/static_conformance.py
cargo test --manifest-path libraries/rust/Cargo.toml
cargo run --manifest-path libraries/rust/Cargo.toml --bin deckr-rust-static-conformance -- --output /tmp/deckr-rust-report.json
```

Build distributables:

```bash
uv build --project libraries/python
uv build --project libraries/python-runtime
cargo build --manifest-path libraries/rust/Cargo.toml
```

The Python and Rust libraries are peers. Parity means the same observable
contract behavior against `contract/v1`, not identical module names or API
layout.

## Architecture

Deckr’s target architecture is:

- named event lanes as the only generic wiring primitive
- shared lane infrastructure for application-facing send/subscribe/fan-out
- NATS as the distributed lane substrate rather than Deckr-specific
  WebSocket/MQTT lane transports
- core message and endpoint identity contracts defined in `deckr`
- standard messaging substrates used before Deckr builds generic broker features
  itself
- stable endpoint addresses and explicit subjects that are separate from
  component runtime ids, transport ids, sessions, topics, and paths

Controllers, hardware managers, action provider runtimes, and protocol adapters
are protocol roles, not separate core contract families.

The Python runtime package exposes a local `Component` hosting model, discovery
model, manifests, readiness, and dependency evaluation. Those are Python
runtime concepts, not parity surfaces that Rust or TypeScript core libraries
must implement to speak Deckr lanes.

If you are looking for the design rules around discovery, lane ownership,
lane substrate configuration, wire-safe schemas, component planning, and alpha
policy, read [docs/runtime-architecture.md](docs/runtime-architecture.md).
Public contract identifier ownership and collision-avoidance rules live in
[docs/namespaces.md](docs/namespaces.md).

The Deckr distributed lane substrate is NATS. Read
[docs/nats-bus.md](docs/nats-bus.md) for endpoint-bound lane handles, recipient
filtering, KV current state, device claims, action resolution, and broker
diagnostics.

The old home-grown WebSocket/MQTT lane transports, route table, route leases,
route metadata, and remote-endpoint hint architecture are removal targets. This
does not apply to adapter-private protocols such as external runtime attach,
third-party plugin protocol adaptation, or concrete device protocols.

The Python NATS substrate surface lives in `deckr-python-runtime` behind the
optional `deckr-python-runtime[nats]` extra. Use
`deckr_python_runtime.runtime.Deckr.lane(...).register_endpoint(...)` for
endpoint-session lane messages and `Deckr.state(...)` for current-state
declarations. The optional `deckr-python-runtime[supervised-nats]` extra also
installs the first-party `deckr-nats-server-bin` binary package so embedded
hosts and the `deckr-python-runtime` launcher can supervise a private local
`nats-server` process. This is still the same NATS/KV runtime contract, not an
in-memory or no-NATS product mode.

A real-NATS smoke harness is available at
`libraries/python-runtime/scripts/nats_smoke.py`, and
`libraries/python-runtime/scripts/nats_state_report.py` summarizes the broker's
current Deckr communication state.

Run the smoke harness against the included JetStream-enabled NATS compose service:

```bash
docker compose -f docker/compose.nats-smoke.yaml up -d nats
uv run --project libraries/python-runtime --extra nats python libraries/python-runtime/scripts/nats_smoke.py --url nats://127.0.0.1:4222 --check-ttl
uv run --project libraries/python-runtime --extra nats python libraries/python-runtime/scripts/nats_state_report.py --url nats://127.0.0.1:4222
docker compose -f docker/compose.nats-smoke.yaml down -v
```

Run the same smoke harness with a supervised local NATS server:

```bash
uv run --project libraries/python-runtime --extra supervised-nats python libraries/python-runtime/scripts/nats_smoke.py --supervised --check-ttl
```

## Package Boundaries

The core architectural rule is that `deckr` stays reusable, controller-free, and
runtime-neutral. Python runtime support belongs in `deckr-python-runtime`; if
code is specific to component hosting, orchestration, rendering policy, device
lifecycle management, controller configuration, or controller-owned state, it
belongs outside the core package.

Internal boundaries are enforced with `libraries/python/.importlinter`:

- `deckr.contracts` must not import `deckr.actions` or `deckr.hardware`
- `deckr.state` must not import `deckr.actions`
- `deckr.hardware` must not import `deckr.actions`
- `deckr` must not import `anyio`, `click`, `nats`, or `deckr_python_runtime`

Run the contract checks with:

```bash
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
```

## Deckr Message Protocols

Deckr's core message protocols are the contracts spoken between Deckr
architectural endpoints such as controllers, action providers, and hardware
managers. They are separate from transport protocols such as MQTT and WebSocket,
and separate from adapter-private third-party protocols.

The supported lane substrate and current-state model is defined in
[docs/nats-bus.md](docs/nats-bus.md). `deckr.actions.messages`,
`deckr.actions.endpoints`, and `deckr.actions.state` contain the shared
`actions` lane contracts used by controllers, action provider runtimes, lane
substrate adapters, and non-Python implementations. The v1 action contract is
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
`action_provider:<provider_instance_id>`, and
`hardware_manager:<manager_id>` are protocol addressing identities. They are not
launcher runtime names, action provider runtime ids, WebSocket connection ids,
MQTT topics, or concrete hardware ids. Device, control, capability, action,
context, profile, and page references are subjects carried by lane messages, not
transport locators.

The key output rule is:

- core action output targets a matched capability through binding-scoped
  commands such as raster `set_frame` and `clear`; third-party command names
  belong only at adapter boundaries.

## Hardware Package

The shared hardware package lives at `deckr.hardware`.

Import `deckr.hardware` directly in all code and docs.

## Releases

This repository currently releases two Python distributions: `deckr` for core
contracts/data and `deckr-python-runtime` for Python runtime support.

- The source of truth for the Python core published version is
  `libraries/python/pyproject.toml`; the runtime version lives in
  `libraries/python-runtime/pyproject.toml`.
- Use package tags in the form `deckr-vX.Y.Z`.
- Stable releases use normal PEP 440 versions such as `0.3.0`.
- After each stable release, bump immediately to the next development line,
  e.g. `0.4.0.dev0`, in a separate follow-up commit.

### Release Flow

1. Update `version` in `libraries/python/pyproject.toml` and
   `libraries/python-runtime/pyproject.toml` to the stable release number when
   releasing both Python packages.
2. Run the validation suite:

   ```bash
   uv run --project libraries/python ruff check libraries/python scripts interop
   uv run --project libraries/python-runtime ruff check libraries/python-runtime
   uv run --project libraries/python lint-imports --config libraries/python/.importlinter
   uv run --project libraries/python pytest libraries/python/tests --rootdir libraries/python
   uv run --project libraries/python-runtime pytest libraries/python-runtime/tests --rootdir libraries/python-runtime
   uv run --project libraries/python python interop/runners/python/static_conformance.py
   cargo test --manifest-path libraries/rust/Cargo.toml
   cargo run --manifest-path libraries/rust/Cargo.toml --bin deckr-rust-static-conformance -- --output /tmp/deckr-rust-report.json
   ```

3. Refresh the lockfile:

   ```bash
   uv lock --project libraries/python --refresh
   uv lock --project libraries/python-runtime --refresh
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
   uv build --project libraries/python
   uv build --project libraries/python-runtime
   git checkout -
   ```

7. Publish the wheel and sdist using your usual PyPI workflow.
8. Immediately bump changed package versions to the next development version,
   refresh lockfiles, and commit that separately:

   ```bash
   uv lock --project libraries/python --refresh
   uv lock --project libraries/python-runtime --refresh
   git commit -am "chore(deckr): bump to development release 0.4.0.dev0"
   ```

The stable tag should always point at the stable release commit, not the later
`.dev0` commit.
