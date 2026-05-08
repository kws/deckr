# deckr Python runtime

This package contains Python-specific runtime support for Deckr.

It is intentionally separate from the `deckr` Python core package. The core
package owns contract/data surfaces and static conformance behavior; this
runtime package owns AnyIO orchestration, component hosting, config loading,
logging, concrete NATS substrates, supervised NATS support, and the Python
launcher CLI.

Component manifests, readiness states, dependency declarations, and dependency
readiness evaluation live here as Python runtime concepts. They may remain
Pydantic-first because they are not language-neutral `contract/v1` artifacts.

Common commands from the repository root:

```bash
uv sync --project libraries/python-runtime
uv run --project libraries/python-runtime pytest libraries/python-runtime/tests --rootdir libraries/python-runtime
uv run --project libraries/python-runtime ruff check libraries/python-runtime
uv build --project libraries/python-runtime
```

Run the local runtime CLI with:

```bash
uv run --project libraries/python-runtime --extra cli deckr-python-runtime --help
```
