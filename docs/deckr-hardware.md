# Deckr hardware v1 specification

This specification defines Deckr hardware discovery, claims, and manager lifecycle requirements.
Deckr core owns the hardware advertisement and claim profiles.
Generic Concord validates contract and token mechanics.
Profile owners validate the meaning of terms and other profile data.
Profile policy does not replace Concord authority.

See the [schema guide](schema-guide.md), [Deckr message catalog](deckr-message-catalog.md),
and [semantic validation rules](semantic-rules.md#hardware).
The [Deckr hardware capabilities specification](deckr-hardware-capabilities.md)
defines capability descriptors, values, and commands.

## Hardware discovery

Deckr core owns the Beacon profile `dev.deckr.profile.hardware.v1`.
This profile gives information about hardware devices, controls, and capabilities.
Its payload must agree with these identity conditions:

| Payload field | Required relationship |
| --- | --- |
| `sessionId` | The value must be the same as the advertisement `sessionId`. |
| `managerEndpoint` | The value must be the same as the advertisement `endpoint`. |
| `managerId` | The value must be the same as the hardware-manager endpoint ID. |

The hardware Beacon profile is the only public source for device inventory.
The hardware lane is named `hardware_messages`.
Hardware lane messages do not announce inventory lifecycle changes.
Hardware lane messages do not replace the device inventory published through
Beacon.
They can carry input, commands, capability state, and replies. These messages
must not replace Beacon discovery or Concord authority.

Beacon can identify a device before an endpoint makes a claim. Beacon does not
give device ownership. A missing hardware advertisement alone does not withdraw
an existing device claim. The [hardware claim profile](#hardware-claims)
gives the claim requirements.

## Hardware claims

The hardware claim profile is `dev.deckr.profile.hardware_claim.v1`.

Its terms identify a controller endpoint, a hardware-manager endpoint, and one or more claimed devices. Device ids in one claim must be unique. `instanceCount` must be greater than zero.

Participants validate ownership through the contract, tokens, endpoint, session, and device refs. Hardware-manager and profile policy control single ownership and capacity over valid Concord claims.

If a claimed device disconnects, the hardware manager must cancel or end the claim, or stop token maintenance. A missing hardware advertisement alone does not withdraw the claim.

The v1 protocol expects claimed device descriptors to stay stable. A material descriptor change uses claim cancellation or token loss, followed by a new discovery candidate. Hardware lane lifecycle messages do not specify this change.

## Managed hardware managers

Hardware managers must follow the
[managed lifecycle requirements](deckr-messaging.md#managed-lifecycle-boundaries).
These requirements assign protocol operations to the managed lifecycle boundary.
Application code supplies device behavior and domain policy.

## Implementation guidance

Publish device, control, and capability descriptors in a hardware Beacon advertisement.
Negotiate a hardware claim.
Validate all hardware lane traffic against the exact contract and device references.
