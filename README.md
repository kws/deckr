# deckr

`deckr` is the shared core for the Deckr ecosystem.

It owns the reusable runtime model, lane contracts, and wire contracts that
other Deckr components build on without pulling in controller-specific policy.
That includes:

- the `Component` runtime abstraction
- named event lanes such as `plugin_messages` and `hardware_events`
- core wire message specifications and transport-neutral contracts
- shared runtime utilities such as component lifecycle support and MQTT helpers
- hardware-facing and plugin-facing shared models

The normative architecture reference now lives in:

- [docs/runtime-architecture.md](docs/runtime-architecture.md)

That document is the source of truth for the current architecture. It is
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
- core wire contracts defined in `deckr`

Controllers, drivers, plugin hosts, and transports are semantic roles, not
different architectural kinds.

If you are looking for the design rules around discovery, lane ownership,
transport configuration, wire contracts, configuration namespacing, and alpha
policy, read [docs/runtime-architecture.md](docs/runtime-architecture.md).

## Package Boundaries

The core architectural rule is that `deckr` stays reusable and controller-free.
If code is specific to orchestration, rendering policy, device lifecycle
management, controller configuration, or controller-owned state, it belongs in
`deckr-controller`, not here.

Internal boundaries are enforced with `.importlinter`:

- `deckr.core` must not import `deckr.hardware`
- `deckr.core` must not import `deckr.plugin`
- `deckr.hardware` must not import `deckr.plugin`

Run the contract checks with:

```bash
uv run lint-imports
```

## Plugin Protocol Layers

The plugin contract is intentionally split into a small core plus Deckr-specific
extensions:

- `deckr.plugin.core_api`
  - The minimum surface a controller-lite implementation should support.
  - Keeps `set_title`, `set_image`, `show_alert`, `show_ok`, and settings
    focused on the shared controller/plugin semantics.
- `deckr.plugin.extensions`
  - Deckr-only features such as static page navigation, dynamic pages, and
    screen power control.

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
