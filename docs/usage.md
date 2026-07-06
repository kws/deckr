# Managed Service-Use Usage

Deckr service consumers should use the managed service-use API in
`deckr.services`. Beacon and Concord remain the runtime protocols underneath
that API:

```text
Beacon discovers service candidates internally.
Concord governs service-use authority internally.
deckr.services exposes the consumer lifecycle.
Plugins express domain intent only.
```

Ordinary feature clients, such as OpenHAB, Sonos, Kaj, and future service
consumers, should not classify Concord validity statuses, inspect terminal
contract codes, or build service-use contracts directly. Treat discovery,
negotiation, command authority, and view fencing as one managed service context.

Direct Beacon and Concord primitives are implementation-level APIs for core
runtime code, service or hardware infrastructure, and conformance tests. Their
normative semantics live in [beacon-concord.md](beacon-concord.md).

## Service Protocol Definition

Service families publish a small shared protocol description. Both service
providers and service consumers import the same `ServiceProtocol`.

```python
from __future__ import annotations

from collections.abc import Mapping

from deckr.services import (
    ServiceBackendStatus,
    ServiceProtocol,
    ServiceViewFamilyDefinition,
)


EXAMPLE_PROTOCOL = ServiceProtocol(
    namespace="org.example.presence",
    feature_id="org.example.presence",
    advertisement_profile="org.example.presence.advertisement.v1",
    use_profile="org.example.presence.service_use.v1",
    operations=("presence.report",),
    view_families={
        "status": ServiceViewFamilyDefinition(
            storeName="org_example_presence_status_v1",
        ),
    },
)


def example_advertisement_payload(
    *,
    service_id: str,
    session_id: str,
    backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
    diagnostics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return EXAMPLE_PROTOCOL.advertisement_payload(
        service_id=service_id,
        session_id=session_id,
        backend_status=backend_status,
        diagnostics=diagnostics or {},
    ).to_dict()
```

The service advertisement payload includes service id, namespace, service
session id, use profile, supported operations, views, and backend status. The
managed service client parses and validates these fields before a candidate is
usable.

## Consumer Context

Create the runtime with the `services` lane enabled, open the consumer endpoint,
then ask the runtime for a managed service context bound to that endpoint.

```python
from deckr.contracts.lanes import SERVICE_LANE_CONTRACT
from deckr.contracts.messages import SERVICES_LANE
from deckr.runtime import Deckr
from deckr.services import ServiceDescriptor, ServiceUnavailable


def usable_presence_service(descriptor: ServiceDescriptor) -> bool:
    return (
        descriptor.backend_status != "unavailable"
        and "presence.report" in descriptor.supported_operations
        and "status" in descriptor.views
    )


async def report_presence() -> None:
    async with Deckr(
        lane_contracts=(SERVICE_LANE_CONTRACT,),
        lanes=(SERVICES_LANE,),
    ) as deckr:
        async with deckr.endpoint("action_provider:org.example.client") as endpoint:
            async with deckr.services(endpoint) as services:
                async with services.use_matching(
                    EXAMPLE_PROTOCOL,
                    operations={"presence.report"},
                    predicate=usable_presence_service,
                    timeout_seconds=30.0,
                ) as lease:
                    reply = await services.command(
                        lease,
                        "presence.report",
                        {"state": "home"},
                        timeout_seconds=8.0,
                    )
                    if reply.status != "ok":
                        raise ServiceUnavailable(
                            reply.error.code if reply.error else "service_rejected",
                            reply.error.message if reply.error else "Service rejected",
                            dict(reply.error.diagnostics) if reply.error else {},
                        )
```

`use_matching(...)` owns discovery and service-use negotiation. The caller
states required operations, views, matching policy, and an optional selector.
The returned lease is valid only inside the context. If service-use authority is
lost, the managed API raises `ServiceUnavailable` or a service command reply
reports the service-domain error.

## Reading And Watching Views

Views are read through the same lease that authorized them. The client does not
need to inspect the contract pointer or the storage fence.

```python
from deckr.services import ServiceViewRef


def status_view(service_id: str, person_id: str) -> ServiceViewRef:
    return ServiceViewRef(
        "org_example_presence_status_v1",
        f"views/{service_id}/status/{person_id}",
    )


async def watch_presence_status(person_id: str) -> None:
    async with Deckr(
        lane_contracts=(SERVICE_LANE_CONTRACT,),
        lanes=(SERVICES_LANE,),
    ) as deckr:
        async with deckr.endpoint("action_provider:org.example.client") as endpoint:
            async with deckr.services(endpoint) as services:
                async with services.use_matching(
                    EXAMPLE_PROTOCOL,
                    views={"status"},
                    predicate=usable_presence_service,
                    timeout_seconds=30.0,
                ) as lease:
                    view = status_view(lease.descriptor.service_id, person_id)
                    async for payload in services.watch_view(lease, view):
                        await render_status(payload)
```

`read_view(...)` returns one fenced payload or `None`. `watch_view(...)` yields
the current payload first and then subsequent changes. The managed client
refreshes the lease before delivering watched changes.

## Handling Lost Service Use

Feature code should classify service-use loss with the shared helpers instead
of maintaining terminal-code sets.

```python
from deckr.services import (
    ServiceUnavailable,
    service_command_reply_ends_service_use,
    service_unavailable_ends_service_use,
)


async def call_with_successor_retry(services, lease) -> None:
    try:
        reply = await services.command(lease, "presence.report", {"state": "away"})
    except ServiceUnavailable as exc:
        if service_unavailable_ends_service_use(exc):
            return await negotiate_successor_service_use()
        raise

    if service_command_reply_ends_service_use(reply):
        return await negotiate_successor_service_use()

    if reply.status != "ok":
        await render_unavailable(reply.error)
```

The helpers own classification for cancelled contracts, missing contracts,
missing or invalid participant tokens, session or generation mismatches, and
service-side `contract_not_managed` reports. Plugins should not duplicate those
rules.

## Provider Boundary

Service providers still use lower-level runtime infrastructure to advertise,
accept service-use contracts, authorize incoming service commands, and publish
fenced views. That code is service infrastructure, not ordinary feature-client
code.

Provider implementations should follow these boundaries:

- Beacon advertisement identifies candidates for future managed service-use
  negotiation only.
- Concord contract creation, cancellation, and participant-token validity are
  the only lifecycle authority.
- A cancelled or stale service-use contract is never resumed or reattached.
- Provider startup must clean stale contracts it owns before treating the
  service as available.
- Public plugin/client APIs should expose domain operations and views, not
  Beacon or Concord mechanics.

For protocol semantics, stale contract cleanup, cancellation rules, and
participant-token behavior, use [beacon-concord.md](beacon-concord.md) and
[nats-bus.md](nats-bus.md) as the implementation references.
