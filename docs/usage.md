# Using the contract-first reference

For a contract change, first define the schema and observable behavior here,
update the catalogs and conformance examples, and validate the bundle. Consumers
implement the accepted contract in their own repositories against a pinned revision.

Start with [schema consumption](schemas.md), then select a complete schema from
the [message catalog](message-catalog.md) or [schema index](../contract/v1/index.html).
Load all dependencies into an offline Draft 2020-12 registry. Follow the profile
and authority checks after structural validation.

For a service implementation, use the reusable service protocol-definition,
advertisement-payload, service-message-body and view-record schemas. Publish an
owner-controlled protocol catalog with operations, message directions, payload
schemas and view families. [Action Runtime's catalog](../contract/v1/catalogs/action-runtime-protocol.v1.json)
is a complete example. The catalog is a reference declaration, not a new lane message.

For a hardware implementation, publish device/control/capability descriptors
inside a hardware Beacon advertisement, negotiate a hardware claim and validate
all hardware lane traffic against the exact contract and device references.
