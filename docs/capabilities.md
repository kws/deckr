# Deckr Capability Contracts

This document defines how Deckr uses device, control, and capability descriptors
for v1. The descriptor model is intentionally extensible: hardware managers and
adapters can expose new non-Deckr capability families without changing the generic Deckr schemas.

At the same time, any capability family under the `dev.deckr.*` namespace is a core
Deckr contract. Core capabilities must be specified in `deckr`, not improvised
by a driver, controller, plugin, or SDK helper. General public identifier
ownership rules live in [`namespaces.md`](namespaces.md).

## Contract Boundaries

A capability descriptor tells Deckr what a device or control can do. The shared
descriptor schema is [CapabilityDescriptor](../contract/v1/schemas/deckr/hardware/capability-descriptor.v1.schema.json) and defines the common fields:

- `capabilityId`: descriptor-local identifier used to target a concrete
  capability on a specific device or control.
- `family`: globally namespaced semantic family.
- `type`: family-local capability type.
- `direction`: one of `input`, `output`, `state`, or `command`.
- `access`: what the capability supports, such as `emits`, `settable`,
  `readable`, `requestable`, or `invokable`.
- `eventTypes`: event names emitted by input or state capabilities.
- `commandTypes`: command names accepted by output or command capabilities.
- `valueSchema`: JSON Schema for emitted values or state values.
- `commandSchema`: JSON Schema for command parameters.
- `constraints`, `units`, `projection`, and `sources`: metadata used for
  validation, UI, derivation, and hardware provenance.

The `capabilityId` is not the semantic contract. It is the addressable id of a
real capability in one descriptor. Generic matching should prefer
`family`, `type`, `direction`, and event or command names. Once a controller has
matched a concrete binding, messages target the selected `capabilityId`.

Validate JSON values against the selected capability schema. Runtime mapping
classes and immutability wrappers are implementation details.

## Core Versus Extension Families

Deckr core families use the `dev.deckr.*` namespace. A `dev.deckr.*` family is valid
only when it is listed by the capability descriptor schema. Adding one is a core
contract change and must define the family, type names, direction, access,
events or commands, schemas, constraints, units, tests, and docs.

Extension families must not use the `dev.deckr.*` namespace. They must be globally
namespaced according to [`namespaces.md`](namespaces.md). Deckr should accept
extension capabilities without code changes when their descriptors are valid and
their values are JSON wire-safe.

Extension semantics belong in the descriptor and in the package that owns the
extension. Deckr may route, bind, expose, and target the capability generically,
but it does not parse extension values unless a Deckr component has explicit
knowledge of that extension family.

If the controller, hardware managers, SDKs, or action providers need shared
semantic behavior that is not naturally extension-local, add a deliberate
Deckr core capability instead of relying on duplicated convention.

## Current Core Families

The current v1 core capability families are:

| Family | Type | Direction | Access | Events / Commands |
| --- | --- | --- | --- | --- |
| `dev.deckr.input.button` | `activation` | `input` | `emits` | event `press` |
| `dev.deckr.input.button` | `momentary` | `input` | `emits` | events `down`, `up` |
| `dev.deckr.input.encoder` | `relative` | `input` | `emits` | event `rotate` |
| `dev.deckr.input.touch` | `gesture` | `input` | `emits` | events `tap`, `swipe` |
| `dev.deckr.output.raster` | `bitmap` | `output` | `settable` or `invokable` | commands `set_frame`, `clear` |
| `dev.deckr.device.power` | `screen` | `command` | `invokable` | commands `sleep`, `wake` |

These family and type names are enforced by `CapabilityDescriptor`. A descriptor
using a `dev.deckr.*` family outside this list is invalid.

## Core Input Values

Input messages carry the selected capability and an `eventType`. The value
payload must match the selected capability's `valueSchema`.

For ordinary action activation, `up` from a momentary button, `press` from an
activation button, and `tap` from a touch gesture are equivalent completion
events. Hardware managers should emit the event for the capability the control
actually supports. They should not emit an additional synonym event for the same
physical interaction. Actions that only need "the user activated this control"
should accept all three events; actions that need lifecycle or gesture-specific
behavior should declare and handle the narrower capability they require.

### Button Activation

Family: `dev.deckr.input.button`

Type: `activation`

Event: `press`

Value schema id: `dev.deckr.value.input.button.activation.v1`

Canonical value:

```json
{
  "eventType": "press"
}
```

The `eventType` field in the message and the `eventType` field in the value
must agree.

### Button Momentary

Family: `dev.deckr.input.button`

Type: `momentary`

Events: `down`, `up`

Value schema id: `dev.deckr.value.input.button.momentary.v1`

Canonical values:

```json
{
  "eventType": "down"
}
```

```json
{
  "eventType": "up"
}
```

The `eventType` field in the message and the `eventType` field in the value
must agree.

### Encoder Relative

Family: `dev.deckr.input.encoder`

Type: `relative`

Event: `rotate`

Value schema id: `dev.deckr.value.input.encoder.relative.v1`

Canonical values:

```json
{
  "delta": 1,
  "direction": "clockwise"
}
```

```json
{
  "delta": -1,
  "direction": "counterclockwise"
}
```

`delta` is a non-zero signed integer measured in detents unless the descriptor
declares a more specific unit. Positive values mean clockwise rotation. Negative
values mean counterclockwise rotation. `direction` is optional, but if present
it must agree with the sign of `delta`.

Consumers validate this payload with the
[encoder value schema](../contract/v1/schemas/deckr/hardware/capabilities/encoder-relative-input-value.v1.schema.json).

### Touch Gesture

Family: `dev.deckr.input.touch`

Type: `gesture`

Events: `tap`, `swipe`

Value schema id: `dev.deckr.value.input.touch.gesture.v1`

Canonical values:

```json
{
  "eventType": "tap"
}
```

```json
{
  "eventType": "swipe",
  "direction": "left"
}
```

```json
{
  "eventType": "swipe",
  "direction": "right"
}
```

The `eventType` field in the message and the `eventType` field in the value
must agree. `direction` is meaningful for `swipe`; currently supported core
directions are `left` and `right`.

## Core Output And Command Values

Command messages carry the selected capability and a `commandType`. The params
payload must match the selected capability's `commandSchema`.

### Raster Bitmap

Family: `dev.deckr.output.raster`

Type: `bitmap`

Commands: `set_frame`, `clear`

Command schema id: `dev.deckr.command.output.raster.bitmap.v1`

`set_frame` params:

```json
{
  "image": "<base64>",
  "encoding": "png"
}
```

`image` is a base64-encoded frame. `encoding` is currently `png` or `jpeg`.
Descriptors should constrain `width`, `height`, and any fixed `rotation` in
`constraints`, using `pixel` and `degree` units.

`clear` params:

```json
{}
```

### Device Power Screen

Family: `dev.deckr.device.power`

Type: `screen`

Commands: `sleep`, `wake`

Command schema id: `dev.deckr.command.device.power.screen.v1`

Command params:

```json
{}
```

Device-level capabilities do not have a `controlId`. They target the device
itself.

## Canonical schemas

All core value and command parameter definitions are published independently in
[the capability schema directory](../contract/v1/schemas/deckr/hardware/capabilities/).
Use the manifest's `wireSchemaId` mapping to resolve the IDs listed above.

Raster commands have separate `set_frame` and `clear` parameter schemas; the
family union alone does not select the command. A `set_frame` requires image and
encoding, and `clear`, `sleep` and `wake` use empty objects. Device-specific raster
sizes constrain the reusable parameter schema rather than redefine it under the
same ID. Decoding base64 and checking actual image bytes/dimensions are semantic checks.

## Descriptor-Driven Extensibility

Deckr must be able to bind and route capabilities it does not know in code. For
an extension capability to be useful, its descriptor should include:

- A globally namespaced `family` outside `dev.deckr.*`.
- A stable `type` within that family.
- The correct `direction` and matching `access`.
- Complete `eventTypes` or `commandTypes` when the capability emits or accepts
  named operations.
- `valueSchema` or `commandSchema` whenever values or params are not empty.
- `constraints` and `units` for numeric values, sizes, ranges, rates, or
  physical quantities.
- `projection` metadata when the capability is derived from another capability.
- `sources` metadata when provenance matters for diagnostics or adapter
  boundaries.

Controllers and action provider runtimes should treat unknown extension values
as JSON data governed by the descriptor. They should not add extension-specific
fallback parsing in Deckr core.

Action providers can request extension capabilities by declaring requirements
against the extension `family`, `type`, direction, events, and commands. A
provider that needs semantic helpers for an extension should get those helpers
from the extension package that owns the family, not from Deckr core.

## Adding A Deckr Core Capability

Add a new `dev.deckr.*` capability only when the behavior is shared platform
semantics rather than one device family or one plugin's private convention.

The required checklist is:

1. Specify the family, allowed types, direction, access and event/command vocabulary.
2. Update the capability descriptor schema and publish reusable value/parameter schemas.
3. Add complete positive and negative examples, including command-specific cases.
4. Add semantic cases where descriptor lookup, image decoding or cross-field
   relationships cannot be checked structurally.
5. Update producers/consumers and this reference in their respective repositories.
