# Device Capabilities Architecture

This document is normative architecture, not a description of whatever the code
happens to do today.

Deckr is in ALPHA. We are still deciding what the architecture is. Because of
that, backwards compatibility is not a goal. Compatibility shims, aliases,
compatibility adapter layers, dual APIs, transitional discovery paths, legacy
fallback behavior, and old wire-contract aliases are strictly forbidden. If the
current hardware model is wrong, remove it and replace it with the right design.
Do not preserve `slot`, `key`, `dial`, `touch`, or `hid` shaped abstractions as
core Deckr APIs just because they already exist.

## Core Goal

Deckr needs one shared language for devices, controls, and capabilities.

That language must let controllers, hardware managers, plugin hosts, and future
adapters answer:

- what physical, virtual, browser-hosted, OS-hosted, or remote surface is
  available
- what user-facing controls exist on that surface
- what each control can emit
- what each control or device can display, indicate, vibrate, play, or change
- which commands are safe and meaningful
- which state is bindable user interaction, configuration, diagnostics, or
  transport health
- how a controller can match a device to configuration without depending on USB
  paths, HID paths, MQTT topics, WebSocket sessions, or Elgato slot shapes

The goal is not to invent a huge abstract device ontology. The goal is to define
enough structure that new devices are additions to the descriptor model, not new
special cases in the platform architecture.

## Scope

This document defines the Deckr device capability model owned by `deckr`.

It covers:

- device descriptors
- control descriptors
- capability descriptors
- structured device, control, and capability references
- connection facts and source metadata
- capability directions, access flags, schemas, constraints, and units
- hardware message implications for discovery, input, commands, and state
- controller binding implications
- plugin action requirement implications

This document does not define:

- concrete USB, HID, MQTT, WebSocket, Zigbee, mobile widget, browser, or vendor
  protocols
- driver-private layouts or report parsing
- plugin runtime process protocols
- final controller UI design
- every future capability family

Concrete protocols adapt into this model at their manager boundary.

## Architectural Posture

The core object is not a slot. The core object is a device descriptor.

Conceptually:

```text
hardware manager endpoint
  exposes one or more devices
    each device has identifiers, connection facts, topology, controls, and capabilities
      each control has zero or more input capabilities
      each control has zero or more output capabilities
      each control may expose state/config/diagnostic capabilities
```

The same descriptor model must work for:

- local USB/HID hardware
- remote HID managers
- MQTT and Zigbee2MQTT controls
- all-in-one remotes
- desktop control surfaces
- browser surfaces
- iOS and Android widgets
- virtual devices
- future devices Deckr does not know about yet

The model must be explicit and schema-bearing. It must not depend on gesture
strings, delimiter-packed ids, or transport-local names.

## Ownership And Contract Format

Core descriptor and hardware message contracts belong in `deckr`.

Python is expected to author these contracts using Pydantic models. Generated
JSON Schema is the interoperability artifact for non-Python implementations.

Drivers, controllers, plugin hosts, and transports consume these contracts. They
must not invent competing core payload shapes.

Extension capability families are allowed, but they must be globally namespaced.
They must not redefine Deckr-owned capability families or rely on unqualified
names that look like core contracts.

## Devices

A device is a controller-visible surface or sub-surface exposed by a hardware
manager.

A device may be:

- physical
- virtual
- remote
- OS-hosted
- browser-hosted
- a child endpoint of a larger device

A device descriptor must include:

- manager-local `device_id`
- stable `fingerprint` for controller configuration matching
- display name
- optional manufacturer, model, model id, serial number, hardware version, and
  firmware version
- structured identifiers
- structured connections
- optional parent device reference
- optional suggested area or placement metadata
- controls
- device-level capabilities
- source references where useful for diagnostics

`device_id` is scoped to the hardware manager that exposed it. It is not a
deployment-wide identity unless carried inside a structured device reference that
also identifies the manager context.

`fingerprint` is the stable identity used for controller configuration matching.
Virtual and remote devices must provide a stable fingerprint just as physical
devices do.

Connection facts are not routing identities. Examples include:

- USB vendor id, product id, serial, interface number
- HID usage page, usage, report descriptor hash
- Bluetooth address or service uuid when appropriate
- MQTT topic root
- Zigbee IEEE address and endpoint
- browser session origin
- mobile platform widget family

These facts are useful for diagnostics, matching hints, and driver behavior.
They must not become Deckr endpoint addresses or durable controller binding ids.

## Controls

A control is an addressable user-facing element or region on a device.

Examples:

- button
- key with display
- rotary encoder
- motorized fader
- touch strip
- touchscreen region
- LED ring
- indicator LED
- haptic actuator
- speaker or beeper
- phone widget button
- phone widget toggle
- browser tile
- IR emitter command pad

A control descriptor must include:

- `control_id`
- kind
- optional label
- optional group id
- optional parent control id
- optional geometry
- optional surface id
- input capabilities
- output capabilities
- state/config/diagnostic capabilities
- source references where useful for diagnostics

`control_id` is scoped to one device.

Controls may be compound. A Stream Deck Plus style dial with touchscreen area can
be represented as one compound control or as related child controls. The
relationship must be structured through fields such as `group_id`,
`parent_control_id`, `surface_id`, geometry, and source references. It must not
be inferred from matching slot names.

Controls may overlap or share a physical surface. V1 does not need a full
surface-composition engine. Hardware managers may flatten complex devices into
related controls with their own raster and input capabilities. The descriptor
must leave room for:

- shared `surface_id`
- parent/child or group relationships
- raster regions within a larger physical surface
- optional layer or priority hints when the hardware has observable overlay
  behavior
- source references back to the same underlying display, report, or protocol
  endpoint

Controllers may ignore those relations when they only need simple bindings. More
capable controllers can use them for preview, layout, conflict warnings, or
future advanced configuration.

## Capabilities

A capability describes something a device or control can do.

Capabilities are explicit, typed, namespaced, and schema-bearing. They are not a
bag of gesture strings.

A capability descriptor must include:

- `capability_id`
- family
- type
- direction
- access
- optional value schema
- optional command schema
- optional constraints
- optional units
- optional event types
- optional command types
- optional source references

`capability_id` is scoped to the descriptor that owns it. When a message targets
a control capability, the target is the tuple of device reference, control id,
and capability id.

Suggested direction values:

- `input`
- `output`
- `state`
- `command`

Suggested access flags:

- `emits`
- `readable`
- `settable`
- `requestable`
- `invokable`

Minimum v1 core capability families:

- `deckr.input.button`
- `deckr.input.encoder`
- `deckr.input.touch`
- `deckr.output.raster`
- `deckr.device.power`
- `deckr.device.brightness`
- `deckr.state.battery`
- `deckr.state.diagnostic`

Future capability families may include:

- `deckr.input.switch`
- `deckr.input.axis`
- `deckr.input.pointer`
- `deckr.output.text`
- `deckr.output.indicator`
- `deckr.output.haptic`
- `deckr.output.audio`
- `deckr.state.connectivity`

Third-party capability families must use globally owned names. They must not use
short unqualified identifiers that could collide with Deckr core contracts.

## Device References And Subjects

Hardware routing uses the hardware manager endpoint. Device/control/capability
identity is the message subject.

A controller-to-hardware command is routed to:

```text
hardware_manager:<manager_id>
```

The affected device, control, and capability are carried as structured subject
data or structured body references.

Do not encode manager identity into `device_id`. Do not route through USB paths,
HID paths, MQTT topics, WebSocket sessions, browser sessions, mobile widget
runtime ids, or transport ids.

## Hardware Messages

The core hardware lane is `hardware_messages`.

No legacy hardware lane alias exists. All implementations should use the single
public hardware lane name before v1.

Device discovery must carry `DeviceDescriptor`.

Input messages should use a generic capability-targeted shape:

```text
controlInput
  device_ref
  control_id
  capability_id
  event_type
  value
  sequence
  occurred_at
```

Output and command messages should use a generic capability-targeted shape:

```text
controlCommand
  device_ref
  control_id
  capability_id
  command_type
  params
```

Readable or requestable state should use explicit capability state messages and
request/reply correlation through the Deckr envelope.

Typed convenience models may exist for common core capability values when they
are true specializations of the canonical message shape. They must not become
parallel wire contracts or aliases for old hardware messages.

The following are not v1 core wire message types:

- `keyDown`
- `keyUp`
- `dialRotate`
- `touchTap`
- `touchSwipe`
- `setImage`
- `clearSlot`
- `sleepScreen`
- `wakeScreen`

If the behavior is still needed, express it as a capability event type, command
type, value schema, or SDK helper over `controlInput` / `controlCommand`.

## Controller Binding

Controllers bind actions to controls and capabilities, not to slots.

Useful selectors include:

- exact control id
- capability family/type
- control kind
- geometry region
- group id
- parent control id
- labels or semantic tags when explicitly modeled

Selector resolution must be deterministic. Ambiguous matches must fail visibly.
Transport-local metadata must not participate in durable configuration matching.

Controller state remains keyed by controller-local config id and action instance
id, not raw device id.

This supports:

- same config for a device regardless of manager
- different config based on where a device is attached
- virtual devices with stable fingerprints
- controls selected by capability rather than grid coordinate
- output targeting based on available raster, text, indicator, brightness, or
  other output capabilities

## Plugin Model

Plugin-facing APIs are controller mediated.

Devices should know nothing about plugins. Plugins should know nothing about
concrete device protocols.

Plugin actions declare capability requirements, for example:

- requires a momentary input
- requires raster output
- can use encoder delta
- can use touch input
- can render text only
- can run on a host-rendered widget button

Plugin input delivery should use generic control/capability events. Old
Elgato-shaped callbacks such as `on_key_down`, `on_key_up`, `on_dial_rotate`,
`on_touch_tap`, and `on_touch_swipe` are not v1 compatibility APIs.

SDK helpers may exist when they are first-class capability APIs or thin
constructors over canonical Deckr messages. They must not expose alternate wire
contracts.

## Driver And Manager Boundary

Drivers and hardware managers translate concrete protocols into Deckr
descriptors and hardware messages.

USB and HID should influence descriptors, but they must not become the Deckr
model.

Useful source metadata includes:

- vendor id
- product id
- serial number
- interface number
- usage page
- usage
- report descriptor hash
- input, output, and feature report mappings
- raw report ids when needed by a manager

For devices with good HID descriptors, a generic HID manager may derive an
initial Deckr descriptor. For devices with vendor-specific reports, a driver or
layout file can provide the normalized descriptor.

Application code must not route or bind by HID path.

## V1 Minimum

Minimum v1 device descriptor:

- device id
- fingerprint
- display name
- manufacturer/model/version fields
- structured identifiers/connections
- optional parent device
- controls
- device-level capabilities

Minimum v1 control descriptor:

- control id
- kind
- label
- optional group/parent relation
- optional geometry
- input capabilities
- output capabilities
- state/config/diagnostic capabilities

Minimum v1 capability descriptor:

- capability id
- family/type
- direction
- access
- value schema or command schema
- constraints
- units where relevant
- source references

Minimum v1 core capability families:

- momentary button
- relative encoder
- touch tap/swipe compatible input
- raster output
- screen power
- brightness
- battery state
- diagnostic state

This covers today's Elgato, MiraBox, and MQTT drivers while making it clear how
to add faders, haptics, text displays, LEDs, browser controls, phone widgets,
and richer HID devices later.

## Hard Rules

- `slot` must not be the core device abstraction.
- `key`, `dial`, and `touch` are not the whole platform vocabulary.
- `hid` must not be a required core device field.
- Core descriptors must separate identity, topology, controls, capabilities,
  connection facts, and raw source metadata.
- Device descriptors must be owned by `deckr` as wire-safe contracts.
- Capabilities must declare direction, access, schemas, constraints, and units
  where relevant.
- Controllers bind actions to capabilities on controls, not to grid slots.
- Managers translate concrete protocols into Deckr descriptors and messages.
- Plugin APIs may offer ergonomic capability helpers, but the underlying Deckr
  protocol must support generic capability events and commands.
- Diagnostics and configuration exposes are first-class capabilities, but are
  not automatically bindable user controls.
- Derived gestures must not erase lower-level event data when the lower-level
  data is available and meaningful.
- Shared, overlapping, and compound controls must be representable with optional
  relation and geometry metadata, while still allowing simple flattened
  descriptors.
- USB/HID paths, MQTT topics, WebSocket sessions, browser sessions, and mobile
  widget runtime ids must not become durable Deckr device identity.
- Future hardware families must be additions to the descriptor/capability model,
  not new special cases in the core architecture.
- No aliases.
- No compatibility shims.
- No dual old/new hardware protocol.
- No old lane-name fallback.
- No old descriptor-name re-export.
- No SDK compatibility wrappers over removed alpha behavior.

If the implementation drifts from this model, fix the implementation. Do not
soften the architecture to accommodate accidental complexity.
