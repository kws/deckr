# Deckr protocol and schema reference

This repository is the language-neutral source of truth for Beacon, Concord,
and the Deckr application contracts. It contains authoritative JSON Schemas,
normative specifications, examples, and conformance vectors. It does not contain
or publish a Python, TypeScript, or Rust runtime, SDK, launcher, or command-line tool.

The contracts are alpha. The current reference release is `1.0.0-alpha.1` for
wire protocol v1. See the [migration guide](docs/migration-guide.md) for intentional
changes from the former implementation-derived bundle.

## Start here

- [Schema consumption and validation](docs/schemas.md): choose an entry point,
  resolve references offline, and separate structural validation from authority.
- [Schema index](contract/v1/index.html) and [machine-readable manifest](contract/v1/manifest.json).
- [Message catalog](docs/message-catalog.md): every supported hardware and service message.
- [Extraction coverage](docs/schema-coverage.md): retained definitions and retired contracts.
- [Semantic rules](docs/semantic-rules.md) and [interoperability scenarios](interoperability-tests.md).

## Contract boundaries

| Module | Responsibility | Dependencies |
| --- | --- | --- |
| Protocol common | Neutral endpoint syntax, nonempty text, JSON values | None |
| Beacon | Discovery advertisement records | Protocol common |
| Concord | Agreements, participant tokens, pointers, maintenance records | Protocol common |
| Deckr messaging | Deckr endpoint policy, envelope, targets and subjects | Protocol common, Beacon, Concord |
| Deckr services | Service advertisements, declarations, service-use contracts and protected views | Deckr messaging, Beacon, Concord |
| Deckr hardware | Descriptors, capability values/commands, inventory, claims and hardware messages | Deckr messaging, Beacon, Concord |
| Deckr Action Runtime | Action/binding/page/settings payloads, service messages and availability views | Deckr services, Deckr hardware |

Beacon and Concord can be implemented without Deckr. Neither protocol schema
imports Deckr schemas or enumerates Deckr endpoint families. Applications own
their profile payloads and compose them with the generic records. Historic
`dev.deckr.beacon.*` and `dev.deckr.concord.*` wire labels are retained identifiers,
not application dependencies.

## Repository layout

```text
contract/v1/
  manifest.json       Schema IDs, module ownership, wire IDs and fixture expectations
  index.html          Static browsing index
  schemas/
    protocols/        common, beacon, concord
    deckr/            messaging, services, hardware, action-runtime
  catalogs/           Message registry, service protocol declaration, extraction coverage
  fixtures/           Valid and invalid JSON examples
  vectors/            Keys, hashes, NATS mapping and semantic cases
docs/                  Normative specifications and consumption guidance
interoperability-tests.md
.github/workflows/      Reference validation only
```

## Specifications

- [Beacon](docs/beacon.md): application-independent discovery.
- [Concord](docs/concord.md): application-independent agreement authority.
- [Deckr profiles](docs/deckr-profiles.md): hardware claims, services and Action Runtime.
- [Deckr message architecture](docs/runtime-architecture.md): envelopes, routing and lane rules.
- [NATS binding](docs/nats.md): lane subjects, KV keys, store policies and recovery.
- [Capabilities](docs/capabilities.md) and [identifier ownership](docs/namespaces.md).
- [Deployment responsibilities](docs/runtime-modes.md).

## Changing the reference

Edit JSON Schemas directly. They are source documents; do not regenerate them
from runtime type definitions. Update the manifest, examples, specifications and vectors
in the same change. Reuse public definitions with `$ref`; application modules
must not become dependencies of protocol modules.

CI validates schema syntax, offline reference resolution, dependency boundaries,
every declared fixture, coverage, and local documentation links. It installs
generic JSON Schema tooling and does not import an SDK. Run the same validation
step from [the workflow](.github/workflows/reference-validation.yml) locally with
Python 3.13 and `uv` when needed. Semantic and distributed lifecycle vectors must
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

The former implementations are available in Git history at commit
`1dae18252aa1ddfd52e1f3e6f0655d75ae9673b6`. They are historical evidence, not the
current specification. All reference material remains under [MIT](LICENSE).
