# Deckr contract-first reference

This repository defines the contracts for Beacon, Concord, and the Deckr
application protocols. The authoritative deliverables are language-neutral JSON
Schemas, protocol specifications, message catalogs, and conformance artifacts.

Define and review contracts here before consumers implement them. Author schemas
directly in `contract/v1/schemas/` and validate them with the repository's CI
workflow.

The contracts are alpha. The current bundle version is `1.0.0-alpha.1` for
wire protocol v1.

## Start here

- [Schema guide](docs/schema-guide.md): choose an entry point,
  resolve references offline, and separate structural validation from authority.
- [Schema index](contract/v1/index.html) and [machine-readable manifest](contract/v1/manifest.json).
- [Deckr message catalog](docs/deckr-message-catalog.md): every supported hardware and service message.
- [Semantic validation rules](docs/semantic-rules.md) and [Deckr conformance scenarios](deckr-conformance-scenarios.md).

## Contract boundaries

| Module | Responsibility | Direct schema dependencies |
| --- | --- | --- |
| Protocol common | Neutral endpoint syntax, nonempty text, JSON values | None |
| Beacon | Discovery advertisement records | Protocol common |
| Concord | Agreements, participant tokens, pointers, maintenance records | Protocol common |
| Deckr messaging | Deckr endpoint policy, envelope, targets and subjects | Protocol common, Beacon, Concord |
| Deckr services | Service advertisements, declarations, service-use contracts and protected views | Protocol common, Concord, Deckr messaging |
| Deckr hardware | Descriptors, capability values/commands, inventory, claims and hardware messages | Protocol common, Concord, Deckr messaging |
| Deckr Action Runtime | Action/binding/page/settings payloads, service messages and availability views | Protocol common, Deckr messaging, Deckr services, Deckr hardware |

Beacon and Concord can be implemented without Deckr. Neither protocol schema
imports Deckr schemas or enumerates Deckr endpoint families. Applications own
their profile payloads and compose them with the generic records. The
`dev.deckr.beacon.*` and `dev.deckr.concord.*` wire labels identify protocol
records independently of the application using them.

## Repository layout

```text
contract/v1/
  manifest.json       Schema IDs, module ownership, wire IDs and fixture expectations
  index.html          Static browsing index
  schemas/
    protocols/        common, beacon, concord
    deckr/            messaging, services, hardware, action-runtime
  catalogs/           Message registry and service protocol declaration
  fixtures/           Valid and invalid JSON examples
  vectors/            Keys, hashes, NATS mapping and semantic cases
docs/                  Normative specifications and consumption guidance
deckr-conformance-scenarios.md
.github/workflows/      Reference validation only
```

## Specifications

- [Beacon](docs/beacon.md): application-independent discovery.
- [Concord](docs/concord.md): application-independent agreement authority.
- [Concord terms hashing](docs/concord-terms-hashing.md): canonical JSON encoding and terms digests.
- [Deckr hardware](docs/deckr-hardware.md): discovery, device claims and manager lifecycles.
- [Deckr services](docs/deckr-services.md): service-use agreements, protected views and service lifecycles.
- [Deckr Action Runtime](docs/deckr-action-runtime.md): service identity, availability and runtime messages.
- [Deckr messaging](docs/deckr-messaging.md): envelopes, routing, lane rules and shared lifecycle requirements.
- [Deckr NATS binding](docs/deckr-nats-binding.md): lane subjects, KV keys, store policies and recovery.
- [Deckr hardware capabilities](docs/deckr-hardware-capabilities.md): descriptors, values and commands.
- [Deckr identifier policy](docs/deckr-identifiers.md): public ownership, local identifiers and storage names.
- [Deckr deployment guide](docs/deckr-deployment.md): deployment patterns and responsibilities.

## Contract-first workflow

1. Specify the wire shape, ownership and observable behavior here before changing
   a consuming implementation. Edit schemas directly and reuse definitions with
   `$ref`.
2. Update the normative specifications, catalogs, manifest, examples and
   conformance vectors together. Assess compatibility and version the contract.
3. Validate and review the complete reference change, then publish a pinned
   commit or immutable bundle release.
4. Update implementations in their own repositories against that pinned contract,
   and run the applicable structural, semantic and lifecycle conformance checks.

Schemas define JSON structure; specifications define additional behavior and
authority rules. Examples and vectors demonstrate those requirements. An
implementation cannot redefine the contract through its types or behavior.
Resolve reference inconsistencies here through a reviewed contract change.

CI validates schema syntax, offline reference resolution, dependency boundaries,
every declared fixture, coverage, and local documentation links using pinned
JSON Schema tooling. Run the same validation step from
[the workflow](.github/workflows/reference-validation.yml) locally with Python
3.13 and `uv` when needed. Semantic and distributed lifecycle vectors must
also be exercised by each consuming implementation; passing repository CI is not
a claim of runtime protocol conformance.

## Releases and reuse

Pin a Git commit or an immutable `contracts-v<bundleVersion>` release. The bundle
version lives in `contract/v1/manifest.json`; it is independent of the wire-major
version. Publish the complete `contract/v1` directory as a release asset or static
reference site. A new incompatible wire contract gets a new wire-major/schema ID;
do not silently change a stable schema under an immutable release identifier.

Schema IDs use `https://kws.github.io/deckr/contract/v1/schemas/` as their canonical
namespace. These are identifiers, not a promise of a currently deployed website.
The complete bundle supports offline resolution. Consumers can vendor its needed
dependency closure or generate language types in their own repositories.

All reference material is available under [MIT](LICENSE).
