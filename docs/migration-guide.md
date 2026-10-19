# Migration to the reference-only repository

## Source baseline

The extraction baseline is commit `1dae18252aa1ddfd52e1f3e6f0655d75ae9673b6`.
The former Python, TypeScript and Rust sources, runtime tests and package build
files are retained in Git history. This repository now contains only reference
contracts, documentation, fixtures, vectors and generic validation configuration.

The JSON Schemas are now authored directly. Do not regenerate them from the
historic Pydantic classes: doing so would erase validation constraints and newly
extracted payloads.

## Consumer changes

1. Pin the reference bundle by commit or release version.
2. Load schema files through the manifest and register their absolute `$id`s.
3. Replace old file-path assumptions with the module paths in the manifest.
4. Choose complete application schemas where payload validation is needed.
5. Implement the [semantic rules](semantic-rules.md) and run the conformance
   scenarios against your runtime. Code generation alone is not conformance.

`wireSchemaId` preserves existing reverse-DNS identifiers. Schema document IDs
are now absolute HTTPS URIs. No historic Python version determines reference
release versions; `bundleVersion` in the manifest does.

## Intentional alpha corrections

- Generic Beacon/Concord endpoint syntax is application-neutral. Deckr endpoint
  family/reserved-identity policy is enforced by Deckr wrappers. This broadens
  generic protocol acceptance without broadening the Deckr application schemas.
- Bounds, nonempty identities, uniqueness and conditional payload rules formerly
  enforced only by model validators are now structural schema assertions.
- Wire validation does not trim strings, accept Python field aliases, or coerce
  numbers/booleans from strings. Producers emit canonical JSON field/value types.
- Service broadcasts are rejected, matching the core lane policy. Hardware
  broadcasts remain limited to the controllers scope.
- Complete Action Runtime schemas validate namespace, name, direction, intent,
  one-way exchange, payload position, payload structure and session targeting.
- Service-use contracts forbid `terms` and `termsHash`, including null values.
- Protected view schemas include the actual flat storage fields, notably
  `serviceSessionId`, `consumerSessionId`, `contractId`, `generation` and `viewKey`.
  Older prose mentioning only `sessionId` did not describe the stored record.
- Timestamps are checked with enabled RFC 3339 date-time validation. JSON Schema
  defaults are not constructor calls or permission to invent missing identity.
- Messages require a nonempty `messageId` and a `createdAt` timestamp on the wire.
  Earlier exported schemas marked these fields optional because constructor
  factories supplied them. Receivers must not generate a replacement ID or time.

## Retired surfaces

| Former surface | Reference decision |
| --- | --- |
| `actions` lane and camelCase action message discriminators | Retired. The 14 current snake_case Action Runtime operations use the `services` lane. Shared payload models are retained. |
| `actionExtension` / `ActionExtensionBody` | Retired with the old lane. Owner-defined service protocols provide extension payloads. |
| `ActionsBeaconPayload`, `ActionBeaconDescriptor`, `RegisteredAction`, `ActionProviderSessionTerms` from older ports | Replaced by Action Runtime service advertisements, termless service-use contracts, action descriptors and availability views. |
| `DynamicPageRoleDescriptor`, `DynamicPageTemplateDescriptor` from older TypeScript | Retired templates. Current dynamic-page commands specify concrete child bindings and `self` / `action` targets. |
| `ServiceUseTerms`, `ServiceUseScopeIndexRecord`, service scope/index states in older Rust/TypeScript | Retired. Current service-use authority is an opaque termless Concord contract with exact participants and sessions. |
| `SettingsSnapshot` from TypeScript | Retained as a reusable settings payload, alongside Python-derived settings definitions. It does not introduce a new core lane operation. |
| Component manifests/config loaders, local handles, views/watch wrappers, service client/context objects, reaper configuration | Runtime API/configuration, not independent wire records. Observable protocol behaviour remains in the specifications. |
| Python CLI and artifact generator; Python/Rust/TypeScript packages and runtime tests | Removed. Consumers implement SDKs and runtime conformance in their own repositories. |

There is one current contract for each supported message. Do not add compatibility
aliases or revive the retired authority mechanisms alongside it. Existing
implementations must deliberately migrate to the chosen reference release.
