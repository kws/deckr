# Deckr Action Runtime v1 specification

Deckr core owns the Action Runtime service protocol.
This specification defines its service identity, availability view, and use of service agreements.

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

## Messages and schemas

The [Deckr message catalog](deckr-message-catalog.md#action-runtime) lists the Action Runtime messages and their payload schemas.
The [protocol declaration](../contract/v1/catalogs/action-runtime-protocol.v1.json)
defines operations, message directions, and view families.
The [schema guide](schema-guide.md#choosing-a-schema) lists message and availability entry points.
