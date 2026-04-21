# deckr

`deckr` is the home of the stable Python packages that define the Deckr runtime
boundary.

This repo currently contains:

- `deckr`: shared contracts and runtime primitives for hardware events, plugin
  messaging, manifests, component lifecycle, and MQTT transport.
- `deckr-controller`: the controller/orchestrator that owns config, device
  state, routing, rendering, and controller-side services.

The repository is managed as a `uv` workspace so both packages can evolve
together while still being built and released independently.

## Repository Layout

```text
packages/
  deckr/              Core contracts and shared runtime utilities
  deckr-controller/   Controller runtime and CLI entry points
```

## Requirements

- Python 3.11+
- `uv`

## Quick Start

Install the workspace and development tooling:

```bash
uv sync --all-packages
```

Run the test suite:

```bash
uv run pytest
```

Run Ruff:

```bash
uv run ruff check .
```

Run import-linter:

```bash
uv run lint-imports
```

Build distributables:

```bash
uv build --package deckr
uv build --package deckr-controller
```

## Package Notes

### `deckr`

The `deckr` package is intended to stay small and stable. It carries the
cross-package contracts that downstream drivers, plugin hosts, and plugins can
depend on without inheriting controller internals.

### `deckr-controller`

The `deckr-controller` package depends on `deckr` and provides the runtime that
ties devices, config, and plugin-facing behavior together. It exposes the
`deckr` and `deckr-device-manager` console entry points.

## Development

This repo is a workspace, not a single publishable root package. Build and
release the individual packages from `packages/` via `uv build --package ...`.

Import boundaries are enforced with `.importlinter` so `deckr-controller` can
depend on `deckr`, but the core `deckr` layers cannot depend back on
`deckr.controller`.

`uv.lock` is committed to keep the development environment reproducible.
