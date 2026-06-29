# Beacon & Concord Usage

Below is the lifecycle I would expect from the Python side. The important rule is:

```text
Beacon discovers candidates.
Concord governs authority.
A Concord contract is one live incarnation and should not be revived after authority is lost.
```

Deckr does not have separate discovery/authority systems for services,
hardware, actions, or future namespaces.

All runtime discovery uses Beacon.
All negotiated live authority uses Concord.
Domain-specific meaning is supplied by endpoint family, feature id, profile id,
payload schema, terms schema, and profile validation policy

The examples below are identical for services, hardware, actions, or 
future namespaces. So where we use service below, we use it in the most
generic sense, and there should not be specific paths for hardware or actions
as examples.

## Shared service protocol definition

Both the provider and the consumer need to agree on the same service protocol.

```python
from __future__ import annotations

import uuid
from collections.abc import Mapping

import anyio

from deckr.runtime import Deckr
from deckr.beacon import (
    BeaconAdvertisementSpec,
    BeaconFeatureEventType,
)
from deckr.concord import (
    ConcordAgreementSpec,
    ConcordConflict,
    ContractState,
    ContractValidityStatus,
    ConcordEventType,
)
from deckr.contracts.messages import EndpointAddress
from deckr.services import (
    SERVICE_LANE_CONTRACT,
    ServiceAdvertisementPayload,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceUseTerms,
    ServiceViewFamily,
    newest_service_descriptor,
    parse_service_descriptor,
    service_use_terms,
)
```

```python
CLOCK_PROTOCOL = ServiceProtocol(
    namespace="org.example.clock",
    feature_id="org.example.clock.service",
    advertisement_profile="org.example.clock.advertisement.v1",
    use_profile="org.example.clock.service_use.v1",
    operations=(
        "time.now",
        "time.watch",
    ),
    view_families={
        "status": ServiceViewFamily(
            storeName="org_example_clock_status_v1",
            keyPrefix="views.clock.status.",
        ),
    },
)
```

A service advertisement payload already has the important service identity fields: service id, service endpoint, namespace, service session id, use profile, supported operations, views, and backend status. The current `parse_service_descriptor()` path validates those fields against the protocol before a candidate is treated as a service descriptor.

---

# 1. Service publishes and maintains a Beacon advertisement

This is the **provider** side.

```python
class ClockService:
    def __init__(self, *, service_id: str = "clock-main") -> None:
        self.service_id = service_id
        self.service_endpoint = f"service:{service_id}"
        self.session_id = f"clock-session-{uuid.uuid4()}"

        self._beacon_lease = None
        self._contract_acceptor: ServiceContractAcceptor | None = None

    def _advertisement_payload(
        self,
        *,
        backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
        diagnostics: Mapping[str, object] | None = None,
    ) -> dict:
        payload = CLOCK_PROTOCOL.advertisement_payload(
            service_id=self.service_id,
            session_id=self.session_id,
            backend_status=backend_status,
            diagnostics=diagnostics or {},
        )
        return payload.to_dict()

    async def run(self, stop_event: anyio.Event) -> None:
        async with Deckr(
            lanes=("services",),
            lane_contracts=(SERVICE_LANE_CONTRACT,),
        ) as deckr:
            async with deckr.endpoint(
                self.service_endpoint,
                session_id=self.session_id,
            ):
                beacon = deckr.beacon
                concord = deckr.concord

                self._beacon_lease = await beacon.advertise(
                    BeaconAdvertisementSpec(
                        feature_id=CLOCK_PROTOCOL.feature_id,
                        endpoint=self.service_endpoint,
                        advertiser=self.service_endpoint,
                        session_id=self.session_id,
                        protocol={
                            "namespace": CLOCK_PROTOCOL.namespace,
                            "version": "1",
                        },
                        operations=CLOCK_PROTOCOL.operations,
                        labels={
                            "serviceId": self.service_id,
                            "namespace": CLOCK_PROTOCOL.namespace,
                        },
                        payload=self._advertisement_payload(),
                    ),
                    cleanup_stale_same_endpoint=True,
                )

                self._contract_acceptor = ServiceContractAcceptor(
                    concord=concord,
                    protocol=CLOCK_PROTOCOL,
                    service_id=self.service_id,
                    service_endpoint=self.service_endpoint,
                    service_session_id=self.session_id,
                )

                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._contract_acceptor.run)

                    try:
                        await stop_event.wait()
                    finally:
                        # First stop accepting new contracts.
                        #
                        # Existing Concord contracts remain governed by Concord;
                        # withdrawing Beacon only removes us from future discovery.
                        if self._beacon_lease is not None:
                            await self._beacon_lease.aclose()

                        # Then terminate active contracts and withdraw participant tokens.
                        if self._contract_acceptor is not None:
                            await self._contract_acceptor.aclose(
                                reason="service_shutdown"
                            )

                        tg.cancel_scope.cancel()
```

What keeps the Beacon advertisement alive?

The returned `BeaconAdvertisementLease` is the important object. Keeping it alive keeps the managed heartbeat loop alive. The branch’s Beacon lease has an internal heartbeat loop and `aclose()`/withdraw path. The docs describe the default Beacon TTL as 300 seconds, with managed refreshes scheduled around 150–225 seconds by default.

To update the advertisement because the service degraded:

```python
async def mark_degraded(self, reason: str) -> None:
    if self._beacon_lease is None:
        return

    await self._beacon_lease.update(
        payload=self._advertisement_payload(
            backend_status=ServiceBackendStatus.DEGRADED,
            diagnostics={"reason": reason},
        ),
        labels={
            "serviceId": self.service_id,
            "namespace": CLOCK_PROTOCOL.namespace,
        },
        operations=CLOCK_PROTOCOL.operations,
    )
```

If the service is not accepting new contracts but wants to keep existing contracts alive, it should withdraw Beacon but keep its Concord participant leases running.

```python
async def stop_accepting_new_contracts(self) -> None:
    if self._beacon_lease is not None:
        await self._beacon_lease.aclose()
        self._beacon_lease = None
```

---

# 2. Service discovers a Beacon advertisement based on criteria

This is the **consumer** side.

The hot path should use the materialised Beacon view:

```python
candidates = beacon.candidates(CLOCK_PROTOCOL.feature_id)
```

That uses the in-memory `_keys_by_feature` and `_entries_by_key` indexes, not a raw KV prefix scan.

```python
def service_descriptor_matches(
    descriptor: ServiceDescriptor,
    *,
    required_operations: set[str],
    required_view_families: set[str],
) -> bool:
    if descriptor.backend_status == ServiceBackendStatus.UNAVAILABLE:
        return False

    if not required_operations.issubset(descriptor.supported_operations):
        return False

    if not required_view_families.issubset(set(descriptor.views)):
        return False

    return True
```

```python
async def find_clock_service(
    deckr: Deckr,
    *,
    required_operations: set[str],
    required_view_families: set[str],
) -> ServiceDescriptor:
    beacon = deckr.beacon

    await beacon.wait_current()

    descriptors: list[ServiceDescriptor] = []

    for candidate in beacon.candidates(CLOCK_PROTOCOL.feature_id):
        descriptor = parse_service_descriptor(candidate, CLOCK_PROTOCOL)
        if descriptor is None:
            continue

        if service_descriptor_matches(
            descriptor,
            required_operations=required_operations,
            required_view_families=required_view_families,
        ):
            descriptors.append(descriptor)

    selected = newest_service_descriptor(descriptors)
    if selected is not None:
        return selected

    raise ServiceUnavailable(
        "no_candidate",
        "No matching clock service is currently advertised",
        {
            "featureId": CLOCK_PROTOCOL.feature_id,
            "operations": sorted(required_operations),
            "views": sorted(required_view_families),
        },
    )
```

A watch-based version is better when the caller is willing to wait:

```python
async def wait_for_clock_service(
    deckr: Deckr,
    *,
    required_operations: set[str],
    required_view_families: set[str],
    timeout_seconds: float = 10.0,
) -> ServiceDescriptor:
    beacon = deckr.beacon

    # First try the already-materialised snapshot.
    try:
        return await find_clock_service(
            deckr,
            required_operations=required_operations,
            required_view_families=required_view_families,
        )
    except ServiceUnavailable:
        pass

    with anyio.fail_after(timeout_seconds):
        async with beacon.watch(
            CLOCK_PROTOCOL.feature_id,
            replay_current=True,
        ) as events:
            async for event in events:
                if event.event_type not in {
                    BeaconFeatureEventType.ADVERTISED,
                    BeaconFeatureEventType.UPDATED,
                }:
                    continue

                if event.candidate is None:
                    continue

                descriptor = parse_service_descriptor(
                    event.candidate,
                    CLOCK_PROTOCOL,
                )
                if descriptor is None:
                    continue

                if service_descriptor_matches(
                    descriptor,
                    required_operations=required_operations,
                    required_view_families=required_view_families,
                ):
                    return descriptor

    raise ServiceUnavailable(
        "discovery_timeout",
        "Timed out waiting for a matching clock service",
    )
```

This is the shape I would expect a future `ServiceDirectory` helper to wrap: one Beacon watch, local indexes, and zero raw KV scans.

---

# 3. Service proposes a Concord contract

This is the **consumer** proposing service use.

The important part: `service_use_terms(...)` creates deterministic terms/scope material, but the Concord contract itself remains opaque and incarnation-specific.

```python
TERMINAL_DURING_NEGOTIATION = {
    ContractValidityStatus.CANCELLED,
    ContractValidityStatus.MISSING_CONTRACT,
    ContractValidityStatus.INVALID_CONTRACT,
    ContractValidityStatus.INVALID_TOKEN,
    ContractValidityStatus.MISSING_TOKEN,
    ContractValidityStatus.GENERATION_MISMATCH,
    ContractValidityStatus.SESSION_MISMATCH,
    ContractValidityStatus.TERMS_HASH_MISMATCH,
}
```

```python
async def propose_clock_service_use(
    deckr: Deckr,
    *,
    client_endpoint: str,
    client_session_id: str,
    descriptor: ServiceDescriptor,
    required_operations: set[str],
    required_view_families: set[str],
    task_group: anyio.abc.TaskGroup,
    timeout_seconds: float = 10.0,
) -> ServiceUseLease:
    terms = service_use_terms(
        descriptor,
        client_endpoint=client_endpoint,
        operations=required_operations,
        views=required_view_families,
    )

    agreement = await deckr.concord.propose(
        ConcordAgreementSpec(
            participants=(
                client_endpoint,
                str(descriptor.endpoint),
            ),
            local_participant=client_endpoint,
            local_session_id=client_session_id,
            profile=descriptor.use_profile,
            terms=terms,
            # Intentionally no deterministic contract id.
            #
            # The serviceUseScopeId inside terms is a scope/index key only,
            # not the Concord contract identity.
        ),
        # Starts the local participant token heartbeat loop.
        start_soon=task_group.start_soon,
    )

    with anyio.fail_after(timeout_seconds):
        while True:
            validity = await agreement.refresh()

            if validity.valid:
                return ServiceUseLease(
                    agreement=agreement,
                    descriptor=descriptor,
                    terms=terms,
                )

            if validity.status in TERMINAL_DURING_NEGOTIATION:
                await agreement.aclose()
                raise ServiceUnavailable(
                    f"contract_{validity.status.value}",
                    "Clock service-use contract became terminal during negotiation",
                    {
                        "status": validity.status.value,
                        "reason": validity.reason,
                        "contractId": agreement.contract.contract_id,
                        "generation": agreement.contract.generation,
                    },
                )

            # Expected while the provider has not attached its token yet.
            if validity.status == ContractValidityStatus.NOT_YET_FULFILLED:
                await anyio.sleep(0.25)
                continue

            if validity.status == ContractValidityStatus.UNAVAILABLE:
                await anyio.sleep(0.5)
                continue

            await anyio.sleep(0.25)

    await agreement.aclose()
    raise ServiceUnavailable(
        "contract_timeout",
        "Timed out waiting for clock service-use contract to become valid",
    )
```

The Concord contract becomes valid only when every named participant has attached an acceptable token. The protocol requires exact validation of the contract plus all participant tokens for strict authority.

A consumer using the service should refresh or validate before important work:

```python
async def use_clock_service(lease: ServiceUseLease) -> None:
    await lease.refresh()

    # Now send service commands or read protected views.
    # The service lane itself is ordinary message traffic;
    # Concord is the authority check, not the message carrier.
```

Consumer-side graceful shutdown:

```python
async def close_service_use(
    deckr: Deckr,
    lease: ServiceUseLease,
    *,
    client_endpoint: str,
    reason: str = "client_shutdown",
) -> None:
    # Cancellation is explicit terminal signalling.
    try:
        await deckr.concord.cancel(
            lease.contract,
            participant=client_endpoint,
            reason=reason,
        )
    finally:
        # Withdraw the local participant token / stop heartbeat.
        await lease.agreement.aclose()
```

---

# 4. Service registers for new contracts and accepts them

This is the **provider** accepting proposed contracts.

I would implement the acceptor directly over `Concord.watch(...)`. The current public API exposes `Concord.watch(profile=..., participant=..., replay_current=True)`, and the watch implementation replays current contract status then streams later Concord events.

```python
class ServiceContractAcceptor:
    def __init__(
        self,
        *,
        concord,
        protocol: ServiceProtocol,
        service_id: str,
        service_endpoint: str,
        service_session_id: str,
    ) -> None:
        self.concord = concord
        self.protocol = protocol
        self.service_id = service_id
        self.service_endpoint = service_endpoint
        self.service_session_id = service_session_id

        self._leases = {}
        self._closed = False

    async def run(self) -> None:
        async with self.concord.watch(
            profile=self.protocol.use_profile,
            participant=self.service_endpoint,
            replay_current=True,
        ) as events:
            async for event in events:
                if self._closed:
                    return

                await self._handle_event(event)

    async def _handle_event(self, event) -> None:
        contract = event.contract
        if contract is None:
            return

        if event.event_type in {
            ConcordEventType.CONTRACT_CANCELLED,
            ConcordEventType.CONTRACT_DELETED,
            ConcordEventType.CONTRACT_INVALID,
        }:
            await self._close_lease_for_contract(contract.key)
            return

        if contract.key in self._leases:
            return

        if event.record is None:
            return

        if event.record.state != ContractState.OPEN:
            return

        if not self._record_is_acceptable(event.record):
            return

        try:
            lease = await self.concord.attach(
                contract,
                participant=self.service_endpoint,
                session_id=self.service_session_id,
                # Optional. The token bucket TTL still governs the real cadence.
                refresh_interval=60.0,
                log_label="ClockService",
            )
        except ConcordConflict:
            # Another local acceptor may have attached, the contract may have
            # been cancelled, or the contract may no longer name this service.
            return

        self._leases[contract.key] = lease

    def _record_is_acceptable(self, record) -> bool:
        if record.profile != self.protocol.use_profile:
            return False

        if record.terms is None:
            return False

        participants = {str(item) for item in record.participants}
        if self.service_endpoint not in participants:
            return False

        try:
            terms = ServiceUseTerms.model_validate(record.terms)
        except ValueError:
            return False

        if terms.profile != self.protocol.use_profile:
            return False

        if terms.service_id != self.service_id:
            return False

        if str(terms.service_endpoint) != self.service_endpoint:
            return False

        # Critical stale-contract protection:
        # A restarted service has a new session id and must not accept old terms.
        if terms.service_session_id != self.service_session_id:
            return False

        if terms.service_namespace != self.protocol.namespace:
            return False

        unsupported_operations = set(terms.allowed_operations).difference(
            set(self.protocol.operations)
        )
        if unsupported_operations:
            return False

        for family, prefixes in terms.allowed_views.items():
            view_family = self.protocol.view_families.get(family)
            if view_family is None:
                return False

            for prefix in prefixes:
                if not prefix.startswith(view_family.key_prefix):
                    return False

        return True

    async def _close_lease_for_contract(self, contract_key: str) -> None:
        lease = self._leases.pop(contract_key, None)
        if lease is not None:
            await lease.aclose()

    async def aclose(self, *, reason: str = "service_shutdown") -> None:
        self._closed = True

        leases = list(self._leases.values())
        self._leases.clear()

        for lease in leases:
            try:
                await self.concord.cancel(
                    lease.contract,
                    participant=self.service_endpoint,
                    reason=reason,
                )
            finally:
                await lease.aclose()
```

What keeps the accepted contract alive?

`concord.attach(...)` creates or adopts this service participant’s token and returns a `ConcordParticipantLease`. In the current runtime, `Concord.attach()` adds the lease to the Concord instance and starts it if Concord has a task group, so keeping the lease alive keeps the participant token heartbeat alive.

The acceptor rejects stale proposals by checking:

```python
terms.service_session_id == self.service_session_id
```

That is important. If the service restarts, it gets a new session id. Old terms should not be accepted by the new process as if the old service state still exists.

**`ServiceContractAcceptor` is not currently a `deckr` core class.** That was my illustrative wrapper name for “the provider-side loop that watches Concord and attaches to acceptable contracts.”

The class I sketched could become a useful `deckr.services` helper, but I would not make it something services “extend” in the inheritance sense. I would prefer composition:

```python
acceptor = ServiceUseAcceptor(
    concord=deckr.concord,
    protocol=CLOCK_PROTOCOL,
    service_id="clock-main",
    service_endpoint="service:clock-main",
    service_session_id=session_id,
    accept=application_policy,
)

tg.start_soon(acceptor.run)
```

That keeps `deckr` responsible for protocol mechanics and lets each concrete service own domain policy: what terms it accepts, whether it has capacity, whether backend state is healthy, and what shutdown/drain behaviour it wants.

A good core helper would probably be named something like:

```text
ServiceUseAcceptor
ServiceUseParticipant
ConcordServiceUseAcceptor
```

and it would wrap these generic operations:

```text
Concord.watch(profile=..., participant=...)
validate ServiceUseTerms
Concord.attach(...)
hold ConcordParticipantLease objects
cancel/withdraw leases on shutdown
```

It should not be a base class unless there is a strong reason. The service should implement policy callbacks, not subclass protocol machinery.

---

# Full consumer flow

```python
class ClockConsumer:
    def __init__(self, *, endpoint_id: str = "clock-client-main") -> None:
        self.client_endpoint = f"service:{endpoint_id}"
        self.client_session_id = f"clock-client-session-{uuid.uuid4()}"

    async def run(self, stop_event: anyio.Event) -> None:
        async with Deckr(
            lanes=("services",),
            lane_contracts=(SERVICE_LANE_CONTRACT,),
        ) as deckr:
            async with deckr.endpoint(
                self.client_endpoint,
                session_id=self.client_session_id,
            ):
                async with anyio.create_task_group() as tg:
                    lease: ServiceUseLease | None = None

                    try:
                        descriptor = await wait_for_clock_service(
                            deckr,
                            required_operations={"time.now"},
                            required_view_families={"status"},
                            timeout_seconds=10.0,
                        )

                        lease = await propose_clock_service_use(
                            deckr,
                            client_endpoint=self.client_endpoint,
                            client_session_id=self.client_session_id,
                            descriptor=descriptor,
                            required_operations={"time.now"},
                            required_view_families={"status"},
                            task_group=tg,
                            timeout_seconds=10.0,
                        )

                        while not stop_event.is_set():
                            await lease.refresh()

                            # Send service commands or read protected views here.
                            await anyio.sleep(5.0)

                    finally:
                        if lease is not None:
                            await close_service_use(
                                deckr,
                                lease,
                                client_endpoint=self.client_endpoint,
                                reason="consumer_shutdown",
                            )

                        tg.cancel_scope.cancel()
```

---

## Lifecycle summary

Provider startup:

```text
open endpoint session
publish Beacon advertisement
start Concord watch for service-use profile + service endpoint
accept acceptable proposed contracts
maintain Beacon heartbeat
maintain Concord participant-token heartbeats
```

Consumer startup:

```text
open endpoint session
read Beacon materialised candidates
select service descriptor
build service-use terms
propose opaque Concord contract
maintain local participant-token heartbeat
wait for provider token
use service only while Concord validates
```

Provider shutdown:

```text
withdraw Beacon ad first
optionally allow existing contracts to drain
cancel/close active Concord contracts
withdraw provider participant tokens
close endpoint session
```

Consumer shutdown:

```text
cancel service-use contract
withdraw local participant token
close endpoint session
```

The architectural invariant is: **Beacon disappearance only affects future discovery; existing service authority is Concord-governed.** 
