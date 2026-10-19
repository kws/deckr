# Semantic validation rules

These rules complement JSON Schema. A structurally valid record can still be
invalid, stale, unauthorized, or inconsistent with another record. Implementations
must apply the applicable rules before acting. The
[semantic vectors](../contract/v1/vectors/semantic-validation.v1.json) contain
inputs and expected outcomes; the repository validator checks their structural
expectations, while consuming implementations execute the semantic checks.

## Protocol common

**SEM-JSON-001 — JSON input.** Parse UTF-8 JSON with unique object keys, Unicode
scalar strings, and finite numbers. Reject NaN, infinities, duplicate keys and
unpaired surrogates. Do not coerce runtime objects into JSON strings. Schema
validation starts after this check; `json-value` accepts every JSON value.

**SEM-KEY-001 — Key identity.** Encode key tokens using the NATS-safe algorithm in
[Concord section 4.2](concord.md#42-key-tokens). A record's identity must equal
the identity encoded in its exact store key. Apply the key vectors to Unicode,
reserved `b64_` prefixes and punctuation. A successful lookup is not an identity check.

## Beacon

**SEM-BCN-001 — Ownership and freshness.** Advertisement refresh and withdrawal
must match the held advertisement identity, endpoint/session and revision.
Refresh sequences strictly increase, and positive TTL values come from the
store's agreed TTL policy. Source currentness, expiry, session selection and
owner checks follow [Beacon](beacon.md). A record's date fields cannot prove liveness.

## Concord

**SEM-CCD-001 — Participant relationships.** `participants` and
`attachedParticipants` are sorted lexicographically by Unicode scalar value.
Both are unique; attached participants are a subset of participants, and every
`participantSessions` key names a participant. Sorting, subsets and map-key
membership are semantic checks; size/uniqueness are also schema assertions.

**SEM-CCD-002 — Terms hash.** If terms are present, their canonical JSON bytes
must hash to `termsHash`. If both the contract profile and `terms.profile` are
present, they must agree. Tokens must carry the expected terms hash. Follow
[canonical JSON](canonical-json.md) and the hash vectors; do not substitute an
arbitrary language serializer or a different canonicalization scheme.

**SEM-CCD-003 — Exact authority.** Validate the named contract ID and generation,
open state, complete participant attachment, participant token identities,
session assertions and current store observations. Each token must correspond
to its exact key and agreement generation. Apply the status/reason ordering in
[Concord section 12](concord.md#12-contract-validation). Local authority loss is
permanent for that lifecycle; later matching data does not revive it. No schema
or search for a different agreement can grant authority.

**SEM-CCD-004 — Mutations and maintenance.** Enforce immutable generation identity,
revision guards, bootstrap ownership, token refresh deadlines, cancellation,
successor creation, stale grace/retention periods, and durable cleanup markers.
An attached-participant bit alone does not substitute for a live token. The
protocol lifecycle scenarios remain in the [conformance catalog](../interoperability-tests.md).

## Hardware

**SEM-HW-001 — Inventory identity.** For a hardware advertisement, payload
`sessionId` equals envelope `sessionId`, and `managerEndpoint` equals envelope
`endpoint` and `hardware_manager:<managerId>`. Each device-map key equals
`deviceRef.deviceId`, each device ref names that manager, and descriptor IDs
and any supplied fingerprints agree with the ref.

**SEM-HW-002 — Capacity.** Counts are nonnegative. When total and available counts
are both present, `availableInstances = max(totalInstances - claimedInstances, 0)`.
Claim conflicts and capacity limits are evaluated over currently valid agreements,
not advertisements alone.

**SEM-HW-003 — Claim identity and scope.** Claimed device IDs are unique within
one claim. All device refs name the manager encoded by `managerEndpoint`.
The agreement participants are exactly the controller and manager named in its
terms. Check device ownership, session identity and capacity against current
state. Device disconnect/material descriptor changes must terminate or replace
the claim as specified in [Deckr profiles](deckr-profiles.md#hardware-claims).

**SEM-HW-004 — Descriptor relationships.** Within a device, connection IDs,
control IDs and device-level capability IDs are unique. Within each control,
capability IDs are unique across all capability collections. Source connection
refs resolve to a declared connection. Parent/related control refs resolve to
declared controls and must not reference themselves. A default status indicator
resolves to an output capability. Projection sources resolve to real capabilities
and must not refer to the projected capability itself. Array `uniqueItems` alone
cannot enforce uniqueness of one field across otherwise different objects.

**SEM-HW-005 — Constraints and values.** A capability constraint's minimum must
not exceed its maximum. Apply descriptor units, ranges, enumerated values and
device-specific limits to the selected capability value or command. The selected
family/type/event/command determines the payload schema; capability IDs alone do
not. Validate any embedded descriptor schemas themselves against their declared
JSON Schema dialect. Family, direction/access, positive scales, normalized geometry
and core event/command vocabulary are also asserted by the structural schemas.

**SEM-HW-006 — Raster data.** `set_frame` parameters must match the set-frame
schema, and `clear` uses an empty object. Decode base64 and verify the actual
image encoding and dimensions against the declared values and device capability.
An annotation such as `contentEncoding` is not a decoder. The general raster
params union does not determine the command type.

## Services

**SEM-SVC-001 — Advertisement agreement.** `serviceEndpoint` equals
`service:<serviceId>` and the advertised endpoint. Payload and advertisement
sessions agree. Feature ID, protocol namespace/version, advertisement profile
and service-use profile agree with the selected service protocol declaration.
The declaration's operations and messages are authoritative, not arbitrary
unverified advertisement strings.

**SEM-SVC-002 — Declaration relationships.** Every non-null message `operation`
names a declared operation. Consumer-to-service, bidirectional, command and query
messages require an operation. Each advertised message name, intent, exchange,
direction and payload schema agrees with the protocol. View-family prefixes,
writers and store names agree with the service declaration. Embedded schema
documents must be valid in their dialect. Structural declaration validation
cannot compare a supplied schema's meaning with the owner's published schema.

**SEM-SVC-003 — Service-use authority.** The termless agreement contains exactly
the service and its consumer. The message pointer must equal the held agreement;
sender, recipient, sessions, direction and declared operation must be authorized
by that agreement. Retained resource scope is enforced by the service protocol.
After agreement, loss of Beacon discovery alone does not terminate authority.

**SEM-SVC-004 — Protected view fences.** A stored view is a flat object containing
payload plus `viewKey`, `serviceId`, `serviceNamespace`, `serviceEndpoint`,
`serviceSessionId`, `consumerEndpoint`, `consumerSessionId`, `writer`, `contractId`
and `generation`. Compare all applicable values against the authorized view
context and active lease. The storage key is
`<viewKey>.contract.<encoded-contractId>.<generation>`. Revisions are store
metadata, not payload fields. A mismatching replacement record must not expose
its payload through an existing reader/watch. Follow [the NATS recovery rules](nats.md).

**SEM-SVC-005 — Payload and reply selection.** Use the message declaration to
select params/event/result/error schemas. Check request/reply correlation,
contract and session identity and expiry. Unsupported names, wrong directions or
unauthorized operations fail even if the generic service envelope validates.
Do not replay published commands under a different agreement after reconnect.

## Action Runtime

**SEM-ACT-001 — Runtime identity.** `serviceId` equals
`action-runtime.<providerInstanceId>`; the service endpoint equals
`service:<serviceId>`. The provider-instance ID matches its restricted token
syntax. Availability's service session agrees with the held service-use agreement.
Every non-null availability descriptor's action ID equals its entry's action ID.
An available entry must contain a descriptor (also a structural assertion).

**SEM-ACT-002 — Requirements and routing.** Action requirement names are unique.
Resolved capabilities and sources must exist in the negotiated descriptors and
match the declared requirement. Binding, action-instance and page-session IDs
are controller-owned and must resolve in the authorized session. Output and
overlay generations must match the controller's current binding generation;
nonnegative integers alone do not establish freshness. A direct runtime message
does not grant permission to invent a binding or cross a device/claim boundary.

**SEM-ACT-003 — Dynamic pages.** A dynamic page has at least one child binding,
with unique control IDs. `self` resolves to the page opener's current action
instance. `action` resolves through its action/provider selector. Providers must
not supply controller-owned live action-instance IDs for dynamic children.
Conditional target fields are asserted by the schema; selection and identity
ownership require controller state.

**SEM-ACT-004 — Settings identity.** Settings-target descriptions must mirror
the target's provider-instance ID, provider ID and optional action ID. Provider
settings omit action IDs; action-instance settings require action/action-instance
IDs. Stable target keys use the key-token algorithm and the scope-specific layout
in [settings key vectors](../contract/v1/vectors/settings-target-keys.v1.json).

## Testing obligations

Use both structurally invalid fixtures and structurally valid semantic failures.
Test identical outcomes from cached and exact observations, and test currentness,
session changes, expiry, cleanup and recovery with independent implementations.
The schemas contain no runtime clock, KV state, image decoder or authority engine.
