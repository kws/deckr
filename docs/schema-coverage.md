# Extraction coverage

Baseline: `1dae18252aa1ddfd52e1f3e6f0655d75ae9673b6`. This inventory records public
wire models from the former Python implementation plus the retained TypeScript
settings snapshot. The machine-readable equivalent is
[source coverage](../contract/v1/catalogs/source-coverage.v1.json).

The extraction was checked before removing implementation files. Shared nested
models are individually reusable; a model's field validators were reviewed for
schema constraints and cross-field semantic requirements. Language-specific
constructor coercions and generated runtime defaults are not wire operations.

## Model definitions

| Former definition | Disposition |
| --- | --- |
| `EndpointAddress` | [Schema](../contract/v1/schemas/deckr/messaging/endpoint-address.v1.schema.json) |
| `EndpointTarget` | [Schema](../contract/v1/schemas/deckr/messaging/endpoint-target.v1.schema.json) |
| `BroadcastTarget` | [Schema](../contract/v1/schemas/deckr/messaging/broadcast-target.v1.schema.json) |
| `EntitySubject` | [Schema](../contract/v1/schemas/deckr/messaging/entity-subject.v1.schema.json) |
| `TraceContext` | [Schema](../contract/v1/schemas/deckr/messaging/trace-context.v1.schema.json) |
| `DeckrMessage` | [Schema](../contract/v1/schemas/deckr/messaging/message.v1.schema.json) |
| `ContractPointer` | [Schema](../contract/v1/schemas/protocols/concord/contract-pointer.v1.schema.json) |
| `BeaconProtocol` | [Schema](../contract/v1/schemas/protocols/beacon/beacon-protocol.v1.schema.json) |
| `AdvertisementRecord` | [Schema](../contract/v1/schemas/protocols/beacon/advertisement.v1.schema.json) |
| `TokenObservation` | [Schema](../contract/v1/schemas/protocols/concord/token-observation.v1.schema.json) |
| `ContractRecord` | [Schema](../contract/v1/schemas/protocols/concord/contract.v1.schema.json) |
| `ParticipantTokenRecord` | [Schema](../contract/v1/schemas/protocols/concord/participant-token.v1.schema.json) |
| `ConcordStaleObservationRecord` | [Schema](../contract/v1/schemas/protocols/concord/stale-observation.v1.schema.json) |
| `ConcordTokenCleanupRecord` | [Schema](../contract/v1/schemas/protocols/concord/token-cleanup.v1.schema.json) |
| `ConcordReaperConfig` | Local implementation configuration; timing policy remains in the Concord specification. |
| `DeviceIdentifier` | [Schema](../contract/v1/schemas/deckr/hardware/device-identifier.v1.schema.json) |
| `DeviceConnection` | [Schema](../contract/v1/schemas/deckr/hardware/device-connection.v1.schema.json) |
| `DeviceSourceReference` | [Schema](../contract/v1/schemas/deckr/hardware/device-source-reference.v1.schema.json) |
| `DeviceRef` | [Schema](../contract/v1/schemas/deckr/hardware/device-ref.v1.schema.json) |
| `ControlRef` | [Schema](../contract/v1/schemas/deckr/hardware/control-ref.v1.schema.json) |
| `CapabilityRef` | [Schema](../contract/v1/schemas/deckr/hardware/capability-ref.v1.schema.json) |
| `DescriptorCapabilityRef` | [Schema](../contract/v1/schemas/deckr/hardware/descriptor-capability-ref.v1.schema.json) |
| `ControlGeometry` | [Schema](../contract/v1/schemas/deckr/hardware/control-geometry.v1.schema.json) |
| `CapabilitySchema` | [Schema](../contract/v1/schemas/deckr/hardware/capability-schema.v1.schema.json) |
| `CapabilityConstraint` | [Schema](../contract/v1/schemas/deckr/hardware/capability-constraint.v1.schema.json) |
| `CapabilityUnit` | [Schema](../contract/v1/schemas/deckr/hardware/capability-unit.v1.schema.json) |
| `CapabilityProjection` | [Schema](../contract/v1/schemas/deckr/hardware/capability-projection.v1.schema.json) |
| `CapabilityDescriptor` | [Schema](../contract/v1/schemas/deckr/hardware/capability-descriptor.v1.schema.json) |
| `ControlDescriptor` | [Schema](../contract/v1/schemas/deckr/hardware/control-descriptor.v1.schema.json) |
| `DeviceDescriptor` | [Schema](../contract/v1/schemas/deckr/hardware/device-descriptor.v1.schema.json) |
| `ButtonActivationInputValue` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/button-activation-input-value.v1.schema.json) |
| `ButtonMomentaryInputValue` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/button-momentary-input-value.v1.schema.json) |
| `EncoderRelativeInputValue` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/encoder-relative-input-value.v1.schema.json) |
| `TouchGestureInputValue` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/touch-gesture-input-value.v1.schema.json) |
| `RasterBitmapSetFrameParams` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/raster-bitmap-set-frame-params.v1.schema.json) |
| `RasterBitmapClearParams` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/raster-bitmap-clear-params.v1.schema.json) |
| `DevicePowerCommandParams` | [Schema](../contract/v1/schemas/deckr/hardware/capabilities/device-power-command-params.v1.schema.json) |
| `ProfileCapacity` | [Schema](../contract/v1/schemas/deckr/hardware/profile-capacity.v1.schema.json) |
| `HardwareAdvertisementDevice` | [Schema](../contract/v1/schemas/deckr/hardware/hardware-advertisement-device.v1.schema.json) |
| `HardwareBeaconPayload` | [Schema](../contract/v1/schemas/deckr/hardware/advertisement-payload.v1.schema.json) |
| `HardwareClaimDevice` | [Schema](../contract/v1/schemas/deckr/hardware/hardware-claim-device.v1.schema.json) |
| `HardwareClaimTerms` | [Schema](../contract/v1/schemas/deckr/hardware/claim-terms.v1.schema.json) |
| `ControlInputMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/control-input-message.v1.schema.json) |
| `ControlCommandMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/control-command-message.v1.schema.json) |
| `CapabilityStateChangedMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/capability-state-changed-message.v1.schema.json) |
| `CapabilityStateRequestMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/capability-state-request-message.v1.schema.json) |
| `CapabilityStateReplyMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/capability-state-reply-message.v1.schema.json) |
| `CommandAcceptedMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/command-accepted-message.v1.schema.json) |
| `CommandRejectedMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/command-rejected-message.v1.schema.json) |
| `CommandReplyMessage` | [Schema](../contract/v1/schemas/deckr/hardware/bodies/command-reply-message.v1.schema.json) |
| `ServiceError` | [Schema](../contract/v1/schemas/deckr/services/service-error.v1.schema.json) |
| `ServiceMessageBody` | [Schema](../contract/v1/schemas/deckr/services/service-message-body.v1.schema.json) |
| `ServiceOperationDefinition` | [Schema](../contract/v1/schemas/deckr/services/service-operation-definition.v1.schema.json) |
| `ServicePayloadSchema` | [Schema](../contract/v1/schemas/deckr/services/service-payload-schema.v1.schema.json) |
| `ServiceMessageDefinition` | [Schema](../contract/v1/schemas/deckr/services/service-message-definition.v1.schema.json) |
| `ServiceViewFamily` | [Schema](../contract/v1/schemas/deckr/services/service-view-family.v1.schema.json) |
| `ServiceViewFamilyDefinition` | [Schema](../contract/v1/schemas/deckr/services/service-view-family-definition.v1.schema.json) |
| `ServiceAdvertisementPayload` | [Schema](../contract/v1/schemas/deckr/services/service-advertisement-payload.v1.schema.json) |
| `ActionMessageBody` | Abstract authoring base, with no independent wire contract. |
| `EmptyActionBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/empty-action-body.v1.schema.json) |
| `ActionExtensionBody` | Retired actions lane extension. Extensions use owner-defined service namespaces and payload schemas. |
| `CapabilityRequirementSelector` | [Schema](../contract/v1/schemas/deckr/action-runtime/capability-requirement-selector.v1.schema.json) |
| `CapabilityRequirement` | [Schema](../contract/v1/schemas/deckr/action-runtime/capability-requirement.v1.schema.json) |
| `MatchedCapability` | [Schema](../contract/v1/schemas/deckr/action-runtime/matched-capability.v1.schema.json) |
| `BindingMetadata` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-metadata.v1.schema.json) |
| `ActionInstanceMetadata` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-instance-metadata.v1.schema.json) |
| `PageSessionMetadata` | [Schema](../contract/v1/schemas/deckr/action-runtime/page-session-metadata.v1.schema.json) |
| `CapabilityInputEvent` | [Schema](../contract/v1/schemas/deckr/action-runtime/capability-input-event.v1.schema.json) |
| `ActionInstanceLifecycleBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-instance-lifecycle-body.v1.schema.json) |
| `BindingAttachedBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-attached-body.v1.schema.json) |
| `BindingDetachedBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-detached-body.v1.schema.json) |
| `PageSessionLifecycleBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/page-session-lifecycle-body.v1.schema.json) |
| `ActionLifecycleRejectedBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-lifecycle-rejected-body.v1.schema.json) |
| `CapabilityInputBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/capability-input-body.v1.schema.json) |
| `BindingOutputBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-output-body.v1.schema.json) |
| `BindingOverlayBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-overlay-body.v1.schema.json) |
| `BindingOverlayClearBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/binding-overlay-clear-body.v1.schema.json) |
| `SettingsSchemaMetadata` | [Schema](../contract/v1/schemas/deckr/action-runtime/settings-schema-metadata.v1.schema.json) |
| `SettingsTargetRef` | [Schema](../contract/v1/schemas/deckr/action-runtime/settings-target-ref.v1.schema.json) |
| `SettingsTargetDescription` | [Schema](../contract/v1/schemas/deckr/action-runtime/settings-target-description.v1.schema.json) |
| `ActionDescriptor` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-descriptor.v1.schema.json) |
| `ActionAvailabilitySelector` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-availability-selector.v1.schema.json) |
| `ActionAvailabilityEntry` | [Schema](../contract/v1/schemas/deckr/action-runtime/action-availability-entry.v1.schema.json) |
| `PageChildBindingTarget` | [Schema](../contract/v1/schemas/deckr/action-runtime/page-child-binding-target.v1.schema.json) |
| `PageChildBindingDescriptor` | [Schema](../contract/v1/schemas/deckr/action-runtime/page-child-binding-descriptor.v1.schema.json) |
| `DynamicPageCommand` | [Schema](../contract/v1/schemas/deckr/action-runtime/dynamic-page-command.v1.schema.json) |
| `OpenPageBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/open-page-body.v1.schema.json) |
| `ReplacePageBody` | [Schema](../contract/v1/schemas/deckr/action-runtime/replace-page-body.v1.schema.json) |
| `ActionRuntimeAvailabilityViewPayload` | [Schema](../contract/v1/schemas/deckr/action-runtime/availability-payload.v1.schema.json) |
| `SettingsSnapshot` | [Schema](../contract/v1/schemas/deckr/action-runtime/settings-snapshot.v1.schema.json) |

## Composed and previously implicit records

Additional schemas cover all eight hardware messages, all fourteen Action Runtime
messages, family unions, Deckr protocol wrappers, complete hardware/service/Action
Runtime advertisements and agreements, protected service-view storage fields,
stored availability, the portable service protocol declaration and service view refs.
These were previously generated unions, dataclasses, or assembled dictionaries.

All active lane operations are listed in [the message catalog](message-catalog.md).
The former `actions` lane and divergent older-port contracts are explicitly
retired in the [migration guide](migration-guide.md#retired-surfaces).

## Coverage boundary

Local task handles, watcher snapshots, callbacks, runtime configuration, exceptions,
component/host objects and transport client options are not independent wire
records. Their observable requirements are retained in protocol specifications
and the language-neutral conformance catalog. They are not recreated as SDK classes.

Every schema is listed in the manifest, resolves offline, and has at least one
positive and one negative fixture. The unconstrained JSON-value schema is the
sole negative-fixture exception. Structural fixture coverage is separate from
the semantic and distributed lifecycle scenarios consumers must execute.
