# AGENTS.md

This file adds agent-specific guidance for the `deckr` repository.

Use [README.md](./README.md) as the primary source of truth for setup, package
purpose, and the human-facing release workflow. Keep shared guidance in the
README and use this file for placement rules and implementation hints.

## What Lives Here

`deckr` is the controller-independent spec/core repository for the Deckr
ecosystem.

Use this repo for:

- shared hardware contracts and wire models
- action-provider-facing manifests, messages, and lifecycle primitives
- reusable language core libraries that are not controller-specific

Do not place controller orchestration, rendering policy, filesystem config, or
device-manager-specific behavior here. That belongs in the sibling
`deckr-controller` repo.

## Directory Guide

- `contract/v1`
  - Generated, checked language-neutral v1 contract artifact bundle.
- `interop`
  - Language-neutral conformance harnesses and report schemas.
- `libraries/python/src/deckr/core`
  - Generic runtime utilities and messaging primitives.
  - Must not depend on `deckr.hardware` or `deckr.actions`.
- `libraries/python/src/deckr/actions`
  - Runtime-neutral `actions` lane wire contracts, provider-instance catalogs,
    settings targets, endpoint helpers, and action-facing capability contracts.
- `libraries/python/src/deckr/hardware`
  - Shared hardware-facing contracts.
  - Must not depend on `deckr.actions`.
- `libraries/python/tests`
  - Tests for the core package only.

## Placement Rules

- Put code in `deckr` only if it is intended to be reused by multiple Deckr
  components.
- Prefer a narrow dependency surface. Small, stable contracts are better than
  moving implementation details here for convenience.
- If a change starts to look controller-specific, stop and move it to
  `deckr-controller`.

## Dependency Rules

These are enforced in
[`libraries/python/.importlinter`](./libraries/python/.importlinter):

- `deckr.core` must not import `deckr.hardware`
- `deckr.core` must not import `deckr.actions`
- `deckr.contracts` must not import `deckr.actions` or `deckr.hardware`
- `deckr.state` must not import `deckr.actions`
- `deckr.hardware` must not import `deckr.actions`

After touching package boundaries or import structure, run:

```bash
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
```

## Development Commands

Use `uv` consistently:

```bash
uv sync --project libraries/python
uv run --project libraries/python ruff check libraries/python scripts interop
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
uv run --project libraries/python pytest
uv build --project libraries/python
```

## Release Notes

Do not duplicate the full release procedure here; follow the release section in
[README.md](./README.md#releases).

Short version:

- `libraries/python/pyproject.toml` owns the published Python `deckr` version
- tag stable releases as `deckr-vX.Y.Z`
- after a stable release, bump immediately to the next `X.(Y+1).0.dev0`
- refresh `uv.lock` after every version change

## Hardware Imports

Use `deckr.hardware` directly in code, tests, and documentation.
