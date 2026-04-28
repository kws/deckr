# deckr

`deckr` is the shared core for the Deckr ecosystem.

It owns the reusable runtime model, lane contracts, message contracts, and
wire-safe schemas that other Deckr components build on without pulling in
controller-specific policy.
That includes:

- the `Component` runtime abstraction
- named event lanes such as `plugin_messages` and `hardware_messages`
- core Deckr message specifications and identity rules
- shared runtime utilities such as component lifecycle support and lane/substrate
  helpers
- hardware-facing and plugin-facing shared models

The normative architecture reference now lives in:

- [docs/runtime-architecture.md](docs/runtime-architecture.md)
- [docs/runtime-modes.md](docs/runtime-modes.md)

Those documents are the source of truth for the current architecture. They are
explicitly normative, alpha-stage, and intentionally non-backward-compatible.
The distributed bus replacement is still in-flight and currently lives in the
workspace planning note at
[`../notes/bus-planning.md`](../notes/bus-planning.md). Once implementation
settles, write a new formal `deckr` specification instead of resurrecting the
old route-table architecture.

The controller now lives in its own sibling repository:

- `https://github.com/kws/deckr-controller`

## Repository Layout

```text
src/deckr/
  components/  Public component model, lifecycle manager, and component host
  core/        Generic runtime primitives, lanes, lifecycle, and substrate helpers
  hardware/    Hardware-facing shared contracts and wire models
  plugin/      Plugin-facing contracts, rendering types, and protocol types
  runtime.py   Managed Deckr runtime context for lanes and endpoint lifecycle
docs/
  runtime-architecture.md
  runtime-modes.md
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
- shared lane infrastructure for application-facing send/subscribe/fan-out
- NATS as the planned distributed lane substrate rather than Deckr-specific
  WebSocket/MQTT lane transports
- core message and endpoint identity contracts defined in `deckr`
- standard messaging substrates used before Deckr builds generic broker features
  itself
- stable endpoint addresses and explicit subjects that are separate from
  component runtime ids, transport ids, sessions, topics, and paths

Controllers, drivers, plugin hosts, and protocol adapters are semantic roles, not
different architectural kinds.

If you are looking for the design rules around discovery, lane ownership,
lane substrate configuration, wire-safe schemas, configuration namespacing, and
alpha policy, read [docs/runtime-architecture.md](docs/runtime-architecture.md).

The Deckr lane substrate is being replaced with NATS. During that work, use the
workspace planning note at [`../notes/bus-planning.md`](../notes/bus-planning.md)
for current lane substrate direction, endpoint-bound lane handles, recipient
filtering, KV current state, device claims, and action resolution.

The old home-grown WebSocket/MQTT lane transports, route table, route leases,
route metadata, and remote-endpoint hint architecture are removal targets. This
does not apply to adapter-private protocols such as plugin worker attach,
external runtime attach, Elgato-compatible plugin protocol adaptation, or
concrete device protocols.

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
around `plugin_messages`, remote hardware delivery, context ids, action
addresses, and broadcast pseudo-addresses. The intended lane substrate model is
currently in [`../notes/bus-planning.md`](../notes/bus-planning.md), not in a
formal `deckr` specification yet.

`deckr.pluginhost.messages` currently contains shared plugin-host message models
used by controllers, plugin hosts, lane substrate adapters, and non-Python
implementations. Its public API shape should follow the bus planning direction
rather than preserve mistaken implementation details.

In particular, endpoint addresses such as `controller:<controller_id>`,
`host:<host_id>`, and `hardware_manager:<manager_id>` are protocol addressing
identities. They are not launcher runtime names, plugin runtime ids, WebSocket
connection ids, MQTT topics, or concrete hardware ids. Device, slot, action,
context, profile, and page references are subjects carried by lane messages, not
transport locators.

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
