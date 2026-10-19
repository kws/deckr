# Deckr identifier policy v1 specification

## 1 Purpose and scope

This specification defines identifier ownership and naming for Deckr v1 application contracts.
Public contracts can cross package, process, host, language, and organization boundaries.
Owner namespaces prevent name conflicts between independently installed packages.

Generic Beacon and Concord endpoint families remain application-owned.
Deckr adds its own application naming rules.
These rules do not restrict endpoint families in the generic protocol schemas.

## 2 Identifier categories

Each identifier has a scope and an owner.
Short infrastructure names and local addresses must not replace globally owned contract identifiers.

| Category | Scope | Ownership | Syntax or format | Example |
| --- | --- | --- | --- | --- |
| Public contract label | Contract shared across packages and installations. | The contract owner. | Owner namespace and contract name. Dotted names are preferred. | `com.example.media.service` |
| Schema document ID | Schema reference and resolution. | The schema publisher. | Absolute URI. | [Capability descriptor schema ID](../contract/v1/schemas/deckr/hardware/capability-descriptor.v1.schema.json) |
| Deployment-local ID | One installation. | The installation selects the local address or ID. | The applicable contract defines the field syntax. | `sonos-home` |
| Core infrastructure name | Deckr lanes, endpoint families, and broker infrastructure. | Deckr core. | The fixed name defined by the contract. | `hardware_messages` |
| Private KV bucket name | Package-owned storage. | The package. | Owner-qualified, purpose-specific, versioned name with underscores. | `com_example_media_cache_v1` |

The following sections give the rules for each category.
The applicable schema defines structural field constraints.
Structural validation does not establish namespace ownership or runtime authority.

## 3 Public contract names

### 3.1 Contract categories

Public namespace rules apply to these identifiers:

- Action provider IDs
- Action IDs
- Service namespaces
- Beacon feature IDs and payload profile IDs
- Concord profile IDs
- Extension lane names
- Capability families
- Wire schema labels for contract payloads.

Private bucket names also identify an owner.
They use the storage format in [section 8](#8-private-storage).
Schema document IDs use the URI format in [section 4](#4-schema-identifiers).

### 3.2 Owner namespaces

Official Deckr contract labels use `dev.deckr.*`.
Only Deckr core and official Deckr packages can define labels in that namespace.
Third-party packages, deployment-specific packages, and personal plugins must not use it for their own contracts.

Third-party integrations own their service feature IDs, Beacon payload profiles, Concord profiles, and application schemas outside the Deckr namespace.

| Owner condition | Naming rule or recommendation | Examples |
| --- | --- | --- |
| Deckr core or an official Deckr package | Public contract labels use `dev.deckr.*`. | `dev.deckr.profile.hardware.v1`, `dev.deckr.profile.hardware_claim.v1`, `dev.deckr.action_runtime.provider` |
| A package has a stable DNS name. | The package should use a reverse-DNS name. | `com.example.media.service`, `org.example.input.axis`, `dev.example.deckr_plugins.status_panel` |
| A package has no DNS name. | The package should use a stable name qualified by its code-hosting service and owner. | `io.github.example-org.media-service.service`, `io.gitlab.example-group.panel-tools.action.status` |

Use lowercase owner labels where possible.
Hyphens are permitted in DNS owner labels, such as `com.k-si.*`, when they match the owner's DNS name.
Prefer dotted names for public contracts.

Do not use short public names without an owner namespace.
Examples include `sonos`, `openhab`, `media`, `home`, `button`, `service`, `events`, `cache`, and `state`.
Section 6 lists the separate rules for core infrastructure names.

## 4 Schema identifiers

Schema document IDs and wire schema labels have different formats.
The [schema guide](schema-guide.md#authority-and-dialect) defines their mapping in the manifest.

| Identifier | Location | Format |
| --- | --- | --- |
| Schema document ID | The schema's `$id` and the manifest schema entry's `id` and `schemaId`. | An absolute URI. |
| Wire schema label | A wire field or the manifest's `wireSchemaId`, where present. | An owner-qualified label, such as `dev.deckr.hardware.capability_descriptor.v1`. |

For example, the capability descriptor schema has this document ID:

```text
https://kws.github.io/deckr/contract/v1/schemas/deckr/hardware/capability-descriptor.v1.schema.json
```

Its wire schema label is `dev.deckr.hardware.capability_descriptor.v1`.
Do not replace a wire `schema` or `profile` value with a schema document URL.
Deckr core capability wire schema labels use `dev.deckr.*`.
Extension capability wire schema labels must use the extension owner's namespace outside `dev.deckr.*`.

## 5 Deployment-local identifiers

### 5.1 Local IDs and endpoint addresses

Deployment-local IDs belong to one installation.
They do not identify global API namespaces or establish package ownership.

| Local identifier | Examples or subjects |
| --- | --- |
| Service ID | `sonos-home`, `openhab-home`, `media-home` |
| Endpoint ID | `controller-main`, `clock-main`, `mirabox-main` |
| Action-provider instance ID | `clock-main` |
| Other local IDs | Hardware managers, controllers, pages, bindings, and devices. |

An endpoint address combines a core family and a local endpoint ID as `<family>:<id>`.
Examples are `service:sonos-home` and `action_provider:clock-main`.
The family is a Deckr protocol term; the ID is a configured local address.
The [Deckr messaging specification](deckr-messaging.md#endpoint-identity) defines endpoint constraints.

Do not use deployment-local IDs as these public identifiers:

- Provider IDs
- Service namespaces
- Action IDs
- Capability families
- Schema IDs.

### 5.2 Service names

A service namespace identifies the service package's public API and state contract.
A service ID identifies a local service endpoint.

| Service identifier | Meaning | Example |
| --- | --- | --- |
| Service namespace | The contract owned by the integration package. | `org.example.sonos.service`, `org.example.openhab.service` |
| Service ID | The deployment-local endpoint ID. | `sonos-home`, `media-home` |

An integration should use its package's namespace.
Its service contract is a Deckr core contract only if Deckr owns and publishes the service package.
A service ID is not a package name, namespace, runtime name, external host name, or provider ID.

Service packages should use their namespace as a Beacon `featureId` or within their own profile payload.
Each service protocol defines its catalog, status keys, and view keys.
Section 9 gives the related service validation and lifecycle requirements.

## 6 Core infrastructure names

Core infrastructure names remain short.
They are not owner-qualified public contract labels.

| Infrastructure | Names | Purpose or reference |
| --- | --- | --- |
| Core lanes | `hardware_messages`, `services` | [Deckr messaging lane registry](deckr-messaging.md#core-lane-registry). |
| Core endpoint families | `controller`, `action_provider`, `hardware_manager`, `service` | [Deckr messaging endpoint identity](deckr-messaging.md#endpoint-identity). |
| Core NATS subject root | `deckr.msg.*` | [Deckr NATS binding subjects](deckr-nats-binding.md#52-direct-and-broadcast-subjects). |
| Beacon advertisement bucket | `deckr_beacon_advertisement_v1` | Advertisements with a store TTL. |
| Concord contract bucket | `deckr_concord_contract_v1` | Persistent contracts. |
| Concord participant-token bucket | `deckr_concord_token_v1` | Participant tokens with a store TTL. |

The [Deckr NATS binding](deckr-nats-binding.md#protocol-store-bindings) lists the protocol store bindings, including the maintenance store.
These short names do not make `deckr.*` a general public contract namespace.
New Deckr-owned public contract labels use `dev.deckr.*`.

## 7 Extension contracts

Extension lanes and capability families use public owner namespaces.
The following rules apply:

| Contract category | Requirement | Examples |
| --- | --- | --- |
| Extension lane | The name must be a globally owned dotted identifier. Short names are not valid v1 extension lane names. | `com.example.metrics.events`, `org.example.media.events`, `io.github.example-org.media-service.events` |
| Core capability family | The name uses `dev.deckr.*`. | `dev.deckr.input.button`, `dev.deckr.output.raster` |
| Extension capability family | The name must use an owner namespace outside `dev.deckr.*`. | `com.example.input.axis` |

Deckr can bind and route an extension capability through its descriptor.
The package that owns the namespace defines the extension's meaning and behavior.
The [Deckr hardware capabilities specification](deckr-hardware-capabilities.md#descriptor-driven-extensibility) gives the descriptor requirements.

## 8 Private storage

### 8.1 Permitted use

A private KV bucket is permitted only if at least one condition is true:

- The package needs durable state outside the shared protocols.
- The package owns a documented projection, such as controller configuration.

Retained service views and durable service state are package-owned.
They must use an owner-qualified private bucket or a service-owned Concord profile.
Service packages can define their own Beacon payloads and service-view buckets directly backed by KV stores under their own namespace.

### 8.2 Bucket names and boundaries

Private bucket names must meet all these requirements:

| Property | Requirement |
| --- | --- |
| Ownership | The name identifies the package owner. |
| Purpose | The name identifies the storage purpose. |
| Version | The name includes a version. |
| Syntax | The name is valid for a JetStream bucket. Use underscores rather than dots. |

Examples are `com_example_media_cache_v1`, `io_github_example_org_media_service_cache_v1`, and `dev_deckr_controller_config_v1`.
Do not use generic names such as `state`, `cache`, `services`, `views`, or `config`.

Private buckets must not redefine these contracts:

- Beacon advertisements
- Concord contract validity
- Deckr-owned hardware profile semantics
- Deckr-owned action profile semantics.

## 9 Related service requirements

The following specifications define service behavior beyond identifier naming.
Their requirements apply to the named records and operations.

| Subject | Governing reference |
| --- | --- |
| Generic Beacon advertisements and Concord agreements owned by service packages | [Deckr services: Service package ownership](deckr-services.md#service-package-ownership). |
| Protocol-envelope validation followed by application payload checks | [Schema guide: Offline resolution](schema-guide.md#offline-resolution). |
| Service declarations, operations, and payload, result, and view validation | [Semantic validation rules: Services](semantic-rules.md#services). |
| Termless service-use contracts, exact participants, tokens, message pointers, and retained resource scope | [Deckr services: Service use](deckr-services.md#service-use). |
| Beacon discovery after agreement and Concord agreement validity and withdrawal | [Concord: Authority and discovery](concord.md#3-authority-and-discovery). |

## 10 Identifier consistency

Each public contract has one identifier.
Use its canonical namespace consistently in contracts, schemas, fixtures, vectors, examples, and consumers.
