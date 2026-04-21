# AGENTS.md

This file is for AI-agent-specific guidance for the `deckr` repository.

For general developer-facing setup, package overview, and the full release
workflow, see [README.md](./README.md). Keep README as the primary source of
truth for shared developer guidance; use this file for agent hints, decision
rules, and repo-specific working conventions.

## What This Repo Is

This repository is a dual-package `uv` workspace:

- `packages/deckr`
  - Shared contracts and reusable runtime primitives.
  - This is the stable downstream dependency surface for other Deckr packages.
- `packages/deckr-controller`
  - The controller/orchestrator runtime.
  - This package depends on `deckr` and should stay downstream of it.

The root `pyproject.toml` is workspace metadata, not the source of truth for a
published package version.

## Directory Guide

Use this when deciding where code belongs:

- `packages/deckr/src/deckr/core`
  - Generic runtime utilities and messaging primitives.
  - Should not depend on `deckr.hw`, `deckr.plugin`, or `deckr.controller`.
- `packages/deckr/src/deckr/hw`
  - Hardware-facing shared contracts and wire models.
  - Should not depend on `deckr.plugin` or `deckr.controller`.
- `packages/deckr/src/deckr/plugin`
  - Plugin-facing shared contracts, manifests, events, and wire formats.
  - Should not depend on `deckr.hw` or `deckr.controller`.
- `packages/deckr-controller/src/deckr/controller`
  - Controller-specific orchestration, config, rendering, routing, and
    persistence.
- `packages/deckr/tests`
  - Tests for the shared `deckr` package only.
- `packages/deckr-controller/tests`
  - Tests for the controller package only.

## Package Placement Rules

When adding or moving code:

- Put code in `deckr` if it defines a reusable contract or primitive that could
  be consumed outside the controller.
- Put code in `deckr-controller` if it is orchestration logic, controller state,
  rendering policy, controller config, or controller-only behavior.
- Do not move controller-specific implementation details into `deckr` just to
  "share" them. `deckr` should stay intentionally small.
- If a change introduces a new controller dependency from `deckr`, stop and
  rethink the boundary first.

## Dependency Direction

The most important architectural rule in this repo is one-way dependency flow:

- `deckr-controller` may depend on `deckr`
- `deckr` must not depend on `deckr-controller`

Internal `deckr` layering matters too:

- `deckr.core` must not import `deckr.hw`
- `deckr.core` must not import `deckr.plugin`
- `deckr.hw` must not import `deckr.plugin` or `deckr.controller`
- `deckr.plugin` must not import `deckr.hw` or `deckr.controller`

These rules are enforced by [`.importlinter`](./.importlinter). After touching
package boundaries or imports, run:

```bash
uv run lint-imports
```

## Development Commands

This repo uses `uv`. Prefer the README for the general workflow, but as an
agent, the commands you will usually need are:

```bash
uv sync --all-packages
uv run ruff check .
uv run lint-imports
uv run pytest
```

Do not use bare `python`, `pytest`, or `ruff` when working in this repo unless
there is a very specific reason to bypass `uv`.

## Versioning And Releases

The full human-facing release process lives in the
[README release section](./README.md#release-strategy). Do not duplicate that
process here unless the README changes.

The short agent version:

- Treat this as a package-scoped monorepo, not a repo-scoped release stream.
- `packages/deckr/pyproject.toml` owns the `deckr` version.
- `packages/deckr-controller/pyproject.toml` owns the `deckr-controller`
  version.
- Use package-scoped tags:
  - `deckr-vX.Y.Z`
  - `deckr-controller-vX.Y.Z`
- After changing any package version, run:

```bash
uv lock --refresh
```

- After a stable release, bump only the released package to the next minor
  `.dev0` line in a separate commit, as documented in the README.

## Release-Sensitive Change Rules

When making packaging or release-related edits:

- Do not edit the root workspace version expecting it to affect published
  packages.
- If `deckr-controller` starts requiring a newly released `deckr`, update the
  dependency constraint in `packages/deckr-controller/pyproject.toml`.
- Build release artifacts from the release tag, not from a later development
  commit. See README for the exact sequence.

## Testing Expectations

For normal code changes, the default verification set is:

```bash
uv run ruff check .
uv run lint-imports
uv run pytest
```

If a change is clearly scoped to one package, it is fine to start with the
package-specific tests, but do not skip `lint-imports` when import boundaries
are involved.

## Documentation Maintenance

Keep documentation split by audience:

- `README.md`
  - Information useful to all developers.
  - Setup, workspace usage, release workflow, and package overview.
- `AGENTS.md`
  - Extra guidance for AI agents.
  - Decision rules, placement heuristics, and reminders about boundary
    enforcement.

If guidance would matter equally to a human maintainer, it probably belongs in
the README first.
