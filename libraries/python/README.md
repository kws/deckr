# deckr Python core

This is the Python core library for Deckr.

The source package remains importable as `deckr` and the published Python
distribution remains named `deckr`, but it is core-only: contracts, data
surfaces, deterministic key/subject/header helpers, and static conformance
behavior. It is not the Python runtime.

Python-specific runtime support lives in `../python-runtime` as the
`deckr-python-runtime` distribution with the `deckr_python_runtime` import root.
That includes Python component manifests, readiness, dependency evaluation,
component hosting, configuration, and concrete runtime substrates.

The normative contract artifacts live at the repository root under
`contract/v1`. The Python wheel includes that bundle as package data so Python
consumers can read it through `deckr.contracts.artifacts`.

Common commands from the repository root:

```bash
uv sync --project libraries/python
uv run --project libraries/python pytest libraries/python/tests --rootdir libraries/python
uv run --project libraries/python ruff check libraries/python scripts interop
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
uv build --project libraries/python
uv run --project libraries/python python scripts/generate_contract_artifacts.py
```

Run the static Python interop validator from the repository root:

```bash
uv run --project libraries/python python interop/runners/python/static_conformance.py
```
