# Repository guidance

This is a contract-first, reference-only repository. Define contracts here before
implementing them in separate repositories. Read [README.md](README.md) and
[schema guide](docs/schema-guide.md) before changing contracts.

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
- Keep repository content focused on schemas, catalogs, fixtures, vectors and
  documentation. Limit executable tooling to generic CI validation.
- Describe observable protocol behavior in language-neutral terms. Schema titles
  identify contracts.
- Document the current contracts and repository workflows. Local links must
  resolve to files in this reference checkout.
- Use lowercase, hyphenated document filenames that identify the subject.
  Use `deckr-` for Deckr subjects and the protocol name for protocol subjects.
  Document titles identify the purpose and, for specifications, the wire version.
  Use the same subject names in navigation links.
- Write all specification documentation in
  [ASD-STE100 Simplified Technical English, Issue 9](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf).
  Keep this writing requirement and its reference in AGENTS.md; do not include
  STE100 labels, compliance statements or language-reference notes in the documents.
- Keep personal context and working preferences outside this repository.

The validation step in `.github/workflows/reference-validation.yml` is the
required check. Run it with the pinned tooling specified there. Consuming
repositories own their implementation builds and runtime conformance checks.
