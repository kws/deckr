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
- action-provider-facing messages, state shapes, and endpoint helpers
- reusable language core libraries that are not controller-specific

Do not place Python component hosting, controller orchestration, rendering
policy, filesystem config, or device-manager-specific behavior in the core
package. Python component hosting belongs in `deckr-python-runtime`; controller
policy belongs in the sibling `deckr-controller` repo.

## Directory Guide

- `contract/v1`
  - Generated, checked language-neutral v1 contract artifact bundle.
  - Includes `bindings/nats.v1.json`, the generated NATS/KV binding reference
    for subject templates, headers, payload rules, bucket names, and TTL policy.
- `contract/authoring/v1`
  - Neutral contract authoring inputs for schema metadata and coverage. These
    files feed the generator; do not hand-edit generated `contract/v1` output.
- `docs/contract-authoring.md`
  - Rules for contract-first authoring, schema metadata overlays, and v1 data
    shape policy.
- `docs/contract-coverage.md`
  - Human summary of the schema/fixture/vector/conformance coverage matrix.
- `interop`
  - Language-neutral conformance harnesses and report schemas.
- `libraries/python/src/deckr/core`
  - Removed as a Python runtime home; do not reintroduce runtime utilities under
    the core package.
- `libraries/python/src/deckr/contracts`
  - Contract artifact access, lane/message contracts, model utilities, and
    deterministic NATS binding helpers.
- `libraries/python/src/deckr/actions`
  - Runtime-neutral `actions` lane wire contracts, provider-instance catalogs,
    settings targets, endpoint helpers, and action-facing capability contracts.
- `libraries/python/src/deckr/hardware`
  - Shared hardware-facing contracts.
  - Must not depend on `deckr.actions`.
- `libraries/python/tests`
  - Tests for the core package only.
- `libraries/python-runtime/src/deckr_python_runtime`
  - Python-specific runtime support: component hosting, config loading, AnyIO
    orchestration, CLI, concrete NATS substrates, and supervised NATS.
  - Owns component manifests, readiness states, dependency declarations, and
    Python entry-point discovery.
- `libraries/python-runtime/tests`
  - Tests for the Python runtime package only.

## Placement Rules

- Put code in `deckr` only if Rust or TypeScript participants need the same
  observable behavior to speak the shared Deckr contract.
- Prefer a narrow dependency surface. Small, stable contracts are better than
  moving implementation details here for convenience.
- If a change starts to look controller-specific, stop and move it to
  `deckr-controller`.

## Dependency Rules

These are enforced in
[`libraries/python/.importlinter`](./libraries/python/.importlinter):

- `deckr.contracts` must not import `deckr.actions` or `deckr.hardware`
- `deckr.state` must not import `deckr.actions`
- `deckr.hardware` must not import `deckr.actions`
- `deckr` must not import `anyio`, `click`, `nats`, or `deckr_python_runtime`

After touching package boundaries or import structure, run:

```bash
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
```

## Development Commands

Use `uv` consistently:

```bash
uv sync --project libraries/python
uv sync --project libraries/python-runtime
uv run --project libraries/python ruff check libraries/python scripts interop
uv run --project libraries/python-runtime ruff check libraries/python-runtime
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
uv run --project libraries/python pytest libraries/python/tests --rootdir libraries/python
uv run --project libraries/python-runtime pytest libraries/python-runtime/tests --rootdir libraries/python-runtime
uv run --project libraries/python python scripts/generate_contract_artifacts.py
uv run --project libraries/python python interop/runners/python/static_conformance.py
cargo test --manifest-path libraries/rust/Cargo.toml
cargo run --manifest-path libraries/rust/Cargo.toml --bin deckr-rust-static-conformance -- --output /tmp/deckr-rust-report.json
uv build --project libraries/python
uv build --project libraries/python-runtime
```

## Release Notes

Do not duplicate the full release procedure here; follow the release section in
[README.md](./README.md#releases).

Short version:

- `libraries/python/pyproject.toml` owns the published Python `deckr` version
- `libraries/python-runtime/pyproject.toml` owns the published Python
  `deckr-python-runtime` version
- tag stable releases as `deckr-vX.Y.Z`
- after a stable release, bump immediately to the next `X.(Y+1).0.dev0`
- refresh `uv.lock` after every version change

## Hardware Imports

Use `deckr.hardware` directly in code, tests, and documentation.
