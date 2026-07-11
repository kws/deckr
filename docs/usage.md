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
negotiation, request authority, and view fencing as one managed service context.

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
from deckr.services import (
    ServiceDescriptor,
    ServiceUnavailable,
    newest_service_descriptor,
)


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
                    predicate=usable_presence_service,
                    select=newest_service_descriptor,
                    timeout_seconds=30.0,
                ) as lease:
                    reply = await services.request(
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
states matching policy and an optional selector. Predicates and selectors must
be side-effect-free because the directory can evaluate them repeatedly while it
waits for a current snapshot and future Beacon changes. The returned lease is
valid only inside the context. If service-use authority is lost, the managed API
raises `ServiceUnavailable` or a service-message response reports the
service-domain error.

The managed directory always handles both startup cases: an advertisement can
already exist before the consumer starts, or it can be created later. Internally
the directory first materializes the current Beacon snapshot, then waits for
subsequent feature changes. Consumers should therefore use `use_matching(...)`
or `wait_for_descriptor(...)` instead of subscribing to raw Beacon events and
racing the initial snapshot.

Low-level service infrastructure that needs to maintain candidate bookkeeping
can watch current descriptor snapshots and diff them locally:

```python
def descriptor_key(descriptor: ServiceDescriptor) -> tuple[str, str, str]:
    return (
        descriptor.service_id,
        str(descriptor.endpoint),
        descriptor.session_id,
    )


async def watch_presence_candidates(services) -> None:
    directory = services.directory(EXAMPLE_PROTOCOL)
    previous: dict[tuple[str, str, str], ServiceDescriptor] = {}

    async for snapshot in directory.watch_records():
        current = {
            descriptor_key(descriptor): descriptor
            for descriptor in snapshot
            if usable_presence_service(descriptor)
        }
        added = current.keys() - previous.keys()
        removed = previous.keys() - current.keys()
        updated = {
            key
            for key in current.keys() & previous.keys()
            if current[key] != previous[key]
        }

        for key in sorted(added | updated):
            await consider_candidate(current[key])

        for key in sorted(removed):
            await forget_candidate(previous[key])

        previous = current
```

The first yielded snapshot represents the already-current Beacon state, so
entries in that first `added` set are not special or stale. A later `added` or
`updated` descriptor is a candidate for a new service-use negotiation. A
`removed` descriptor should only remove discovery bookkeeping; it must not
cancel or invalidate an already-held service-use lease. Existing lease authority
continues to be governed by Concord validity and participant tokens.

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
                    predicate=usable_presence_service,
                    timeout_seconds=30.0,
                ) as lease:
                    view = status_view(lease.descriptor.service_id, person_id)
                    async for payload in services.watch_view(lease, view):
                        await render_status(payload)
```

`read_view(...)` returns one fenced payload or `None`. `None` is an ordinary
absent-view value under the active service-use lease, not a signal that the
lease should close. `watch_view(...)` yields the current payload first and then
subsequent changes. The managed client refreshes the lease before delivering
watched changes.

## Managed Subscriptions

Long-lived feature code should normally use a domain service client that wraps
service-use leases in a logical subscription session. The session exposes
resource-state messages instead of Concord lifecycle mechanics.

A resource session owns a retained set of service resources under a live
Concord service-use contract. That retained set is both the authoritative state
set and the operation authority boundary for the session: views are authoritative
only for retained resources, and resource-scoped requests must go through the
active session and target a retained resource.

```python
from deckr.services import ServiceSubscriptionState


async with sonos.zone_subscription_session(
    "sonos-home",
    zones={"Kitchen"},
) as session:
    async for message in session.messages:
        if message.state is ServiceSubscriptionState.READY:
            await render_zone(message.resource, message.payload)
        elif message.state is ServiceSubscriptionState.RECONNECTING:
            break
        elif message.state is ServiceSubscriptionState.UNAVAILABLE:
            await render_unavailable(message.resource)
        elif message.state is ServiceSubscriptionState.ERROR:
            await render_error(message.resource, message.error)
            break
```

Ordinary feature code must not read service views, watch service views, or send
service messages unless it is doing so through an active service session. For a
resource-bound action, open the domain session at the scope that owns the
resource. In the Python action SDK, component-scoped service clients and caches
belong in `started(context)` and component tasks; binding-specific zone/item
subscriptions belong in `mounted(context)` and `context.tasks`, and close when
that binding unmounts.

Operation requests for a retained resource should use that same session:

```python
class SonosPlayButton(DeckrAction):
    async def started(self, context) -> None:
        del context
        self._session = None
        self.tasks.start_soon(self._run_sonos_session)

    async def _run_sonos_session(self) -> None:
        try:
            async with sonos.zone_subscription_session(
                "sonos-home",
                zones={self.zone_name},
            ) as session:
                self._session = session
                async for message in session.messages:
                    await render_connected_state(message)
        finally:
            self._session = None

    async def input(self, context, event) -> None:
        if self._session is None:
            await context.overlay("unavailable", title="UNAVAILABLE")
            return
        await self._session.request("play", {"zone": self.zone_name})
```

Normal feature input should not send resource requests while the corresponding
resource state is `PENDING`, `UNAVAILABLE`, `RECONNECTING`, or `ERROR`. Render
the pending, unavailable, reconnecting, or error state instead and wait for a
fresh `READY` message before accepting ordinary resource requests.

`ServiceSubscriptionState` is the shared state vocabulary:

- `PENDING`: requested, but no fresh authoritative payload is available yet.
- `READY`: payload is current under the active service-use contract.
- `UNAVAILABLE`: the service or fenced view reports the resource unavailable,
  including an absent fenced view under an otherwise usable lease.
- `RECONNECTING`: the previous service-use contract ended and a successor is
  being negotiated.
- `ERROR`: an unclassified failure was surfaced to the subscription manager.

Feature code should use `message.state`; it should not infer lifecycle from
`message.payload is None`.

Managers reconnect by default after terminal service-use loss because a logical
session may share one provider-scoped lease with other sessions. Feature code
that does not want to wait for a successor lease should exit the context
manager when it receives a state message it treats as terminal, such as
`RECONNECTING` or `ERROR`.

Service clients build these sessions with `SharedResourceSubscriptionManager`.
The manager owns descriptor resolution, service-use negotiation, retained
resource union, resource-scope updates, view watching, fanout, reconnect, and
best-effort cleanup. Domain clients provide callbacks for resource identity,
resource scope mutation, view refs, and payload-to-message mapping. Domains
whose wire API treats scope as full replacement should use `set_resources(...)`
and `ResourceSubscriptionSession.set(...)`; domains with additive external APIs
can still use `ensure_resources(...)` and `release_resources(...)`.

## Low-Level Lease Loss Helpers

Low-level service infrastructure can still classify terminal service-use loss
with the shared helpers instead of maintaining terminal-code sets. Those helpers
are for core subscription managers and provider/runtime infrastructure that is
directly responsible for negotiating successor service-use contracts.

Ordinary action and display code should consume domain service-client APIs:
managed subscription sessions expose lifecycle through `session.messages`, and
requests return ordinary service-domain replies. Consumers should not import
lease-loss helpers or treat missing payloads as lifecycle authority.

## Provider Boundary

Service providers still use lower-level runtime infrastructure to advertise,
accept service-use contracts, authorize incoming service messages, and publish
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

## Maintenance Infrastructure

Concord maintenance is separate from the normal runtime. Import it from
`deckr.concord_maintenance`, construct it from the three exact/raw stores, and
either call `scan_once()` explicitly or let the built-in lane-less reaper
component own `ConcordReaperService.run()`. Constructing
`ConcordMaintenance` or `ConcordReaperService` starts no task or watch, and
ordinary callers do not pass either object a task group.

Retained deletion persists a maintenance cleanup marker before removing the
contract record. `scan_once()` makes bounded immediate token-cleanup attempts;
if they cannot complete, `token_cleanups_pending` remains non-zero and a later
scan resumes from the marker even though the contract no longer exists.

This complete memory-store example deletes one cancelled contract whose
retention period has elapsed and verifies the persisted result without sleeping:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio

from deckr.concord import ContractRecord, ContractState, concord_contract_key
from deckr.concord_maintenance import (
    ConcordMaintenance,
    ConcordReaperConfig,
    ConcordReaperService,
)
from deckr.testing import MemoryJsonKvBucket


async def maintenance_example() -> None:
    contract_store = MemoryJsonKvBucket(bucket="contracts")
    token_store = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
    maintenance_store = MemoryJsonKvBucket(bucket="maintenance")
    maintenance = ConcordMaintenance(
        contract_store,
        token_store,
        maintenance_store,
    )

    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    key = concord_contract_key(contract_id="retained-contract", generation=1)
    await contract_store.create(
        key,
        ContractRecord(
            contractId="retained-contract",
            generation=1,
            participants=("controller:maintenance-example",),
            attachedParticipants=(),
            state=ContractState.CANCELLED,
            createdBy="controller:maintenance-example",
            createdAt=now - timedelta(minutes=2),
            cancelledBy="controller:maintenance-example",
            cancelledAt=now - timedelta(minutes=1),
            cancelRevision=1,
            cancelReason="example cleanup",
        ),
    )

    reaper = ConcordReaperService(
        maintenance,
        config=ConcordReaperConfig(
            staleGraceSeconds=900,
            cancelledRetentionSeconds=0,
            scanIntervalSeconds=60,
        ),
        clock=lambda: now,
    )
    result = await reaper.scan_once()

    assert result.contracts_deleted == 1
    assert result.token_cleanups_completed == 1
    assert result.token_cleanups_pending == 0
    assert await contract_store.get(key) is None


if __name__ == "__main__":
    anyio.run(maintenance_example)
```

The memory buckets above are test/example dependencies. The built-in reaper
receives the canonical contract, token, and maintenance stores through a typed
core-owned factory. Generic component KV access rejects those reserved bucket
names.
