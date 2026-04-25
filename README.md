# deckr

`deckr` is the shared core for the Deckr ecosystem.

It owns the reusable runtime model, lane contracts, message contracts, and
wire-safe schemas that other Deckr components build on without pulling in
controller-specific policy.
That includes:

- the `Component` runtime abstraction
- named event lanes such as `plugin_messages` and `hardware_events`
- core Deckr message specifications and transport-neutral routing contracts
- shared runtime utilities such as component lifecycle support and MQTT helpers
- hardware-facing and plugin-facing shared models

The normative architecture reference now lives in:

- [docs/runtime-architecture.md](docs/runtime-architecture.md)
- [docs/bus-architecture.md](docs/bus-architecture.md)

Those documents are the source of truth for the current architecture. They are
explicitly normative, alpha-stage, and intentionally non-backward-compatible.

The controller now lives in its own sibling repository:

- `https://github.com/kws/deckr-controller`

## Repository Layout

```text
src/deckr/
  core/        Generic runtime primitives, lanes, lifecycle, and transport helpers
  hardware/    Hardware-facing shared contracts and wire models
  plugin/      Plugin-facing contracts, rendering types, and protocol types
docs/
  runtime-architecture.md
  bus-architecture.md
tests/
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

Build distributables:

```bash
uv build
```

## Architecture

Deckr’s target architecture is:

- one runtime abstraction: `Component`
- one discovery model
- named event lanes as the only generic wiring primitive
- transports modeled as ordinary components
- core message and routing contracts defined in `deckr`

Controllers, drivers, plugin hosts, and transports are semantic roles, not
different architectural kinds.

If you are looking for the design rules around discovery, lane ownership,
transport configuration, wire-safe schemas, configuration namespacing, and
alpha policy, read [docs/runtime-architecture.md](docs/runtime-architecture.md).

If you are looking for the design rules around Deckr message routing,
transport-neutral envelopes, endpoint reachability, clients, broadcasts, and
disconnect cleanup, read [docs/bus-architecture.md](docs/bus-architecture.md).
That document exists because local, MQTT, WebSocket, and future transports must
carry the same Deckr messages without leaking transport identity into
application routing.

## Package Boundaries

The core architectural rule is that `deckr` stays reusable and controller-free.
If code is specific to orchestration, rendering policy, device lifecycle
management, controller configuration, or controller-owned state, it belongs in
`deckr-controller`, not here.

Internal boundaries are enforced with `.importlinter`:

- `deckr.core` must not import `deckr.hardware`
- `deckr.core` must not import `deckr.pluginhost` or `deckr.python_plugin`
- `deckr.hardware` must not import `deckr.pluginhost` or `deckr.python_plugin`
- `deckr.pluginhost` must not import `deckr.python_plugin`

Run the contract checks with:

```bash
uv run lint-imports
```

## Deckr Message Protocols

Deckr's core message protocols are the contracts spoken between Deckr
architectural endpoints such as controllers, plugin hosts, and hardware
managers. They are separate from transport protocols such as MQTT and WebSocket,
and separate from adapter-private protocols such as Elgato plugin messages or
Python plugin runtime control-plane messages.

The current implementation still has known protocol-shape gaps, especially
around `plugin_messages`. The intended bus model is documented in
[docs/bus-architecture.md](docs/bus-architecture.md): local and transported
messages must share the same Deckr routing semantics, while transport-local
framing and client/session identity stay below the application layer.

`deckr.pluginhost.messages` currently contains shared plugin-host message models
used by controllers, plugin hosts, transports, and non-Python implementations.
Its public API shape should follow the bus architecture rather than preserve
mistaken implementation details.

`deckr.python_plugin` defines only the Python plugin SDK surface. Other plugin
formats should define their own SDK/protocol surfaces instead of importing this
package. `deckr.python_plugin.interface` declares the single Python plugin API,
including action lifecycle hooks, title/image/settings commands, page
navigation, dynamic pages, and screen power control.

The key image rule is:

- core `set_image`: image reference, typically a plugin-local path or a data
  URI / base64 image string

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
