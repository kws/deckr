# deckr-profiles: Deckr Hardware And Action Profiles

**Status:** draft
**Scope:** Deckr discovery and live runtime agreements for hardware and actions
**Working name:** Profiles
**Purpose:** define the Deckr-specific Beacon and Concord profiles that replace
the current hardware inventory, action provider catalog, device claim, and
action binding coordination behavior.

---

## 1. Summary

Deckr is not introducing a general service registry here.

This document defines the profile contracts needed to fix the current Deckr
infrastructure:

```text
hardware manager advertises hardware
action provider advertises actions
controller claims hardware
controller binds actions to claimed controls
```

Beacon is the discovery layer:

```text
hardware manager --Beacon--> devices and capabilities
action provider  --Beacon--> actions and requirements
```

Concord is the live agreement layer:

```text
controller + hardware manager --Concord--> hardware claim
controller + action provider  --Concord--> action binding
```

The profile layer defines the Deckr meaning of the opaque data carried by or
referenced from Beacon and Concord.

The base protocols stay generic. They do not understand devices, actions,
controls, capabilities, plugins, or ownership policy.

---

## 2. Replacement rule

These profiles are clean replacements, not compatibility layers.

When the Deckr Beacon/Concord profiles are enabled, implementations must not
treat the old current-state contracts as parallel authorities:

| Current contract | Replacement |
| --- | --- |
| `HardwareInventory` in `deckr_discovery_v1` | Beacon hardware profile |
| `ActionProviderCatalog` in `deckr_discovery_v1` | Beacon action profile |
| `DeviceClaim` in `deckr_lease_v1` | Concord hardware claim profile |
| controller-local action binding attachment state | Concord action binding profile |

Old records may exist during development or test migration, but they must not
be used as a second source of truth for the same runtime decision.

The source of truth becomes:

```text
fresh Beacon advertisement -> candidate hardware/action provider
valid Concord contract     -> live hardware claim or live action binding
```

Endpoint presence records are not a separate authority for these profiles.
Beacon advertisements and Concord participant tokens carry the endpoint session
identity needed to fence stale publishers and restarted participants.

---

## 3. What Is In Scope

This document defines four Deckr profiles:

| Profile | Protocol | Purpose |
| --- | --- | --- |
| `dev.deckr.profile.hardware.v1` | Beacon | advertise hardware devices, controls, and capabilities |
| `dev.deckr.profile.actions.v1` | Beacon | advertise action provider instances and action descriptors |
| `dev.deckr.profile.hardware_claim.v1` | Concord | maintain controller ownership of hardware devices |
| `dev.deckr.profile.action_binding.v1` | Concord | maintain live controller/provider action bindings |

Messaging and RPC remain outside this profile layer, except where they conflict
with live agreement authority. A message can notify, command, or carry data, but
it must not by itself prove that a hardware claim or action binding is live.

Services and extension APIs are not Deckr core profiles here. Legacy service
catalog/status/view records are replaced by generic Beacon advertisements whose
`featureId`, payload profile, operation names, and schemas are owned by the
service package. If a service needs a maintained runtime agreement, it uses
generic Concord with service-owned terms.

For example, Sonos and OpenHAB integrations should advertise and bind through
Beacon/Concord using their own public namespaces. Deckr core must not declare
what a Sonos zone, OpenHAB item, media group, or service-specific view means.

Component ids are package/runtime identities. They may appear as metadata, but
they are not Beacon participant identities and they are not Concord claim or
binding participants.

---

## 4. Shared Profile Rules

Profile identifiers are public Deckr contract identifiers and must follow the
normal namespace rules.

Beacon advertisements for these profiles must include an opaque profile payload
or an explicit reference to a profile payload. The base Beacon layer validates
only the Beacon envelope. The Deckr profile validates the payload.

Concord contracts for these profiles must carry or reference immutable profile
terms. The base Concord layer validates only the Concord envelope and
participant tokens. The Deckr profile validates the terms.

Every profile must define:

```text
profile id
feature id, for Beacon profiles
participant roles, for Concord profiles
payload or term schema
identity fields
session fencing rules
canonical JSON form
hash input, if term hashes are used
valid fixtures
invalid fixtures
restart and stale-session scenarios
```

Terms that are accepted through Concord must be identical for every participant.
If a participant token carries a profile term hash, all implementations must
compute that hash from the same canonical JSON representation.

Beacon hardware and action profiles share a typed capacity shape for claim or
binding selection:

```json
{
  "totalInstances": 1,
  "claimedInstances": 0,
  "availableInstances": 1
}
```

Capacity rules:

```text
claimedInstances is the current number of live Concord agreements for the advertised thing
totalInstances is optional when the advertiser has no fixed or advertised maximum
availableInstances is optional and, when present, should equal max(totalInstances - claimedInstances, 0)
controllers may use capacity for candidate filtering, ordering, and load spreading
capacity is fresh Beacon discovery metadata, not ownership or binding authority
Concord remains the authority for whether a claim or binding is actually live
```

---

## 5. Beacon Hardware Profile

Profile id:

```text
dev.deckr.profile.hardware.v1
```

Feature id:

```text
dev.deckr.hardware
```

Advertiser:

```text
hardware_manager:<manager-id>
```

This profile replaces `HardwareInventory`.

The profile payload describes the devices currently exposed by one hardware
manager endpoint/session. It reuses the existing Deckr hardware descriptor
contracts:

```text
DeviceRef
DeviceDescriptor
ControlDescriptor
CapabilityDescriptor
CapabilitySchema
```

Normative payload shape:

```json
{
  "profile": "dev.deckr.profile.hardware.v1",
  "managerId": "mirabox-main",
  "managerEndpoint": "hardware_manager:mirabox-main",
  "sessionId": "manager-session",
  "labels": {
    "location": "office"
  },
  "devices": {
    "device-1": {
      "capacity": {
        "totalInstances": 1,
        "claimedInstances": 0,
        "availableInstances": 1
      },
      "deviceRef": {
        "managerId": "mirabox-main",
        "deviceId": "device-1",
        "fingerprint": "stable-device-fingerprint"
      },
      "descriptor": {
        "deviceId": "device-1",
        "fingerprint": "stable-device-fingerprint",
        "displayName": "MiraBox Stream Dock",
        "controls": []
      }
    }
  }
}
```

Rules:

```text
managerEndpoint must equal hardware_manager:<managerId>
payload sessionId must match the Beacon advertisement sessionId
device map keys must match deviceRef.deviceId
deviceRef.managerId must match managerId
descriptor.deviceId must match deviceRef.deviceId
deviceRef.fingerprint, when present, must match descriptor.fingerprint
capacity.claimedInstances must count currently valid Concord hardware claims for that device
capacity.totalInstances should normally be 1 for a single physical device
capacity.availableInstances should normally be 0 when a single physical device is claimed
```

The advertisement is aggregate by manager. Device removal is represented by
refreshing the advertisement without that device, or by withdrawing the whole
advertisement if the manager no longer advertises hardware.

There are no per-device tombstones in this profile.

A device is discoverable only while the advertisement is fresh and valid. A
controller may use local policy, labels, fingerprints, controls, and capability
descriptors to decide whether to propose a hardware claim.

Claimed devices should remain advertised while the manager still wants
controllers to know they exist. A fully claimed device is represented by
capacity such as `totalInstances: 1`, `claimedInstances: 1`, and
`availableInstances: 0`, not by withdrawing the device advertisement.

Beacon does not reserve the device and does not prove that a claim attempt will
succeed.

---

## 6. Concord Hardware Claim Profile

Profile id:

```text
dev.deckr.profile.hardware_claim.v1
```

Participants:

```text
controller:<controller-id>
hardware_manager:<manager-id>
```

This profile replaces `DeviceClaim`.

A hardware claim is a two-sided agreement that a controller currently owns one
or more devices from a hardware manager.

Normative term shape:

```json
{
  "profile": "dev.deckr.profile.hardware_claim.v1",
  "claimId": "claim-01J00000000000000000000000",
  "controllerEndpoint": "controller:main",
  "managerEndpoint": "hardware_manager:mirabox-main",
  "managerAdvertisementId": "beacon-01J00000000000000000000000",
  "devices": [
    {
      "deviceRef": {
        "managerId": "mirabox-main",
        "deviceId": "device-1",
        "fingerprint": "stable-device-fingerprint"
      },
      "instanceCount": 1
    }
  ]
}
```

Rules:

```text
controllerEndpoint must name the controller participant
managerEndpoint must name the hardware manager participant
each deviceRef.managerId must match managerEndpoint's manager id
device ids in one claim must be unique
instanceCount must be greater than zero and must not exceed the device's advertised availableInstances when that value is present
fingerprint is required when the Beacon hardware advertisement carried one
managerAdvertisementId is evidence used by the profile, not Beacon authority
```

Concrete Concord records for an accepted single-device claim:

```json
{
  "schema": "dev.deckr.concord.contract.v1",
  "contractId": "c1cf4ce8-9f6f-49e2-a17b-88f407f19c90",
  "generation": 1,
  "profile": "dev.deckr.profile.hardware_claim.v1",
  "participants": [
    "controller:main",
    "hardware_manager:mirabox-main"
  ],
  "state": "open",
  "termsHash": "sha256:...",
  "terms": {
    "profile": "dev.deckr.profile.hardware_claim.v1",
    "claimId": "a3b2fd9a-e84d-4d79-8e1d-a5c6a522c4f3",
    "controllerEndpoint": "controller:main",
    "managerEndpoint": "hardware_manager:mirabox-main",
    "managerAdvertisementId": "7d4b5125-3d69-4d1a-b98a-22e73e1d7bd1",
    "devices": [
      {
        "deviceRef": {
          "managerId": "mirabox-main",
          "deviceId": "device-1",
          "fingerprint": "fingerprint:deck-1"
        },
        "instanceCount": 1
      }
    ]
  },
  "createdBy": "controller:main",
  "createdAt": "2026-05-20T10:00:00Z"
}
```

Controller participant token:

```json
{
  "schema": "dev.deckr.concord.participant-token.v1",
  "contractId": "c1cf4ce8-9f6f-49e2-a17b-88f407f19c90",
  "generation": 1,
  "participant": "controller:main",
  "sessionId": "controller-session",
  "tokenId": "6cd88f4f-1069-4d4c-9f0e-b79b30b107f4",
  "refreshSeq": 1,
  "ttlSeconds": 30,
  "termsHash": "sha256:..."
}
```

Hardware manager participant token:

```json
{
  "schema": "dev.deckr.concord.participant-token.v1",
  "contractId": "c1cf4ce8-9f6f-49e2-a17b-88f407f19c90",
  "generation": 1,
  "participant": "hardware_manager:mirabox-main",
  "sessionId": "manager-session",
  "tokenId": "d7761c97-4399-4e02-89a7-2d0e1c99d84a",
  "refreshSeq": 1,
  "ttlSeconds": 30,
  "termsHash": "sha256:..."
}
```

The claim is live only while the Concord contract is open and every named
participant maintains a live token for the same contract id, generation,
session, and terms hash. After the manager accepts the claim, its next Beacon
hardware advertisement should reflect the occupied capacity, for example
`totalInstances: 1`, `claimedInstances: 1`, and `availableInstances: 0` for a
single physical device.

Claim flow:

```text
1. Hardware manager advertises devices through Beacon.
2. Controller selects one or more devices.
3. Controller creates immutable hardware claim terms.
4. Controller creates a Concord contract naming itself and the manager.
5. Controller attaches its participant token.
6. Hardware manager reads the same terms.
7. Manager accepts by attaching its participant token, or rejects by cancelling.
8. The claim is usable only while the Concord contract is valid.
```

Ownership policy is manager-side profile semantics:

```text
a manager accepts at most one currently valid hardware claim per device
a manager may reject a claim if the device disappeared or fingerprint changed
a manager releases ownership when the Concord claim is cancelled or invalid
```

Either participant may cancel at any time.

If either participant restarts and receives a new endpoint session, the old
claim is not live. A successor claim must be created deliberately.

The old unilateral behavior is forbidden:

```text
controller endpoint address alone does not preserve ownership
manager endpoint address alone does not preserve ownership
stale local claim state does not preserve ownership
old claim terms are not re-adopted after restart without a live Concord contract
```

---

## 7. Beacon Action Profile

Profile id:

```text
dev.deckr.profile.actions.v1
```

Feature id:

```text
dev.deckr.actions
```

Advertiser:

```text
action_provider:<provider-instance-id>
```

This profile replaces `ActionProviderCatalog`.

The profile payload describes the action types currently advertised by one
action provider endpoint/session. It reuses the existing action contract models:

```text
ActionProviderCatalog identity fields
ActionDescriptor
CapabilityRequirement
CapabilityRequirementSelector
settings schema fields
labels
annotations
```

Normative payload shape:

```json
{
  "profile": "dev.deckr.profile.actions.v1",
  "providerInstanceId": "clock-main",
  "providerEndpoint": "action_provider:clock-main",
  "providerId": "dev.deckr.clock",
  "sessionId": "provider-session",
  "labels": {
    "location": "office"
  },
  "annotations": {
    "runtime": "python"
  },
  "actions": {
    "dev.deckr.clock.action.digital": {
      "actionId": "dev.deckr.clock.action.digital",
      "name": "Digital Clock",
      "requirements": [],
      "capacity": {
        "claimedInstances": 3
      },
      "hints": {
        "priority": 100,
        "load": 0.42
      }
    }
  }
}
```

Rules:

```text
providerEndpoint must equal action_provider:<providerInstanceId>
payload sessionId must match the Beacon advertisement sessionId
action map keys must match descriptor actionId
descriptor providerId, when present, must match payload providerId
requirements use Deckr capability descriptor semantics
capacity.claimedInstances must count currently valid Concord action bindings for that action/provider
capacity.totalInstances may be omitted when the provider does not advertise a fixed maximum
hints may guide local candidate ordering but are never binding authority
```

An action is discoverable only while the advertisement is fresh and valid.

Beacon does not bind the action to a control. It only lets a controller find
candidate action providers and action descriptors.

Controllers may use action capacity and hints to spread bindings across
providers, for example by preferring the candidate with the fewest claimed
instances. Concord remains the authority for whether the selected provider
actually accepts and maintains the binding.

---

## 8. Concord Action Binding Profile

Profile id:

```text
dev.deckr.profile.action_binding.v1
```

Participants:

```text
controller:<controller-id>
action_provider:<provider-instance-id>
```

This profile replaces the current fragile live action attachment behavior.

An action binding is a two-sided agreement that an action provider is currently
attached to a controller-owned binding on a concrete claimed control or
capability.

The stable binding terms reuse the existing action binding metadata shape, but
they must be immutable for the lifetime of one Concord binding contract.

Normative term shape:

```json
{
  "profile": "dev.deckr.profile.action_binding.v1",
  "bindingId": "binding-01J00000000000000000000000",
  "controllerEndpoint": "controller:main",
  "providerEndpoint": "action_provider:clock-main",
  "providerInstanceId": "clock-main",
  "providerId": "dev.deckr.clock",
  "actionId": "dev.deckr.clock.action.digital",
  "actionInstanceId": "clock-instance-1",
  "configId": "clock-config-1",
  "contextId": "context-01J00000000000000000000000",
  "hardwareClaimId": "claim-01J00000000000000000000000",
  "deviceRef": {
    "managerId": "mirabox-main",
    "deviceId": "device-1",
    "fingerprint": "stable-device-fingerprint"
  },
  "controlRef": {
    "deviceRef": {
      "managerId": "mirabox-main",
      "deviceId": "device-1",
      "fingerprint": "stable-device-fingerprint"
    },
    "controlId": "key-0-0"
  },
  "matchedCapabilities": []
}
```

Rules:

```text
controllerEndpoint must name the controller participant
providerEndpoint must name the action provider participant
providerEndpoint must equal action_provider:<providerInstanceId>
hardwareClaimId must name a currently valid hardware claim owned by the controller
deviceRef and controlRef must refer to hardware covered by that claim
matchedCapabilities must be concrete capability refs on that control or device
bindingId must be unique within the controller's active binding set
```

Binding flow:

```text
1. Action provider advertises actions through Beacon.
2. Controller resolves an action against a claimed control/capability.
3. Controller creates immutable action binding terms.
4. Controller creates a Concord contract naming itself and the provider.
5. Controller attaches its participant token.
6. Provider reads the same binding terms.
7. Provider accepts by attaching its participant token, or rejects by cancelling.
8. The binding is usable only while the Concord contract is valid.
```

This profile fixes the plugin restart problem:

```text
provider offline -> provider token expires or is cancelled -> binding not live
provider restart -> new session -> old binding not live
controller restart -> new session -> old binding not live
lost provider local state -> provider cancels or stops refreshing
lost controller local state -> controller cancels or stops refreshing
```

The controller may re-resolve and create a successor binding, but that is a new
agreement. It is not an implicit re-attach based only on provider id, action id,
or binding id.

---

## 9. Messaging Conflicts

Existing lane messages remain useful, but they are not live agreement
authority.

For hardware traffic:

```text
hardware input, output, command, and state messages must be fenced by a live
hardware claim when the operation requires claimed hardware ownership
```

For action traffic:

```text
bindingAttached and bindingDetached may notify about Concord binding changes
bindingOutput and bindingOverlay are valid only for a live action binding
dynamic page commands are valid only from a live action binding
```

If a message says a binding exists but the Concord action binding is not valid,
the binding is not live.

If Concord says a binding is cancelled or stale, later messages with old binding
metadata are stale even when their ids match.

The exact message-body changes are outside this document, but messages must
carry or imply enough identity to validate against the live Concord contract.

---

## 10. Component Ids And Runtime Metadata

Component discovery remains the runtime host's component discovery problem.

Beacon does not discover Python entry points, package components, plugin
classes, or local host component definitions.

A component id may be useful metadata in a Beacon profile payload:

```text
which package produced this action provider
which component implementation owns this hardware manager
which runtime started this endpoint
```

But component ids are not:

```text
endpoint addresses
endpoint sessions
hardware manager ids
provider instance ids
controller ids
claim ids
binding ids
Concord participants
```

Runtime agreements must be fenced by endpoint/session/contract identity, not by
package or component identity.

---

## 11. Conformance Expectations

The Python, Rust, and JavaScript implementations must implement the same
profile contracts.

Required artifacts:

```text
Beacon hardware profile JSON Schema
Beacon action profile JSON Schema
Concord hardware claim term JSON Schema
Concord action binding term JSON Schema
valid and invalid fixtures for each profile
canonical JSON and hash vectors for Concord terms
single-owner hardware conflict scenarios
hardware manager restart scenarios
controller restart scenarios
action provider restart scenarios
binding cancellation scenarios
message-with-stale-binding scenarios
```

The existing Deckr descriptor and action schemas should be reused rather than
redefined where their shapes are already correct.

If this document disagrees with the old unilateral claim behavior, this document
wins. Deckr is still alpha; the fix is replacement, not backward compatibility.

---

## 12. One-Sentence Definition

Deckr profiles are the concrete hardware and action contracts that tell Beacon
what Deckr participants advertise and tell Concord what live hardware claims and
action bindings mean.
