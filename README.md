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

## Release Strategy

This is a dual-package monorepo, so releases should be tracked per package, not
per repository.

- `deckr` and `deckr-controller` are independent distributions.
- The root `pyproject.toml` is workspace metadata only. It is not the source of
  truth for published package versions.
- The version source of truth for each distribution is its own package
  `pyproject.toml`:
  - `packages/deckr/pyproject.toml`
  - `packages/deckr-controller/pyproject.toml`
- Use package-scoped git tags so tags are unambiguous:
  - `deckr-vX.Y.Z`
  - `deckr-controller-vA.B.C`
- Do not use a repo-wide tag like `v0.2.0`; in a multi-package repo it is not
  clear which distribution it refers to.

### Versioning Convention

- Stable releases use plain PEP 440 semver, e.g. `0.3.0`.
- Ongoing development uses the next minor line with `.dev0`, e.g.
  `0.4.0.dev0`.
- The normal cadence should be:
  - release `X.Y.0`
  - immediately bump to `X.(Y+1).0.dev0` in a separate follow-up commit
- Only bump the package you actually released. If `deckr-controller` ships and
  `deckr` did not change, leave `deckr` alone.

### Tracking Releases

To inspect release history by package:

```bash
git tag -l 'deckr-v*' --sort=-creatordate
git tag -l 'deckr-controller-v*' --sort=-creatordate
```

To inspect unreleased changes for one package, scope the log to that package's
directory:

```bash
git log --oneline <last-deckr-tag>..HEAD -- packages/deckr
git log --oneline <last-controller-tag>..HEAD -- packages/deckr-controller
```

## Release Process

This follows the same overall style used in `invariant`, adapted for a
package-scoped monorepo.

### 1. Decide What Is Releasing

- Release `deckr` when shared contracts or reusable runtime primitives change.
- Release `deckr-controller` when controller behavior changes but the public
  `deckr` contract does not need a new release.
- Release both when controller work depends on a newly released `deckr`
  version.

If `deckr-controller` must depend on a new `deckr` release, release `deckr`
first, then update the `deckr` dependency range in
`packages/deckr-controller/pyproject.toml`, then release `deckr-controller`.

### 2. Prepare the Release

Before changing versions, make sure the workspace is clean and validation
passes:

```bash
uv run ruff check .
uv run lint-imports
uv run pytest
```

### 3. Cut the Stable Release

For the package being released:

1. Update that package's version in its package `pyproject.toml` to the stable
   release number, e.g. `0.3.0`.
2. If releasing `deckr-controller` against a newly released `deckr`, update the
   dependency constraint as well, e.g. `deckr>=0.3.0,<0.4.0`.
3. Refresh the lockfile:

   ```bash
   uv lock --refresh
   ```

4. Commit the versioned release as a package-scoped commit. Recommended commit
   titles:
   - `chore(deckr): release v0.3.0`
   - `chore(deckr-controller): release v0.3.0`

### 4. Tag the Release

Create a package-scoped tag on the release commit:

```bash
git tag deckr-v0.3.0
git tag deckr-controller-v0.3.0
```

Use the tag that matches the package you actually released. If both packages are
released on the same day, prefer separate release commits and separate tags so
each package's history stays easy to follow.

### 5. Build Artifacts From the Tag

Build from the release tag, not from `main` after the development bump, or you
will get `.dev0` artifacts.

Examples:

```bash
git checkout deckr-v0.3.0
uv build --package deckr
git checkout -
```

```bash
git checkout deckr-controller-v0.3.0
uv build --package deckr-controller
git checkout -
```

### 6. Publish

Upload the wheel and sdist built from the release tag using your normal PyPI
publishing process.

### 7. Immediately Bump Back to Development

After tagging the stable release, create a separate follow-up commit that bumps
the released package to the next development version:

1. Change the released package from `X.Y.0` to `X.(Y+1).0.dev0`.
2. Run:

   ```bash
   uv lock --refresh
   ```

3. Commit with a package-scoped message, for example:
   - `chore(deckr): bump to development release 0.4.0.dev0`
   - `chore(deckr-controller): bump to development release 0.4.0.dev0`

The stable tag should point to the stable release commit, not to the subsequent
development bump commit.

## Release Checklist

For each package release, confirm:

- The package version in its package `pyproject.toml` matches the intended
  stable or development version.
- `uv lock --refresh` has been run after every version edit.
- The tag name is package-scoped and unambiguous.
- Artifacts were built from the release tag, not from a later `.dev0` commit.
- `deckr-controller`'s `deckr` dependency range matches the intended minimum
  supported `deckr` release.
