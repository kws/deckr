# Contract Coverage

`contract/authoring/v1/coverage.json` is the machine-readable coverage matrix
for `contract/v1`. It maps each core surface to the bindings, schemas, fixtures,
vectors, and static conformance groups that currently certify that surface.

The current required static conformance groups are:

- `artifacts.manifest`
- `schemas.fixtures`
- `vectors.keys`
- `vectors.identity`
- `messages.actions`
- `messages.hardware`
- `messages.services`
- `runtime.lane`
- `substrate.nats`

The matrix is intentionally not a compatibility promise for Python API layout.
It is a checklist of observable contract behavior. Python, Rust, and future
TypeScript libraries can expose language-native modules as long as they satisfy
the same artifact coverage and conformance groups.

## Surface Summary

`artifacts.manifest` covers the generated bundle manifest and AsyncAPI entry
point.

`schemas.fixtures` covers all exported JSON Schemas and all valid/invalid
fixtures. Invalid fixtures include missing required fields, unknown top-level
fields, invalid casing, malformed endpoint addresses, malformed timestamps, and
explicit `null` where the schema does not allow null.

`vectors.keys` covers key-token encoding and current-state key builders and
parsers, including invalid parser cases.

`vectors.identity` covers endpoint parsing and entity subject helpers, including
case-sensitive endpoint-family rejection.

`messages.actions`, `messages.hardware`, and `messages.services` cover the
valid message fixtures and language helper behavior for the three current core
lanes.

`state.kv` covers current-state payload schemas, state fixtures, key-token
vectors, and state-key vectors.

`runtime.lane` covers deterministic lane runtime semantics: expiry, endpoint
targeting, recipient-session checks, and deliverability.

`substrate.nats` covers the generated NATS binding artifact, lane subjects,
headers, default current-state buckets, TTL policy, and canonical payload JSON
bytes for lane messages.
