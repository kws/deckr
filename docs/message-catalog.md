# Message catalog

This catalog enumerates current lane traffic. Full schemas validate envelopes and
payloads together; payload schemas can also be used independently. All messages
listed here require a Concord pointer. Semantic authorization remains mandatory.

## Hardware lane

All use `lane = hardware_messages`; the discriminator is `messageType`.

| Message type | Full message | Reusable body |
| --- | --- | --- |
| `controlInput` | [Schema](../contract/v1/schemas/deckr/hardware/messages/control-input.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/control-input-message.v1.schema.json) |
| `controlCommand` | [Schema](../contract/v1/schemas/deckr/hardware/messages/control-command.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/control-command-message.v1.schema.json) |
| `capabilityStateChanged` | [Schema](../contract/v1/schemas/deckr/hardware/messages/capability-state-changed.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/capability-state-changed-message.v1.schema.json) |
| `capabilityStateRequest` | [Schema](../contract/v1/schemas/deckr/hardware/messages/capability-state-request.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/capability-state-request-message.v1.schema.json) |
| `capabilityStateReply` | [Schema](../contract/v1/schemas/deckr/hardware/messages/capability-state-reply.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/capability-state-reply-message.v1.schema.json) |
| `commandAccepted` | [Schema](../contract/v1/schemas/deckr/hardware/messages/command-accepted.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/command-accepted-message.v1.schema.json) |
| `commandRejected` | [Schema](../contract/v1/schemas/deckr/hardware/messages/command-rejected.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/command-rejected-message.v1.schema.json) |
| `commandReply` | [Schema](../contract/v1/schemas/deckr/hardware/messages/command-reply.v1.schema.json) | [Body](../contract/v1/schemas/deckr/hardware/bodies/command-reply-message.v1.schema.json) |

Inputs and state changes report capability data. Control commands carry a named
command and parameters. State requests/replies and command acknowledgements,
rejections and replies retain device/capability identity. Request correlation is
carried by the envelope. Validate values and params against the selected capability
descriptor in addition to the body schema.

## Services lane

All use `lane = services`, `messageType = serviceMessage`.
The [generic service message](../contract/v1/schemas/deckr/services/message.v1.schema.json)
supports owner-defined names and `command`, `query`, `event`, or `notification`
intent. Exchange pattern is `one_way` or `request_reply`. A protocol declaration
specifies message directions, operations and params/result/event/error schemas.

Replies use the same service body and envelope correlation fields. Status is
`ok`, `rejected`, `unavailable`, or `error`. The owning service defines which
positions are required for its replies; no generic validator can infer an
arbitrary service's result schema from its namespace alone.

## Action Runtime

The service namespace is `dev.deckr.action_runtime.provider`. Every message is
one-way and direct, with a recipient session. The runtime endpoint is
`service:action-runtime.<providerInstanceId>`.

| `body.name` | Direction | Intent | Payload | Full message |
| --- | --- | --- | --- | --- |
| `action_instance_created` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/action-instance-lifecycle-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/action-instance-created.v1.schema.json) |
| `action_instance_destroyed` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/action-instance-lifecycle-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/action-instance-destroyed.v1.schema.json) |
| `binding_attached` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/binding-attached-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/binding-attached.v1.schema.json) |
| `binding_detached` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/binding-detached-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/binding-detached.v1.schema.json) |
| `page_session_opened` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/page-session-lifecycle-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/page-session-opened.v1.schema.json) |
| `page_session_closed` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/page-session-lifecycle-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/page-session-closed.v1.schema.json) |
| `capability_input` | controller to runtime | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/capability-input-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/capability-input.v1.schema.json) |
| `action_lifecycle_rejected` | runtime to controller | `event` | [body.event](../contract/v1/schemas/deckr/action-runtime/action-lifecycle-rejected-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/action-lifecycle-rejected.v1.schema.json) |
| `binding_output` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/binding-output-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/binding-output.v1.schema.json) |
| `binding_overlay` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/binding-overlay-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/binding-overlay.v1.schema.json) |
| `binding_overlay_clear` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/binding-overlay-clear-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/binding-overlay-clear.v1.schema.json) |
| `open_page` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/open-page-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/open-page.v1.schema.json) |
| `replace_page` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/replace-page-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/replace-page.v1.schema.json) |
| `close_page` | runtime to controller | `command` | [body.params](../contract/v1/schemas/deckr/action-runtime/empty-action-body.v1.schema.json) | [Schema](../contract/v1/schemas/deckr/action-runtime/messages/close-page.v1.schema.json) |

The [protocol declaration](../contract/v1/catalogs/action-runtime-protocol.v1.json)
also publishes each payload's schema reference. Events have empty `params`;
commands have no event payload. There are no reply/status/result/error payloads
on these one-way operations. `close_page` has an empty params object.

The [availability payload](../contract/v1/schemas/deckr/action-runtime/availability-payload.v1.schema.json)
contains action descriptors. The [stored availability view](../contract/v1/schemas/deckr/action-runtime/availability-view.v1.schema.json)
also requires the service/consumer session and contract fences. Settings identities,
settings snapshots, requirement selectors and page child bindings are reusable
payload definitions; they do not imply additional core message names.
