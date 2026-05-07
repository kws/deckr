# Deckr interop

This directory contains language-neutral conformance harnesses for the Deckr
contract bundle.

The first harness is static Python validator conformance. It proves the Python
core library can consume `contract/v1`, validate the declared fixtures, and
match the published vectors. Later TypeScript and Rust libraries should produce
the same report shape for the same static groups before live NATS scenarios are
added.

Run from the repository root:

```bash
uv run --project libraries/python python interop/runners/python/static_conformance.py
```

Write a report to disk:

```bash
uv run --project libraries/python python interop/runners/python/static_conformance.py \
  --output report.json
```
