# Repository guidance

This is a contract-first, reference-only repository. Define contracts here before
implementing them in separate repositories. Read [README.md](README.md) and
[schema guidance](docs/schemas.md) before changing contracts.

- Maintain authoritative JSON Schema Draft 2020-12 documents directly.
- Keep all protocol schemas under `contract/v1/schemas/protocols/` independent
  of Deckr schemas. Beacon and Concord may depend on protocol-common definitions,
  but must not depend on one another or on application payloads.
- Put Deckr addressing policy, envelopes, profiles, lane messages and payloads
  under `contract/v1/schemas/deckr/`.
- Add reusable payload schemas and full-message/record entry points. Use `$ref`
  instead of duplicating shared definitions.
- Update manifest entries, positive/negative fixtures, catalogs, normative docs
  and applicable semantic vectors together. Keep fixture expectations explicit.
- Preserve semantics that JSON Schema cannot express in the normative specs and
  conformance vectors. Do not claim that structural validation grants authority.
- Do not add runtime/SDK implementations, language packages, generated clients,
  runtime tests, launchers or application configuration. Generic CI validation is
  the only executable repository tooling.
- Describe observable protocol behavior without depending on a language API,
  class, module, or local implementation file. Schema titles identify contracts.
- Document the current contracts. Omit migration history, extraction provenance,
  and retired implementation inventories. Local links must resolve to files in
  this reference checkout.
- Keep personal context and working preferences outside this repository.

The validation step in `.github/workflows/reference-validation.yml` is the
required check. Run it with the pinned tooling specified there. Consuming
repositories own their implementation builds and runtime conformance checks.
