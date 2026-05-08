# Contract Authoring

Deckr v1 is contract-first. The exported bundle under `contract/v1` is the
normative reference for language implementors:

- JSON Schemas define accepted and rejected data shapes.
- fixtures define representative valid and invalid payloads.
- vectors define deterministic helper behavior.
- `asyncapi.json` documents the NATS lane binding.
- static conformance reports prove each core library behaves against the same
  contract bundle.

`contract/v1` is generated and checked in. Do not hand-edit files inside that
directory. Neutral authoring inputs live under `contract/authoring/v1`, and the
generator applies them when rebuilding the exported bundle.

## Python Is A Compiler Layer

The current generator uses the Python core package and Pydantic models to
produce many of the JSON Schemas. That is an implementation/compiler layer, not
the product source of truth.

If Pydantic emits an undesirable data shape, change the Python model or the
generator so the generated contract matches the intended cross-language data
shape. Do not accept a Python-shaped contract just because it is convenient for
one implementation.

Rust, Python, and future TypeScript libraries may expose different native APIs.
Parity means the same observable contract behavior against `contract/v1`, not
the same module names, class hierarchy, or runtime machinery.

## Metadata Overlays

Schema descriptions, examples, comments, and Deckr-specific documentation
annotations live in `contract/authoring/v1/schema-metadata.json`.

Overlay entries are keyed by exported schema path and JSON Pointer:

```json
{
  "schema": "dev.deckr.contract.schema_metadata.v1",
  "schemas": {
    "schemas/actions/actions.v1.schema.json": {
      "": {
        "description": "Envelope schema for the actions lane.",
        "x-deckr-surface": "messages.actions"
      }
    }
  }
}
```

Allowed overlay keys are `title`, `description`, `examples`, `$comment`, and
keys prefixed with `x-deckr-`.

Validation-changing keys are not allowed in overlays. That includes `type`,
`required`, `properties`, `additionalProperties`, `enum`, `const`, `oneOf`,
`anyOf`, `allOf`, `not`, `if`, `then`, `else`, `format`, `pattern`, `minimum`,
and `maximum`. When the data shape is wrong, fix the generator/model rather
than hiding the change in documentation metadata.

Every JSON Pointer in the overlay must resolve against the generated schema.
The generator applies overlays before writing schemas and before embedding those
schemas into AsyncAPI components.

## Data-Shape Rules

These are v1 contract rules, independent of implementation language:

- Optional fields should be omitted when absent.
- Explicit `null` is only allowed where the field description says null is
  meaningful.
- Unknown object properties are rejected unless the schema explicitly models an
  extension map.
- Enum, discriminator, lane, endpoint-family, and message-type values are
  case-sensitive.
- Timestamps are RFC 3339 strings with an explicit timezone.
- Endpoint addresses, state keys, NATS subjects, and headers are contract
  values, not Python implementation details.

Invalid fixtures and invalid vector cases are part of the contract. They should
assert rejection behavior without depending on implementation-specific error
messages.

## Coverage

`contract/authoring/v1/coverage.json` maps core surfaces to exported schemas,
fixtures, vectors, and conformance groups. Keep it updated when adding or
removing artifacts so a future TypeScript implementation can see what behavior
must be implemented before claiming parity.
