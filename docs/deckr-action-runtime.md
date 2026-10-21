# Deckr Action Runtime v1 specification

Deckr core owns the Action Runtime service protocol.
This specification defines its service identity, availability view, service
agreements, page commands, and capability requirement matching.

Action Runtime uses the [Deckr services specification](deckr-services.md)
for service-use agreements, protected views, and managed lifecycles.
See the [schema guide](schema-guide.md) and
[semantic validation rules](semantic-rules.md#action-runtime) for validation requirements.

## Action Runtime service

The action runtime service protocol is `dev.deckr.action_runtime.provider`.

The service id must be `action-runtime.<providerInstanceId>`. The endpoint must be `service:<serviceId>`.
The provider publishes action availability through the contract-fenced
`action_availability` view.

A controller gets authority through the service-use contract. It then reads the contract-fenced `action_availability` view and sends runtime lifecycle messages on the `services` lane. Individual control bindings are controller-owned routing state.

After negotiation, Beacon is not part of the service-use contract lifecycle or validity. Package owners specify service-specific domain semantics and view schemas.

## Message subjects

Every Action Runtime message must use the
[service subject](../contract/v1/schemas/deckr/action-runtime/service-subject.v1.schema.json).
Its `kind` is `service`. Its identifiers are `serviceId`, `namespace`, and `name`.
They must identify the runtime service, `dev.deckr.action_runtime.provider`,
and `body.name`, respectively. The service ID must agree with the runtime
endpoint in the message.

The subject identifies the service operation. The payload identifies the action
instance, binding, or page session. A context subject is not permitted.
These rules apply in both directions. They do not change the subject rules
for other service protocols.

## Page commands

### Targets and authority

Page commands carry a required `target` in `body.params`.
The target identifies existing controller state:

| Command | Permitted target | Other required data |
| --- | --- | --- |
| `open_page` | A binding target or page-session target. | `descriptor` for the new page. |
| `replace_page` | A page-session target. | `descriptor` for the replacement contents. |
| `close_page` | A page-session target. | None. |

A [binding target](../contract/v1/schemas/deckr/action-runtime/binding-command-target.v1.schema.json)
contains `kind = binding`, `bindingId`, and `generation`.
Its generation must equal the binding's current `outputGeneration`.
A [page-session target](../contract/v1/schemas/deckr/action-runtime/page-session-command-target.v1.schema.json)
contains `kind = page_session`, `pageSessionId`, and `generation`.
Its generation must equal the page session's current `generation`.
The two generation counters are independent of the Concord contract generation.

For example, a close command has these parameters:

```json
{
  "target": {
    "kind": "page_session",
    "pageSessionId": "page-session-1",
    "generation": 2
  }
}
```

The controller must resolve the target in the recipient controller session.
It must check the service-use contract and both endpoint sessions. It must also
check that the target belongs to that runtime and agreement. An action instance
or binding from an earlier agreement does not become valid under a successor.

The controller must use its retained target state to find the action instance,
configuration, device, and claim scope. The sender cannot supply a new scope
through the target or page descriptor. All child controls must exist on that
device in the held claim. `self` refers to the opener's retained action instance.
An `action` child uses the existing action/provider selection rules.

The target must be active in its controlled scope. A hidden page session, or
a binding on a hidden page, cannot change the current page. Controller policy
can refuse a page transition, including a nested open. A refusal must leave the
page state unchanged. These one-way commands do not add a reply message.

The controller must check target identity, generation, scope, and authority again
when it commits the transition. It must serialize conflicting transitions.
A queued command that no longer passes those checks must have no effect.
It must not select another page or use a successor agreement instead.

### Page-session lifecycle

On a successful open, the controller allocates a new `pageSessionId` and
`contextId`. The new page session starts at generation zero.
It retains the opener's action identity, runtime ownership, agreement, and device
scope. The controller reports the session through `page_session_opened`.
An opener binding need not remain visible while that page session is active.

An open from a page-session target has the same ownership and scope checks.
Controller policy determines page placement and which existing sessions remain.
The controller must report any sessions that it closes. A new session must
retain its own ownership and scope after the transition.

A successful replacement keeps the page-session ID, context ID, page ID, and
owner. Its descriptor's `pageId` must equal the retained page ID.
It replaces the child bindings and increases the page-session generation by one.
The controller then sends `page_session_opened` with the updated metadata.
This event reports the current session snapshot after either opening or
replacement. Providers must use the reported generation for later commands.
Binding lifecycle messages report the affected child bindings.

A successful close retires the identified session and reports
`page_session_closed` with its final generation. Later commands for that session
must have no effect. Page restoration is controller policy. Closing one session
must not close an unrelated session in another controlled scope.

Binding and page-session IDs must not be reused within a controller session. A controller
must retire page sessions when their service-use authority or required hardware
claim is lost. A reconnect does not restore those sessions.

## Capability requirement matching

### Declared requirements and scope

An action descriptor can contain named
[requirements](../contract/v1/schemas/deckr/action-runtime/capability-requirement.v1.schema.json).
Requirement names must be unique within the action.
An absent `requirements` field, `null`, and an empty array all mean that the
action declares no capability requirements. In each case, the binding has no
matched capabilities. The controller must not add implicit matches.
Actions that need capabilities must declare them.

The controller chooses a device and control according to application policy.
It then matches requirements within that control's configured capability scope.
That scope can restrict candidates, but it must not invent capabilities.
Candidates must occur in the negotiated descriptor and in the bound control.
All five control capability collections can supply candidates.

Requirements select existing capabilities. They do not create projections or
give permission to cross a control, device, or claim boundary.
A projection source can be on another control or on the device itself, but it
must resolve within the same negotiated device descriptor.

### Eligibility and preference order

Apply these steps separately to each requirement:

1. Treat an absent `required` field as `true`.
2. Examine `preferences` in array order.
3. For each selector, require all specified non-null identity fields to match.
   These fields are `capabilityId`, `family`, `type`, and `direction`.
4. Require every event name and command name listed by the selector and the
   requirement to occur in the capability descriptor. Combine the two lists as
   sets. Absent or empty lists add no restriction.
5. Apply the requirement's view filter.
6. Use the first selector with an eligible candidate. Controller policy chooses
   among candidates that satisfy that selector. It must not choose a later
   selector while an earlier selector has an eligible candidate.

Listed event and command names mean that all names are required. They are not
alternatives. Use separate preferences for alternatives. Name-list order has
no meaning. Duplicate entries in these lists are invalid.

If no candidate is eligible for a required requirement, the controller must not
attach the binding. If no candidate is eligible for an optional requirement,
omit that match. If a candidate is eligible, select one match for the requirement.
The controller may select the same capability for more than one requirement.
One requirement does not reserve or remove a candidate from another requirement.

### Views and provenance

An absent or empty `views` list permits all views. A nonempty list accepts a
capability if at least one listed view applies. The view conditions can
overlap:

| View | Eligibility |
| --- | --- |
| `raw` | The descriptor has no `projection` metadata, whether its family is core or extension. |
| `native` | The family is core and the descriptor has no `projection` metadata. |
| `projected` | The descriptor has `projection.type = projection`. An absent type has this meaning. |
| `derived` | The descriptor has `projection.type = derivation`. |
| `extension` | The family is outside `dev.deckr.*`, with or without projection metadata. |

For example, an extension capability with a projection meets both `extension`
and `projected`. A `raw` view does not bypass descriptor or payload validation.
No view grants access to an undeclared hardware value.

Each match reports one `provenance` value:

- Use `projection` or `derivation` when projection metadata exists.
- Otherwise, use `native` for a core family.
- Otherwise, use `extension`.

For a projection or derivation, `source` must identify the descriptor's exact
source capability, with the same device reference. An absent or null source
control ID means a device-level capability. For other matches, `source` must be absent or
null. Source references must resolve; they do not grant an additional match.

### Match results

Every [matched capability](../contract/v1/schemas/deckr/action-runtime/matched-capability.v1.schema.json)
must name its requirement in `requirementName`.
There must be exactly one result for each satisfied requirement and no result
for an undeclared requirement. Duplicate requirement names in results are invalid.
The selected capability reference must identify the bound control and device,
including any fingerprint. Its family, type, and direction must agree with the
descriptor. Its event and command lists report the descriptor's complete name
sets; absent lists mean empty sets. Their order has no meaning.

Use requirement declaration order for `matchedCapabilities`.
Multiple names can refer to one capability. This must not cause one received
capability input message to be delivered more than once to the same binding.
Capability name lists describe support; they do not replace message authority,
scope, generation, or payload checks.

The matching rules define valid results. They do not prescribe a shared matcher
API, device placement, or a tie-breaking algorithm for equally eligible candidates.

## Alpha compatibility

The Action Runtime contract is in alpha development and has no release version.
Consumers must pin the same reviewed revision to use the same contract.
Page commands require explicit targets and the runtime service subject.
Page-session metadata requires `generation`. Each matched capability
requires a non-null `requirementName`. Name and view lists reject duplicates.
Absent, null, and empty requirements do not permit implicit matches.

The [rendering contract](deckr-action-runtime-rendering.md) defines optional
support in binding attachment, `binding_render`, and `binding_render_result`.
Attachment schemas from revisions without rendering reject the support field.
Coordinate specification revisions before using rendering. The hardware raster
command remains an encoded frame.

## Messages and schemas

The [Deckr message catalog](deckr-message-catalog.md#action-runtime) lists the Action Runtime messages and their payload schemas.
The [protocol declaration](../contract/v1/catalogs/action-runtime-protocol.v1.json)
defines operations, message directions, and view families.
The [schema guide](schema-guide.md#choosing-a-schema) lists message and availability entry points.
