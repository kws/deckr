# deckr Python core

This is the Python core library for Deckr.

The source package remains importable as `deckr` and the published Python
distribution remains named `deckr`, but this directory is only one language
library inside the language-neutral Deckr spec/core repository.

The normative contract artifacts live at the repository root under
`contract/v1`. The Python wheel includes that bundle as package data so Python
consumers can read it through `deckr.contracts.artifacts`.

Common commands from the repository root:

```bash
uv sync --project libraries/python
uv run --project libraries/python pytest
uv run --project libraries/python ruff check libraries/python scripts interop
uv run --project libraries/python lint-imports --config libraries/python/.importlinter
uv build --project libraries/python
uv run --project libraries/python python scripts/generate_contract_artifacts.py
```

Run the static Python interop validator from the repository root:

```bash
uv run --project libraries/python python interop/runners/python/static_conformance.py
```
