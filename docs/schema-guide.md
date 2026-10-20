# Schema guide

Select an entry point from the [Deckr message catalog](deckr-message-catalog.md)
or the [schema index](../contract/v1/index.html).
Load its dependencies into an offline Draft 2020-12 registry.
After structural validation, apply the profile and authority checks.
For implementation guidance, see [Deckr hardware](deckr-hardware.md#implementation-guidance)
and [Deckr services](deckr-services.md#implementation-guidance).

## Authority and dialect

The JSON Schemas in `contract/v1/schemas` are authoritative for JSON structure.
They use JSON Schema Draft 2020-12. Normative protocol specifications and
[semantic validation rules](semantic-rules.md) define the additional requirements that
cannot be expressed by a structural schema. Implementations and generated
language types must conform to these contracts.

Contract changes start here, before implementation changes. Review wire shape,
semantic rules and conformance artifacts together, then have consumers pin the
accepted revision. Generated types and validators are downstream outputs owned
by those consumers; this repository's schemas remain the authored source.

The [manifest](../contract/v1/manifest.json) inventories every schema, catalog,
fixture and vector. `id` and `schemaId` on a schema entry are its absolute `$id`.
`wireSchemaId`, where present, records the existing reverse-DNS label used by
messages, profiles, capability descriptors or schema registries. Do not replace
an on-wire `schema` or `profile` value with a document URL.

## Choosing a schema

| Input | Entry point |
| --- | --- |
| Any application's Beacon advertisement | [Protocol advertisement](../contract/v1/schemas/protocols/beacon/advertisement.v1.schema.json) |
| Any application's Concord agreement | [Protocol contract](../contract/v1/schemas/protocols/concord/contract.v1.schema.json) |
| Participant token | [Protocol token](../contract/v1/schemas/protocols/concord/participant-token.v1.schema.json) |
| Deckr hardware advertisement including payload | [Hardware advertisement](../contract/v1/schemas/deckr/hardware/advertisement.v1.schema.json) |
| Hardware claim including terms | [Hardware claim contract](../contract/v1/schemas/deckr/hardware/claim-contract.v1.schema.json) |
| Any supported hardware lane message | [Hardware messages](../contract/v1/schemas/deckr/hardware/messages.v1.schema.json) |
| Generic Deckr service lane message | [Service message](../contract/v1/schemas/deckr/services/message.v1.schema.json) |
| Action Runtime service message, including typed event/params | [Action Runtime messages](../contract/v1/schemas/deckr/action-runtime/messages.v1.schema.json) |
| Stored Action Runtime availability, including authority fields | [Availability view](../contract/v1/schemas/deckr/action-runtime/availability-view.v1.schema.json) |
| Availability payload before storage fields are added | [Availability payload](../contract/v1/schemas/deckr/action-runtime/availability-payload.v1.schema.json) |

The generic service schema deliberately accepts application-owned `params`,
`event`, `result` and diagnostics. It does not validate an Action Runtime payload.
Use the application-specific entry point, selected by service namespace and
message name, or validate against the service's declared payload schema.

All eight hardware messages and all sixteen Action Runtime messages have
individual full-message entry points as well as family unions. The
[Deckr message catalog](deckr-message-catalog.md) lists their payload locations and direction.

## Offline resolution

1. Read the manifest from a pinned bundle.
2. Load every schema entry from its relative `path` and register it by `$id`.
3. Resolve `$ref` from this registry. Do not depend on network fetching.
4. Select the complete schema for the input and validate it using Draft 2020-12.
5. Apply the profile, identity and authority checks in the specifications.

Schema URLs form a stable identifier namespace. The source tree and a download
of the complete bundle contain every reference needed for validation. A consumer
that vendors only Beacon needs `protocols/beacon` and `protocols/common`; Concord
needs `protocols/concord` and `protocols/common`. Neither requires Deckr or the
other protocol. Deckr consumers must retain the transitive dependency closure.

The validator must implement `$ref` siblings, `if`/`then`, `contains`, and Draft
2020-12 composition. Enable `date-time` format assertion explicitly: some
validators treat `format` as an annotation or omit its checker without an optional
dependency. CI verifies that the checker is installed and rejects bad dates.
Base64 content annotations do not prove image validity; consumers decode and
validate images separately.

## Values, defaults and extensions

- Wire field names are camelCase. Language-specific input aliases are not wire names.
- JSON values must be finite and serializable. Reject duplicate object keys and
  non-JSON values before schema validation.
- `default` is documentation, not a mutation instruction. Validation must not
  invent UUIDs, current timestamps, sessions or authority. A field is mandatory
  only when required by the selected schema, including its composed constraints.
- Null and absence are distinct. Follow the schema at each position. In
  particular, service-use contracts prohibit both `terms` and `termsHash`, even
  when their values would be null.
- Identifiers are emitted unpadded. Validation does not coerce values or trim text.
- Most typed objects reject unknown fields. Generic JSON payload positions are
  open by design. Protected service-view records share the root with their
  service payload, so their generic schema is open; the application controls
  payload extensibility. Availability retains its existing open payload policy.
- Capability selectors are descriptor-driven. A hardware message's capability ID
  is local to a descriptor, not a globally resolvable schema name. Validate its
  value/params against the selected descriptor and command/event contract too.

## Composition and ownership

Protocol records own their structural fields and expose `payload` (Beacon) or
`terms` (Concord) as application-defined JSON objects. Application schemas use
`allOf` and `$ref` to constrain those existing fields and fix feature/profile
identifiers. They do not copy or modify the protocol definitions.

Shared reusable objects have their own files and stable IDs. Small private enums
can remain in `$defs`. An application's profile must not be added to a union in
the generic Beacon or Concord schema; new profiles must not require a protocol
release. There is no catch-all 'all protocols' message envelope.

## Versioning and acceptance

The v1 path is the wire-major family. `bundleVersion` identifies a reference
release. Pin a Git revision or release asset; a branch URL is mutable. Published
stable releases must be immutable. The current reference release is alpha.

For each new schema, add a positive fixture and a negative fixture with an
explicit `schemaPath` and `valid` result in the manifest. The unconstrained JSON
value schema has no invalid JSON instance; its exception is explicit. Add targeted
cases for conditional fields and bounds, not only wrong top-level types.

Semantic fixtures distinguish `schemaValid` from `semanticValid`. CI checks the
structural expectation; each consuming implementation must execute semantic
rules and the distributed scenarios in [Deckr conformance scenarios](../deckr-conformance-scenarios.md).
The manifest marks semantic vector documents with `semantic: true`.
A case can contain inline `context` or name a reusable context with `contextRef`.
The document's `contexts` map supplies those named test inputs.
An optional `contextSchemas` map assigns schemas to fields in those contexts.
CI validates those fields when present; it does not evaluate runtime authority
or the expected semantic result.

The [project repository map](../README.md#project-repositories) identifies the
implementations and the cross-language integration harness. Their tests consume
these reference artifacts. Test coverage and results belong in those repositories.

Standards references: [schema identifiers and modular composition](https://json-schema.org/understanding-json-schema/structuring),
[annotations and defaults](https://json-schema.org/understanding-json-schema/reference/annotations),
[object composition](https://json-schema.org/understanding-json-schema/reference/object).
