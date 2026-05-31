# Public Identifier Namespaces

> Live implementation reference: this document describes behavior currently
> implemented in `deckr`. It should stay in sync with code, tests, generated
> schemas, examples, and configuration samples. If it differs from the
> implementation, treat that as a bug: either update the document to match
> current behavior or make an intentional code/schema/test change to match the
> intended v1 contract.

Deckr uses globally owned public identifiers for contracts that can cross
package, process, host, language, or organization boundaries. Short local names
are still used for Deckr infrastructure roots and deployment-local addresses,
but they must not be overloaded as globally owned contract identities.

The goal is simple: two independently installed packages should be able to share
one Deckr runtime and one NATS broker without accidentally colliding because
both chose names such as `service`, `media`, `openhab`, `button`, or `cache`.

## Contract Identifiers

The namespace rules apply to public Deckr contract identifiers, including:

- component ids
- component config source ids
- component instance source ids
- Python action provider ids and `deckr.plugins` entry point names
- action ids
- service namespaces
- extension lane names
- capability families
- schema ids for contract payloads
- package-owned private KV bucket names

Official Deckr contract identifiers use the owned `dev.deckr.*` namespace. For
example:

- `dev.deckr.controller`
- `dev.deckr.action_provider_runtime.python`
- `dev.deckr.action_provider_runtime.python.installed_providers`
- `dev.deckr.clock`
- `dev.deckr.clock.action.digital`
- `dev.deckr.input.button`
- `dev.deckr.output.raster`

Only Deckr core and official Deckr packages may define `dev.deckr.*`
identifiers. Third-party packages, deployment-specific packages, and personal
plugins must not squat under `dev.deckr.*`.

Kaj-owned examples in this workspace use `com.k-si.deckr.*`, such as
`com.k-si.deckr.kaj` and `com.k-si.deckr.kaj.action.album_browser`. Those are not
Deckr-owned identifiers; they are examples of a package owner using its own
namespace. Sonos, OpenHAB, and similar integrations likewise own their service
feature ids, Beacon payload profiles, Concord term profiles, operation names,
and schemas outside Deckr core.

## Owner Names

Packages with a stable DNS name should use reverse-DNS style identifiers:

- `com.example.media.service`
- `org.example.input.axis`
- `dev.example.deckr_plugins.status_panel`

Packages without a DNS name should use a stable forge-qualified style:

- `io.github.example-org.media-service.service`
- `io.github.example-org.media-service.events`
- `io.gitlab.example-group.panel-tools.action.status`

Use lowercase owner labels where possible. DNS labels may contain hyphens, so
hyphenated owners such as `com.k-si.*` are valid when they match the owner's
real name. Prefer dotted identifiers for public contracts. Do not use short
unqualified public names such as `sonos`, `openhab`, `media`, `home`, `button`,
`service`, `events`, `cache`, or `state`.

## Infrastructure Roots That Stay Short

These names are Deckr infrastructure roots, not owner-qualified public contract
ids. They intentionally stay short:

- Python import/package roots, such as `deckr` and `deckr.plugins.*`
- Python entry point group names, such as `deckr.components`, `deckr.plugins`,
  `deckr.config_sources`, and `deckr.component_instance_sources`
- the TOML root, `[deckr.*]`
- core lane names, currently `actions`, `hardware_messages`, and `services`
- core endpoint families, such as `controller`, `action_provider`,
  `hardware_manager`, and `service`
- core NATS subject roots and shared buckets, such as `deckr.lane.*`,
  `deckr_beacon_advertisement_v1`, `deckr_concord_contract_v1`, and
  `deckr_concord_token_v1`

Keeping those roots short does not make `deckr.*` a general-purpose public
contract namespace. New Deckr-owned public contracts use `dev.deckr.*`.

## Deployment-Local Identifiers

Deployment-local identifiers are addresses chosen by one installation. They are
not globally owned API namespaces and they do not imply package ownership.

Examples include:

- service ids such as `sonos-home`, `openhab-home`, or `media-home`
- endpoint ids such as `controller-main`, `python-dev.deckr.clock`, or
  `mirabox-main`
- component instance table names such as `sonos_home` or `clock_actions`
- component `instance_id` and diagnostic `runtime_name` values
- hardware manager ids, controller ids, page ids, binding ids, and device ids

Endpoint addresses combine a short core endpoint family with a deployment-local
id, such as `service:sonos-home` or `action_provider:python-dev.deckr.clock`.
The family names are Deckr protocol terms. The id portion is a configured local
address.

Do not use deployment-local ids as component ids, provider ids, service
namespaces, action ids, capability families, or schema ids.

## Service Namespaces And Service Ids

A service namespace is the globally named API and state contract owned by the
service package. A Sonos or OpenHAB integration should use the namespace owned
by that integration package, for example `org.example.sonos.service` or
`org.example.openhab.service`. These names are not Deckr core contracts unless
Deckr itself owns and publishes that service package.

A service id is a deployment-local endpoint id, such as `sonos-home` or
`media-home`. A service id is not a package name, component id, namespace,
runtime name, external host name, or provider id.

Under Beacon/Concord, service packages advertise those namespaces with generic
Beacon advertisements and, when needed, bind live service-specific agreements
with generic Concord contracts. Deckr core validates the Beacon/Concord
envelopes; the service package validates its payloads, terms, operations, and
result/view schemas. After a service-specific Concord agreement is negotiated,
Beacon remains discovery input for future negotiations only; agreement validity
and withdrawal are Concord concerns.

Service packages should use their namespace as a Beacon `featureId` or inside
their own profile payload. If a service needs retained views or durable domain
state, that storage is package-owned and must use an owner-qualified private
bucket or a service-owned Concord profile. Deckr core no longer defines shared
service catalog, status, or view keys.

## Extension Lanes

Core lane names are short because they are owned by Deckr itself. Extension
lanes must use globally owned dotted identifiers, such as:

- `com.example.metrics.events`
- `org.example.media.events`
- `io.github.example-org.media-service.events`

Short extension lane names are not valid v1 Deckr contracts.

## Capability Families And Schema Ids

Deckr core capability families and their core schema ids use `dev.deckr.*`.
Extension capability families and extension schema ids must use an owner
namespace outside `dev.deckr.*`.

For example, a Deckr core button family is:

```text
dev.deckr.input.button
```

An extension family might be:

```text
com.example.input.axis
```

Deckr may bind and route an extension capability generically from its
descriptor, but extension semantics belong to the package that owns the
extension namespace.

## Private KV Buckets

The shared protocol buckets are:

- `deckr_beacon_advertisement_v1` for TTL-bound Beacon advertisements
- `deckr_concord_contract_v1` for persistent Concord contracts
- `deckr_concord_token_v1` for TTL-bound Concord participant tokens

Package-owned private buckets are allowed only when a package needs durable
state outside those shared protocols, or when it owns a documented
package-specific projection such as controller config.

Private bucket names must be owner-qualified, purpose-specific, versioned, and
safe for JetStream bucket names. Use underscores for bucket names rather than
dots:

- `com_example_media_cache_v1`
- `io_github_example_org_media_service_cache_v1`
- `dev_deckr_controller_config_v1`

Do not use generic private bucket names such as `state`, `cache`, `services`,
`views`, or `config`.

Package-owned private buckets must not redefine Beacon advertisements, Concord
contract validity, or Deckr-owned hardware/action profile semantics. Service
packages may define their own Beacon payloads, Concord terms, and private state
under their own public namespace.

## Pre-V1 Contract Rule

This is a pre-v1 contract rule, not a migration guideline. When an identifier
uses the wrong namespace, rename it to the canonical namespace and update every
consumer, test, schema, example, and config sample. Do not add old/new aliases,
fallback parsing, compatibility shims, or duplicate public names.
