# Semantic validation rules v1 specification

## Purpose and scope

These rules supplement JSON Schema validation for Beacon, Concord, and Deckr v1.
A structurally valid record can still be invalid, stale, unauthorized, or inconsistent with another record.
Implementations must apply the applicable checks before they act.

Each rule has an ID.
The checks state requirements.
Notes explain the checks or the limits of structural validation.
References identify related contracts and acceptance artifacts.
Some semantic requirements are defined in this document only.

The [semantic vectors](../contract/v1/vectors/semantic-validation.v1.json) contain inputs and expected outcomes.
The repository validator checks their structural expectations.
Consuming implementations execute the semantic checks.

## Protocol common

### SEM-JSON-001: JSON input

**Checks**

- Parse the input as UTF-8 JSON.
- Require unique object keys.
- Require Unicode scalar strings.
- Require finite numbers.
- Reject NaN, infinities, duplicate keys, and unpaired surrogates.
- Do not coerce runtime objects into JSON strings.
- Start schema validation only after these input checks.

**Note:** The `json-value` schema accepts every JSON value.

**References:** [Schema guide: Values, defaults and extensions](schema-guide.md#values-defaults-and-extensions),
[JSON value schema](../contract/v1/schemas/protocols/common/json-value.v1.schema.json).

### SEM-KEY-001: Key identity

**Checks**

- Encode key tokens with the NATS-safe algorithm in Concord section 4.2.
- Check that each record's identity equals the identity encoded in its exact store key.
- Apply the key vectors to Unicode, reserved `b64_` prefixes, and punctuation.

**Note:** A successful lookup is not an identity check.

**References:** [Concord: Key tokens](concord.md#42-key-tokens),
[Key-token vectors](../contract/v1/vectors/key-tokens.v1.json).

## Beacon

### SEM-BCN-001: Ownership and freshness

**Checks**

- For refresh and withdrawal, check the advertisement identity against the held identity.
- Check the endpoint, session, and revision against the held values.
- Require strictly increasing refresh sequences.
- Require positive TTL values from the store's agreed TTL policy.
- Apply the Beacon requirements for source currentness, expiry, session selection, and owner checks.
- Do not use record date fields as proof of liveness.

**References:** [Beacon: Advertisement lifecycle](beacon.md#6-advertisement-lifecycle),
[Beacon: Candidate selection](beacon.md#7-candidate-selection),
[Beacon: Runtime view and watches](beacon.md#8-runtime-view-and-watches).

## Concord

### SEM-CCD-001: Participant relationships

**Checks**

- Require lexicographic order by Unicode scalar value in `participants`.
- Require lexicographic order by Unicode scalar value in `attachedParticipants`.
- Require unique entries in each list.
- Check that `attachedParticipants` is a subset of `participants`.
- Check that every `participantSessions` key names a participant.

**Note:** Sorting, subsets, and map-key membership require semantic checks.
The schema also checks size and uniqueness.

**References:** [Concord: Contract record](concord.md#61-contract-record),
[Concord: Participant sessions](concord.md#62-participant-sessions),
[Contract schema](../contract/v1/schemas/protocols/concord/contract.v1.schema.json).

### SEM-CCD-002: Terms hash

**Checks**

- If terms are present, check that their canonical JSON bytes hash to `termsHash`.
- If the contract profile and `terms.profile` are both present, require the same value.
- Require the expected terms hash in participant tokens.
- Apply the Concord terms hashing rules and hash vectors.
- Do not substitute an arbitrary language serializer or a different canonicalization scheme.

**References:** [Concord terms hashing](concord-terms-hashing.md),
[Concord terms-hash vectors](../contract/v1/vectors/concord-terms-hash.v1.json).

### SEM-CCD-003: Exact authority

**Checks**

- Validate the named contract ID and generation.
- Require the contract state to be `open`.
- Require complete participant attachment.
- Validate participant token identities.
- Validate session assertions.
- Validate current store observations.
- Check each token against its exact key and agreement generation.
- Apply the status and reason ordering in Concord section 12.
- Treat local authority loss as permanent for that lifecycle.
- Do not restore that authority when later data matches.
- Do not use schema validation to grant authority.
- Do not authorize through a search for a different agreement.

**References:** [Concord: Contract validation](concord.md#12-contract-validation),
[Concord: Permanent loss of authority](concord.md#9-permanent-loss-of-authority),
[Concord: Contract pointers on messages](concord.md#14-contract-pointers-on-messages),
[DIT-CCD-007: Watch heartbeat failure with valid records](../deckr-conformance-scenarios.md#dit-ccd-007-missed-watch-heartbeat-stops-authority-with-valid-records),
[DIT-CCD-008: Renewal failure and reconnection](../deckr-conformance-scenarios.md#dit-ccd-008-renewal-failure-with-blocked-cleanup-and-reconnection).

### SEM-CCD-004: Mutations and maintenance

**Checks**

- Enforce immutable generation identity.
- Enforce revision guards.
- Enforce bootstrap ownership.
- Enforce token refresh deadlines.
- Enforce cancellation requirements.
- Enforce successor creation requirements.
- Enforce stale grace periods and retention periods.
- Enforce durable cleanup marker requirements.
- Do not use an attached-participant bit as a substitute for a live token.

**References:** [Concord: Store mutations](concord.md#44-store-mutations),
[Concord: Permanent loss of authority](concord.md#9-permanent-loss-of-authority),
[Concord: Withdrawal and successor agreements](concord.md#10-withdrawal-and-successor-agreements),
[Concord: Owner bootstrap and competing sessions](concord.md#11-owner-bootstrap-and-competing-sessions),
[Concord: Maintenance](concord.md#15-concord-maintenance),
[DIT-CCD-008: Renewal failure and reconnection](../deckr-conformance-scenarios.md#dit-ccd-008-renewal-failure-with-blocked-cleanup-and-reconnection),
[DIT-CCD-009: Bootstrap with another live session](../deckr-conformance-scenarios.md#dit-ccd-009-bootstrap-preserves-another-live-session).

## Hardware

### SEM-HW-001: Inventory identity

**Checks**

- Check that the hardware advertisement payload `sessionId` equals the envelope `sessionId`.
- Check that payload `managerEndpoint` equals the envelope `endpoint`.
- Check that payload `managerEndpoint` equals `hardware_manager:<managerId>`.
- Check that each device-map key equals `deviceRef.deviceId`.
- Check that each device reference names that manager.
- Check that descriptor IDs agree with the device reference.
- Check that any supplied fingerprints agree with the device reference.

**References:** [Deckr hardware: Hardware discovery](deckr-hardware.md#hardware-discovery),
[Hardware advertisement payload schema](../contract/v1/schemas/deckr/hardware/advertisement-payload.v1.schema.json),
[Hardware advertisement device schema](../contract/v1/schemas/deckr/hardware/hardware-advertisement-device.v1.schema.json).

### SEM-HW-002: Capacity

**Checks**

- Require nonnegative counts.
- When total and available counts are both present, check this relationship:

  ```text
  availableInstances = max(totalInstances - claimedInstances, 0)
  ```

- Evaluate claim conflicts over currently valid agreements.
- Evaluate capacity limits over currently valid agreements.
- Do not use advertisements alone for these evaluations.

**References:** [Deckr hardware: Hardware claims](deckr-hardware.md#hardware-claims),
[Profile capacity schema](../contract/v1/schemas/deckr/hardware/profile-capacity.v1.schema.json).

### SEM-HW-003: Claim identity and scope

**Checks**

- Require unique claimed device IDs within one claim.
- Check that all device references name the manager encoded by `managerEndpoint`.
- Require exactly the controller and manager named in the terms as agreement participants.
- Check device ownership against current state.
- Check session identity against current state.
- Check capacity against current state.
- On device disconnect, terminate or replace the claim as specified in Deckr hardware.
- On a material descriptor change, terminate or replace the claim as specified in Deckr hardware.

**References:** [Deckr hardware: Hardware claims](deckr-hardware.md#hardware-claims),
[Hardware claim terms schema](../contract/v1/schemas/deckr/hardware/claim-terms.v1.schema.json).

### SEM-HW-004: Descriptor relationships

**Checks**

- Within each device, require unique connection IDs.
- Within each device, require unique control IDs.
- Within each device, require unique device-level capability IDs.
- Within each control, require unique capability IDs across all capability collections.
- Check that source connection references resolve to a declared connection.
- Check that parent control references resolve to declared controls.
- Check that related control references resolve to declared controls.
- Reject parent or related control references to the control itself.
- Check that a default status indicator resolves to an output capability.
- Check that projection sources resolve to real capabilities.
- Reject projection sources that refer to the projected capability itself.

**Note:** Array `uniqueItems` alone cannot enforce uniqueness of one field across otherwise different objects.

**References:** [Device descriptor schema](../contract/v1/schemas/deckr/hardware/device-descriptor.v1.schema.json),
[Control descriptor schema](../contract/v1/schemas/deckr/hardware/control-descriptor.v1.schema.json),
[Capability projection schema](../contract/v1/schemas/deckr/hardware/capability-projection.v1.schema.json).

### SEM-HW-005: Constraints and values

**Checks**

- Check that each capability constraint's minimum does not exceed its maximum.
- Apply descriptor units to the selected capability value or command.
- Apply descriptor ranges and enumerated values to the selected capability value or command.
- Apply device-specific limits to the selected capability value or command.
- Select the payload schema from the capability family, type, event, and command.
- Do not select the payload schema from the capability ID alone.
- Validate embedded descriptor schemas against their declared JSON Schema dialect.

**Note:** Structural schemas also check the family, direction, access, positive scales, normalized geometry, and core event and command vocabulary.

**References:** [Deckr hardware capabilities: Contract boundaries](deckr-hardware-capabilities.md#contract-boundaries),
[Capability descriptor schema](../contract/v1/schemas/deckr/hardware/capability-descriptor.v1.schema.json),
[Capability constraint schema](../contract/v1/schemas/deckr/hardware/capability-constraint.v1.schema.json).

### SEM-HW-006: Raster data

**Checks**

- Validate `set_frame` parameters against the set-frame schema.
- Require an empty object for `clear` parameters.
- Decode the base64 image data.
- Verify the actual image encoding against the declared values and device capability.
- Verify the actual image dimensions against the declared values and device capability.

**Note:** An annotation such as `contentEncoding` is not a decoder.
The general raster parameter union does not determine the command type.

**References:** [Deckr hardware capabilities: Raster bitmap](deckr-hardware-capabilities.md#raster-bitmap),
[Deckr hardware capabilities: Canonical schemas](deckr-hardware-capabilities.md#canonical-schemas),
[Set-frame parameter schema](../contract/v1/schemas/deckr/hardware/capabilities/raster-bitmap-set-frame-params.v1.schema.json),
[Clear parameter schema](../contract/v1/schemas/deckr/hardware/capabilities/raster-bitmap-clear-params.v1.schema.json).

## Services

### SEM-SVC-001: Advertisement agreement

**Checks**

- Check that `serviceEndpoint` equals `service:<serviceId>`.
- Check that `serviceEndpoint` equals the advertised endpoint.
- Check that payload and advertisement sessions agree.
- Check the feature ID against the selected service protocol declaration.
- Check the protocol namespace and version against that declaration.
- Check the advertisement profile and service-use profile against that declaration.
- Use the declaration's operations and messages as authoritative.
- Do not use arbitrary unverified advertisement strings as authoritative.

**References:** [Service advertisement payload schema](../contract/v1/schemas/deckr/services/service-advertisement-payload.v1.schema.json),
[Service protocol definition schema](../contract/v1/schemas/deckr/services/protocol-definition.v1.schema.json),
[Deckr services: Service package ownership](deckr-services.md#service-package-ownership).

### SEM-SVC-002: Declaration relationships

**Checks**

- Check that every non-null message `operation` names a declared operation.
- For consumer-to-service messages, require an operation.
- For bidirectional messages, require an operation.
- For command messages, require an operation.
- For query messages, require an operation.
- Check each advertised message name, intent, exchange, direction, and payload schema against the protocol.
- Check view-family prefixes, writers, and store names against the service declaration.
- Validate embedded schema documents against their declared dialect.

**Note:** Structural declaration validation cannot compare a supplied schema's meaning with the owner's published schema.

**References:** [Service protocol definition schema](../contract/v1/schemas/deckr/services/protocol-definition.v1.schema.json),
[Service message definition schema](../contract/v1/schemas/deckr/services/service-message-definition.v1.schema.json),
[Service view-family definition schema](../contract/v1/schemas/deckr/services/service-view-family-definition.v1.schema.json).

### SEM-SVC-003: Service-use authority

**Checks**

- Require an agreement without terms.
- Require exactly the service and its consumer as agreement participants.
- Check that the message pointer equals the held agreement pointer.
- Check that the agreement authorizes the sender and recipient.
- Check that the agreement authorizes the sessions, direction, and declared operation.
- Enforce the retained resource scope through the service protocol.
- After agreement, do not terminate authority because Beacon discovery alone is lost.

**References:** [Deckr services: Service use](deckr-services.md#service-use),
[Concord: Authority and discovery](concord.md#3-authority-and-discovery),
[Service-use contract schema](../contract/v1/schemas/deckr/services/use-contract.v1.schema.json).

### SEM-SVC-004: Protected view fences

**Checks**

- Require one flat stored object containing the payload and all fence fields listed below.
- Compare all applicable fence values against the authorized view context and active lease.
- Require the storage key format shown below.
- Treat revisions as store metadata, not payload fields.
- For a mismatching replacement record, prevent an existing reader or watch from exposing its payload.
- Apply the Deckr NATS binding recovery requirements.

| Fence subject | Fields |
| --- | --- |
| View | `viewKey` |
| Service identity | `serviceId`, `serviceNamespace`, `serviceEndpoint` |
| Service session | `serviceSessionId` |
| Consumer identity | `consumerEndpoint` |
| Consumer session | `consumerSessionId` |
| Writer | `writer` |
| Contract pointer | `contractId`, `generation` |

Storage key:

```text
<viewKey>.contract.<encoded-contractId>.<generation>
```

**References:** [Deckr services: Protected service views](deckr-services.md#protected-service-views),
[Deckr NATS binding: Materialized sources and recovery](deckr-nats-binding.md#8-materialized-sources-and-recovery),
[Protected service view record schema](../contract/v1/schemas/deckr/services/view-record.v1.schema.json),
[Service view key vectors](../contract/v1/vectors/service-view-keys.v1.json).

### SEM-SVC-005: Payload and reply selection

**Checks**

- Use the message declaration to select `params`, `event`, `result`, and `error` schemas.
- Check request and reply correlation.
- Check contract identity.
- Check session identity.
- Check expiry.
- Reject unsupported message names, even if the generic service envelope validates.
- Reject wrong message directions, even if the generic service envelope validates.
- Reject unauthorized operations, even if the generic service envelope validates.
- After reconnect, do not replay published commands under a different agreement.

**References:** [Deckr message catalog: Services lane](deckr-message-catalog.md#services-lane),
[Deckr messaging: Envelope](deckr-messaging.md#envelope),
[Deckr services: Managed service consumers](deckr-services.md#managed-service-consumers).

## Action Runtime

### SEM-ACT-001: Runtime identity

**Checks**

- Check that `serviceId` equals `action-runtime.<providerInstanceId>`.
- Check that the service endpoint equals `service:<serviceId>`.
- Check the provider-instance ID against its restricted token syntax.
- Check that the availability service session agrees with the held service-use agreement.
- For each non-null availability descriptor, check that its action ID equals its entry's action ID.
- Require a descriptor in each available entry.

**Note:** The schema also requires a descriptor in each available entry.

**References:** [Deckr Action Runtime: Action Runtime service](deckr-action-runtime.md#action-runtime-service),
[Availability payload schema](../contract/v1/schemas/deckr/action-runtime/availability-payload.v1.schema.json),
[Stored availability view schema](../contract/v1/schemas/deckr/action-runtime/availability-view.v1.schema.json),
[Action availability entry schema](../contract/v1/schemas/deckr/action-runtime/action-availability-entry.v1.schema.json).

### SEM-ACT-002: Requirements and routing

**Checks**

- Require unique action requirement names.
- Check that resolved capabilities and sources exist in the negotiated descriptors.
- Check that those capabilities and sources match the declared requirement.
- Treat binding, action-instance, and page-session IDs as controller-owned.
- Check that those IDs resolve in the authorized session.
- Check output generations against the controller's current binding generation.
- Check overlay generations against the controller's current binding generation.
- Do not treat a direct runtime message as permission to invent a binding.
- Do not treat that message as permission to cross a device or claim boundary.

**Note:** Nonnegative generation integers alone do not establish freshness.

**References:** [Deckr messaging: Message contract rule](deckr-messaging.md#message-contract-rule),
[Deckr Action Runtime](deckr-action-runtime.md),
[Capability requirement schema](../contract/v1/schemas/deckr/action-runtime/capability-requirement.v1.schema.json),
[Binding output schema](../contract/v1/schemas/deckr/action-runtime/binding-output-body.v1.schema.json),
[Binding overlay schema](../contract/v1/schemas/deckr/action-runtime/binding-overlay-body.v1.schema.json).

### SEM-ACT-003: Dynamic pages

**Checks**

- Require at least one child binding in a dynamic page.
- Require unique control IDs in those child bindings.
- Resolve `self` to the page opener's current action instance.
- Resolve `action` through its action/provider selector.
- Do not let providers supply controller-owned live action-instance IDs for dynamic children.
- Check selection and identity ownership against controller state.

**Note:** The schema checks conditional target fields.

**References:** [Dynamic page command schema](../contract/v1/schemas/deckr/action-runtime/dynamic-page-command.v1.schema.json),
[Page child binding target schema](../contract/v1/schemas/deckr/action-runtime/page-child-binding-target.v1.schema.json).

### SEM-ACT-004: Settings identity

**Checks**

- Check that each settings-target description mirrors the target's provider-instance ID.
- Check that the description mirrors the target's provider ID and optional action ID.
- For provider settings, omit action IDs.
- For action-instance settings, require the action ID and action-instance ID.
- Use the key-token algorithm for stable target keys.
- Use the layout for the target scope in the settings key vectors.

**References:** [Settings-target description schema](../contract/v1/schemas/deckr/action-runtime/settings-target-description.v1.schema.json),
[Settings-target reference schema](../contract/v1/schemas/deckr/action-runtime/settings-target-ref.v1.schema.json),
[Concord: Key tokens](concord.md#42-key-tokens),
[Settings key vectors](../contract/v1/vectors/settings-target-keys.v1.json).

## Testing obligations

- Use structurally invalid fixtures.
- Use structurally valid inputs that fail semantic validation.
- Test identical outcomes from cached and exact observations with independent implementations.
- Test source currentness with independent implementations.
- Test session changes, expiry, cleanup, and recovery with independent implementations.

**Note:** Schemas contain no runtime clock, KV state, image decoder, or authority engine.
Structural validation cannot establish runtime authority.

**References:** [Semantic validation vectors](../contract/v1/vectors/semantic-validation.v1.json),
[Deckr conformance scenarios](../deckr-conformance-scenarios.md),
[Schema guide: Versioning and acceptance](schema-guide.md#versioning-and-acceptance).
