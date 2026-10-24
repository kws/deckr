# Deckr contract-first reference

This repository defines the contracts for Beacon, Concord, and the Deckr
application protocols. The authoritative deliverables are language-neutral JSON
Schemas, protocol specifications, message catalogs, and conformance artifacts.

Define and review contracts here before consumers implement them. Author schemas
directly in `contract/v1/schemas/` and validate them with the repository's CI
workflow.

The project is in alpha development. The contracts have no release versions yet;
features and the standard are still being defined. The existing `v1` schema and
wire identifiers remain in use. Identify contract revisions by Git commit.

## Project repositories

Deckr separates contract definitions, language implementations, and integration
tests into independent repositories.

| Repository | Responsibility |
| --- | --- |
| [deckr](https://github.com/kws/deckr) (this repository) | Authoritative specifications, schemas, catalogs, fixtures, and conformance vectors and scenarios. |
| [deckr-core-python](https://github.com/kws/deckr-core-python) | The `deckr` Python package: reusable protocol, runtime, hardware, service, and Action Runtime support. |
| [deckr-core-rust](https://github.com/kws/deckr-core-rust) | The `deckr` Rust crate: reusable protocol, runtime, hardware, service, and Action Runtime support. |
| [deckr-core-typescript](https://github.com/kws/deckr-core-typescript) | The `@deckr/core` TypeScript package: reusable protocol, runtime, hardware, service, and Action Runtime support. |
| [deckr-integration-tests](https://github.com/kws/deckr-integration-tests) | Cross-language service tests with Docker Compose, recovery workloads, and a native Concord lifecycle runner. |

The three cores implement the same contracts. Their APIs, package versions, and
test coverage are independent. A shared specification pin does not establish
equal implementation coverage.

Application repositories own controller orchestration, routing, rendering,
configuration policy, device drivers, and action or integration behavior.
The cores supply reusable managed boundaries for those applications. The
integration harness supplies test applications; it is not a Deckr launcher.
See the [architecture proposal](docs/deckr-architecture.md) for these boundaries.

For development across repositories, check them out as siblings:

```text
workspace/
  deckr/
  deckr-core-python/
  deckr-core-rust/
  deckr-core-typescript/
  deckr-integration-tests/
```

Run commands from the relevant repository root. There is no shared root build or
release. Each implementation README gives its dependencies and validation steps.

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
- [Deckr Action Runtime rendering](docs/deckr-action-runtime-rendering.md): content formats, support discovery, output ordering, and render results.
- [KSI Invariant rendering extension](docs/deckr-render-invariant-extension.md): bounded graph input and image downloads.
- [Inky priority output extension](docs/deckr-inky-extension.md): gated e-paper updates.
- [Deckr messaging](docs/deckr-messaging.md): envelopes, routing, lane rules and shared lifecycle requirements.
- [Deckr NATS binding](docs/deckr-nats-binding.md): lane subjects, KV keys, store policies and recovery.
- [Deckr hardware capabilities](docs/deckr-hardware-capabilities.md): descriptors, values and commands.
- [Deckr identifier policy](docs/deckr-identifiers.md): public ownership, local identifiers and storage names.

## Informative guidance

- [Deckr architecture proposal for v1](docs/deckr-architecture.md): optional baseline architecture, component responsibilities, and lifecycle ownership. Informative, not normative.
- [Deckr deployment guide](docs/deckr-deployment.md): deployment patterns and responsibilities.

## Contract-first workflow

1. Specify the wire shape, ownership and observable behavior here before changing
   a consuming implementation. Edit schemas directly and reuse definitions with
   `$ref`.
2. Update the normative specifications, catalogs, manifest, examples and
   conformance vectors together. During alpha development, contracts can change
   without a release version or backward compatibility.
3. Validate and review the complete reference change, then share the reviewed
   Git commit for consumers to pin.
4. Update implementations in their own repositories against that pinned contract,
   and run the applicable structural, semantic and lifecycle conformance checks.
5. Update the integration harness's source lock to the reviewed commits and run
   the applicable cross-language workloads. Its three implementation pins must
   select the same specification revision.

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

Validation has three separate scopes:

| Scope | Owner and evidence |
| --- | --- |
| Reference validation | This repository checks schemas, fixtures, catalogs, vector structure, and documentation links. |
| Implementation validation | Each core tests its APIs and lifecycle behavior, and reads the reference artifacts for its implemented conformance checks. |
| Cross-language validation | The integration harness tests its declared workloads against real NATS. Its README states coverage and limits. |

Python and TypeScript record their CI specification commit in
`.deckr-spec-revision`; Rust uses `spec-revision`. The integration harness records
all four source commits in `sources.lock.json`. Update these files in their owning
repositories. Documentation links to `main` are navigation links; reproducible
validation uses the recorded commits.

## Reuse during alpha

Pin a Git commit. The manifest inventories the contracts; it does not assign a
release version. Do not add numbered alpha releases for feature changes.
The `v1` paths and wire labels identify the contracts under development and do
not promise backward compatibility. Coordinate contract changes with consumers
through their specification pins.

Use the complete `contract/v1` directory from the selected commit for offline
validation or a static reference site.

Schema IDs use `https://kws.github.io/deckr/contract/v1/schemas/` as their canonical
namespace. These are identifiers, not a promise of a currently deployed website.
The complete bundle supports offline resolution. Consumers can vendor its needed
dependency closure or generate language types in their own repositories.

All reference material is available under [MIT](LICENSE).
