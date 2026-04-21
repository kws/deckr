# deckr

`deckr` is the stable Python core for the Deckr ecosystem.

It holds the shared contracts and runtime primitives that other components can
build on without pulling in controller-specific behavior. In practice that
means:

- hardware event models and wire formats
- plugin manifests, events, and message types
- shared runtime utilities such as MQTT helpers and component lifecycle support

Python-specific invariant recipe helpers now live with the controller-side
runtime, not in `deckr` core.

The controller now lives in its own sibling repository:

- `https://github.com/kws/deckr-controller`

## Repository Layout

```text
src/deckr/
  core/        Generic runtime utilities and messaging primitives
  hardware/    Hardware-facing shared contracts and wire models
  hw/          Compatibility shim for older imports
  plugin/      Plugin-facing contracts and manifests
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

## Package Boundaries

The core architectural rule is that `deckr` stays reusable and controller-free.
If code is specific to orchestration, rendering policy, device lifecycle
management, or controller configuration, it belongs in `deckr-controller`, not
here.

Internal boundaries are enforced with `.importlinter`:

- `deckr.core` must not import `deckr.hardware`
- `deckr.core` must not import `deckr.plugin`
- `deckr.hardware` must not import `deckr.plugin`

Run the contract checks with:

```bash
uv run lint-imports
```

## Plugin Protocol Layers

The plugin contract is intentionally split into an Elgato-aligned core plus
Deckr-specific extensions:

- `deckr.plugin.core_api`
  - The minimum surface a controller-lite implementation should support.
  - Keeps `set_title`, `set_image`, `set_state`, `show_alert`, `show_ok`,
    settings, `open_url`, and `switch_to_profile` close to Stream Deck
    semantics.
- `deckr.plugin.extensions`
  - Deckr-only features such as dynamic pages, screen power control, and graph
    image capability advertisement.

The key image rule is:

- core `set_image`: Stream Deck-style image reference, typically a plugin-local
  path or a data URI / base64 image string
- Deckr graph image extension: still uses `set_image`, but the string can be a
  Deckr graph-image data URI with media type
  `application/vnd.deckr.graph+json`

## `deckr.hw` Rename

The shared hardware package has been renamed from `deckr.hw` to
`deckr.hardware`.

New code should import `deckr.hardware`. A lightweight compatibility shim is
kept in place for older `deckr.hw` imports while downstream packages migrate.

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
