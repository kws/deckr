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
payload schema, terms schema, and profile validation policy.

## Breaking-change posture

This document describes the target shape for an intentional breaking change.
The goal is to remove the old service-specific discovery and service-use index
paths, not to keep them working beside the new generic path.

Do this in one coherent migration branch:

```text
add BeaconDirectory
move callers to BeaconDirectory and direct Concord proposals
delete ServiceDirectory, ServiceResolver, and service-use index APIs
delete stale tests and docs for the removed APIs
```

It is fine for intermediate commits in that branch to be temporarily broken.
The merged result should be workable, tested, and internally consistent. Do not
add compatibility aliases, deprecated wrappers, dual lookup paths, feature
flags, or fallback scans just to keep old clients running during the migration.
Downstream clients and cross-language mirrors should move with the contract.

The `BeaconDirectory` API used below is the target API this branch creates. The
Beacon advertisement lease, direct Concord proposal flow, and
`Concord.participant(...)` provider flow are already the intended shape; the
breaking work is to make discovery generic and remove the service-specific
authority shortcuts around it.

The examples below are identical for services, hardware, actions, or
future namespaces. So where we use service below, we use it in the most
generic sense, and there should not be specific paths for hardware or actions
as examples.

## Shared example service feature definition

Both the advertiser and the watcher need to agree on the same Beacon feature id
and payload profile. This document deliberately uses one small fictitious
presence service so the examples demonstrate the Beacon and Concord lifecycle,
not the domain-specific work the service performs.

```python
from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping

import anyio

from deckr.runtime import Deckr
from deckr.beacon import BeaconAdvertisementSpec, BeaconDirectory
from deckr.concord import (
    ConcordAgreementSpec,
    ConcordConflict,
    ContractValidityStatus,
)
from deckr.services import (
    SERVICE_LANE_CONTRACT,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceUseTerms,
    ServiceViewFamilyDefinition,
    newest_service_descriptor,
    parse_service_descriptor,
    service_use_terms,
    service_view_prefix,
)
```

```python
EXAMPLE_FEATURE_ID = "org.example.presence"
EXAMPLE_ADVERTISEMENT_PROFILE = "org.example.presence.advertisement.v1"
EXAMPLE_USE_PROFILE = "org.example.presence.service_use.v1"
EXAMPLE_OPERATIONS = ("presence.report",)


EXAMPLE_PROTOCOL = ServiceProtocol(
    namespace="org.example.presence",
    feature_id=EXAMPLE_FEATURE_ID,
    advertisement_profile=EXAMPLE_ADVERTISEMENT_PROFILE,
    use_profile=EXAMPLE_USE_PROFILE,
    operations=EXAMPLE_OPERATIONS,
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

A Beacon advertisement has generic envelope fields, such as feature id,
endpoint, session id, labels, operations, and protocol hints. Domain-specific
meaning belongs in the payload and its profile. A service advertisement payload,
for example, adds service id, namespace, service session id, use profile,
supported operations, views, and backend status; `parse_service_descriptor()`
validates those fields before a candidate is treated as a service descriptor.

---

# 1. Feature publishes and maintains a Beacon advertisement

This is the **advertiser** side. It uses a `service:<id>` endpoint only because
Deckr endpoint addresses must use one of the core endpoint families; Beacon does
not care which domain owns the payload.

```python
class ExampleAdvertiser:
    def __init__(self, *, service_id: str = "presence-main") -> None:
        self.service_id = service_id
        self.endpoint = f"service:{service_id}"
        self.session_id = f"presence-session-{uuid.uuid4()}"

        self._beacon_lease = None

    def _advertisement_payload(
        self,
        *,
        backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
        diagnostics: Mapping[str, object] | None = None,
    ) -> dict:
        return example_advertisement_payload(
            service_id=self.service_id,
            session_id=self.session_id,
            backend_status=backend_status,
            diagnostics=diagnostics,
        )

    async def run(self, stop_event: anyio.Event) -> None:
        async with Deckr() as deckr:
            async with deckr.endpoint(
                self.endpoint,
                session_id=self.session_id,
            ):
                beacon = deckr.beacon

                self._beacon_lease = await beacon.advertise(
                    BeaconAdvertisementSpec(
                        feature_id=EXAMPLE_PROTOCOL.feature_id,
                        endpoint=self.endpoint,
                        advertiser=self.endpoint,
                        session_id=self.session_id,
                        protocol={
                            "namespace": EXAMPLE_PROTOCOL.namespace,
                            "version": "1",
                        },
                        operations=EXAMPLE_PROTOCOL.operations,
                        labels={
                            "serviceId": self.service_id,
                            "namespace": EXAMPLE_PROTOCOL.namespace,
                        },
                        payload=self._advertisement_payload(),
                    ),
                    cleanup_stale_same_endpoint=True,
                )

                try:
                    await stop_event.wait()
                finally:
                    # Withdrawing Beacon removes us from future discovery.
                    if self._beacon_lease is not None:
                        await self._beacon_lease.aclose()
```

What keeps the Beacon advertisement alive?

The returned `BeaconAdvertisementLease` is the important object. Keeping it alive keeps the managed heartbeat loop alive. The branch’s Beacon lease has an internal heartbeat loop and `aclose()`/withdraw path. The docs describe the default Beacon TTL as 300 seconds, with managed refreshes scheduled around 150–225 seconds by default.

To update the advertisement because the advertised feature degraded:

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
            "namespace": EXAMPLE_PROTOCOL.namespace,
        },
        operations=EXAMPLE_PROTOCOL.operations,
    )
```

If the advertiser wants to disappear from future discovery without changing any
existing Concord authority, it should withdraw Beacon and leave Concord
participant leases alone. This is discovery drain, not hard admission control:
a consumer that already cached the descriptor may still propose a Concord
contract. Services that need a hard drain should also pause or close the
participant or make the accept policy reject new proposals.

```python
async def stop_advertising(self) -> None:
    if self._beacon_lease is not None:
        await self._beacon_lease.aclose()
        self._beacon_lease = None
```

---

# 2. Service discovers a Beacon advertisement based on criteria

This is the **consumer** side.

Consumers should use a generic `BeaconDirectory`, not raw Beacon KV scans,
ad hoc candidate parsing loops, or a service-specific directory/resolver path.
The directory belongs to Beacon: it owns one watch for one feature id and keeps
an in-process parsed descriptor set. Service-specific meaning is supplied only
by the parser function.

Do not add a fallback from `BeaconDirectory` to lower-level exact candidate or
key-listing APIs in this path. If the local Beacon view is not ready or current,
wait for it or surface the feature as temporarily unavailable; an exact key scan
on the request path recreates the slow failure mode this shape is meant to
remove.

```python
def parse_example_service_candidate(candidate) -> ServiceDescriptor | None:
    return parse_service_descriptor(candidate, EXAMPLE_PROTOCOL)


def example_service_directory(beacon) -> BeaconDirectory[ServiceDescriptor]:
    return BeaconDirectory(
        beacon,
        EXAMPLE_PROTOCOL.feature_id,
        parse_example_service_candidate,
        log_label="ExampleService",
    )
```

Start the directory with the surrounding task group and wait for its initial
watch replay before resolving from it:

```python
directory = example_service_directory(deckr.beacon)
directory.start(task_group)
await directory.wait_ready()
```

Resolving from the directory is local selection over parsed descriptors. The
predicate encodes required capability; the selector encodes preference.

```python
def example_service_matches(
    descriptor: ServiceDescriptor,
    *,
    required_operations: set[str],
    required_view_families: set[str],
) -> bool:
    if descriptor.backend_status == ServiceBackendStatus.UNAVAILABLE:
        return False

    if descriptor.namespace != EXAMPLE_PROTOCOL.namespace:
        return False

    if descriptor.use_profile != EXAMPLE_PROTOCOL.use_profile:
        return False

    if not required_operations.issubset(descriptor.supported_operations):
        return False

    if not required_view_families.issubset(set(descriptor.views)):
        return False

    return True


def select_newest_available(
    descriptors: Collection[ServiceDescriptor],
) -> ServiceDescriptor | None:
    available = tuple(
        descriptor
        for descriptor in descriptors
        if descriptor.backend_status != ServiceBackendStatus.UNAVAILABLE
    )
    return newest_service_descriptor(available)
```

```python
async def find_example_service_now(
    directory: BeaconDirectory[ServiceDescriptor],
    *,
    required_operations: set[str],
    required_view_families: set[str],
) -> ServiceDescriptor:
    await directory.wait_ready()

    selected = directory.resolve(
        lambda descriptor: example_service_matches(
            descriptor,
            required_operations=required_operations,
            required_view_families=required_view_families,
        ),
        select=select_newest_available,
    )
    if selected is not None:
        return selected

    raise ServiceUnavailable(
        "no_candidate",
        "No matching example service is currently advertised",
        {
            "featureId": EXAMPLE_PROTOCOL.feature_id,
            "operations": sorted(required_operations),
            "views": sorted(required_view_families),
        },
    )
```

The normal startup path should wait for the directory's Beacon watch to observe
a matching descriptor. If the service is a required dependency, timing out does
not recover anything; it usually only moves failure into a caller retry loop.
Let cancellation or shutdown stop the wait. Pass a timeout only when an outer
workflow has a real bounded latency budget, such as an interactive UI action.

```python
async def wait_for_example_service(
    directory: BeaconDirectory[ServiceDescriptor],
    *,
    required_operations: set[str],
    required_view_families: set[str],
    timeout_seconds: float | None = None,
) -> ServiceDescriptor:
    try:
        return await directory.wait_for(
            lambda descriptor: example_service_matches(
                descriptor,
                required_operations=required_operations,
                required_view_families=required_view_families,
            ),
            select=select_newest_available,
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise ServiceUnavailable(
            "discovery_timeout",
            "Timed out waiting for a matching example service",
            {
                "featureId": EXAMPLE_PROTOCOL.feature_id,
                "operations": sorted(required_operations),
                "views": sorted(required_view_families),
            },
        ) from exc
```

This is the expected shape for every feature family: one Beacon feature watch,
one parser that returns a typed descriptor or `None`, and local predicate plus
selector functions.

---

# 3. Service proposes a Concord contract

This is the **consumer** proposing service use.

The important part: `service_use_terms(...)` creates deterministic terms so
both participants can validate the requested scope. The `serviceUseId` inside
those terms is semantic agreement material only. It is not a
service-use index key, not a reusable Concord contract id, and not a lookup path
for reviving prior authority. Concord contract ids remain opaque and
incarnation-specific.

Use a long enough proposal timeout. The consumer cannot safely use the service
until the provider has accepted and Concord validates the contract, so a short
timeout usually does not make progress; it cancels a contract that may have been
about to be accepted and starts retry churn. A typical service-use negotiation
timeout should be at least 30 seconds unless the caller has an explicit
interactive latency budget.

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
async def propose_example_service_use(
    deckr: Deckr,
    *,
    client_endpoint: str,
    client_session_id: str,
    descriptor: ServiceDescriptor,
    required_operations: set[str],
    required_view_families: set[str],
    task_group: anyio.abc.TaskGroup,
    timeout_seconds: float = 30.0,
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
            # Intentionally no deterministic contract id and no service-use
            # scope index lookup. If this authority is lost, propose a fresh
            # Concord contract; use supersedes only when replacing an exact
            # known contract pointer.
            #
            # The serviceUseId inside terms describes the requested scope. It
            # is not Concord identity.
        ),
        # Starts the local participant token heartbeat loop.
        start_soon=task_group.start_soon,
    )

    with anyio.move_on_after(timeout_seconds):
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
                    "Example service-use contract became terminal during negotiation",
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

    try:
        await agreement.cancel("contract_timeout")
    except ConcordConflict:
        pass
    finally:
        await agreement.aclose()

    raise ServiceUnavailable(
        "contract_timeout",
        "Timed out waiting for example service-use contract to become valid",
    )
```

The Concord contract becomes valid only when every named participant has attached an acceptable token. The protocol requires exact validation of the contract plus all participant tokens for strict authority.

A consumer using the service should refresh or validate before important work:

```python
async def use_example_service(lease: ServiceUseLease) -> None:
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

The provider should use `Concord.participant(...)` for normal service-use
acceptance. The participant manager watches matching contracts, reconciles from
Concord's materialized contract index, attaches or refreshes this provider's
participant token, and releases tokens when a contract is no longer selected.
The service supplies policy through callbacks.

```python
class ExampleServiceUseParticipant:
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

        self.participant = concord.participant(
            participant=service_endpoint,
            session_id=service_session_id,
            profile=protocol.use_profile,
            current_sessions=self._current_sessions,
            accept_contract=self._accept_contract,
            log_label="ExampleService",
        )

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.participant.start(task_group)

    async def _current_sessions(self, _contract) -> Mapping[str, str]:
        # The provider's current service session must be part of validation.
        # A restarted service gets a new session id and must not revive old
        # proposals.
        return {self.service_endpoint: self.service_session_id}

    async def _accept_contract(self, _contract, record) -> bool:
        terms = self._validated_terms(record)
        if terms is None:
            return False

        return await self._application_accepts(terms)

    async def _application_accepts(self, terms: ServiceUseTerms) -> bool:
        # Domain policy lives here: capacity, backend health, tenant policy,
        # requested operations, requested view prefixes, and so on.
        return True

    def _validated_terms(self, record) -> ServiceUseTerms | None:
        if record.profile != self.protocol.use_profile:
            return None

        if record.terms is None:
            return None

        participants = {str(item) for item in record.participants}
        if self.service_endpoint not in participants:
            return None

        try:
            terms = ServiceUseTerms.model_validate(record.terms)
        except ValueError:
            return None

        if terms.profile != self.protocol.use_profile:
            return None

        if terms.service_id != self.service_id:
            return None

        if str(terms.service_endpoint) != self.service_endpoint:
            return None

        # Critical stale-contract protection:
        # A restarted service has a new session id and must not accept old terms.
        if terms.service_session_id != self.service_session_id:
            return None

        if terms.service_namespace != self.protocol.namespace:
            return None

        unsupported_operations = set(terms.allowed_operations).difference(
            set(self.protocol.operations)
        )
        if unsupported_operations:
            return None

        for family, prefixes in terms.allowed_views.items():
            if family not in self.protocol.view_families:
                return None

            expected_prefix = service_view_prefix(self.service_id, family)
            for prefix in prefixes:
                if not prefix.startswith(expected_prefix):
                    return None

        return terms

    async def aclose(self, *, reason: str = "service_shutdown") -> None:
        for managed in self.participant.managed_contracts:
            try:
                await self.participant.cancel(managed.contract, reason=reason)
            finally:
                await self.participant.release(
                    managed.contract,
                    reason=reason,
                    withdraw=True,
                )

        await self.participant.aclose()
```

What keeps the accepted contract alive?

`Concord.participant(...)` owns the selected contract set and the provider's
participant-token leases. Keeping the participant manager running keeps the
provider token heartbeats alive for contracts that still pass validation and
policy.

The provider rejects stale proposals by checking:

```python
terms.service_session_id == self.service_session_id
```

That is important. If the service restarts, it gets a new session id. Old terms
should not be accepted by the new process as if the old service state still
exists.

Soft drain and hard drain are different operations:

```text
soft drain: withdraw Beacon, keep Concord participant running for existing contracts
hard drain: stop accepting, cancel selected contracts, withdraw provider tokens
```

The provider path is just the generic Concord participant path plus
service-specific terms validation and application policy:

```text
validate ServiceUseTerms
Concord.participant(profile=..., participant=...)
hold Concord managed-contract state
cancel/withdraw leases on shutdown
```

Raw `Concord.watch(...)` and `Concord.attach(...)` remain useful low-level
primitives, but ordinary feature integrations should not need to reimplement
the participant manager.

---

# Full consumer flow

```python
class ExampleConsumer:
    def __init__(self, *, endpoint_id: str = "presence-client-main") -> None:
        self.client_endpoint = f"service:{endpoint_id}"
        self.client_session_id = f"presence-client-session-{uuid.uuid4()}"

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
                    directory = example_service_directory(deckr.beacon)
                    directory.start(tg)
                    await directory.wait_ready()

                    lease: ServiceUseLease | None = None

                    try:
                        descriptor = await wait_for_example_service(
                            directory,
                            required_operations={"presence.report"},
                            required_view_families={"status"},
                        )

                        lease = await propose_example_service_use(
                            deckr,
                            client_endpoint=self.client_endpoint,
                            client_session_id=self.client_session_id,
                            descriptor=descriptor,
                            required_operations={"presence.report"},
                            required_view_families={"status"},
                            task_group=tg,
                            timeout_seconds=30.0,
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
start Concord.participant for service-use profile + service endpoint
accept acceptable proposed contracts through policy callbacks
maintain Beacon heartbeat
maintain Concord participant-token heartbeats
```

Consumer startup:

```text
open endpoint session
start BeaconDirectory for the service protocol feature
wait for service descriptor from local parsed descriptors
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

---

## Breaking implementation plan

The first workable slice should introduce the generic `BeaconDirectory`, move
Python service discovery onto it, and delete the Python
`ServiceDirectory`/`ServiceResolver` public API in the same branch. The same
breaking branch should remove service-use index APIs and callers instead of
preserving them with aliases or shims.

`Concord.participant(...)` is already the desired provider-side shape. Keep it
and compose it with pure profile validation helpers; do not replace it with a
new service-specific acceptor.

Do not replace the removed APIs with another service-specific directory,
service-use acceptor, compatibility manager, or deterministic contract-reuse
helper unless a concrete repeated call-site need appears later.

Create:

```text
deckr.beacon.BeaconDirectory
BeaconDirectory tests for replay, update, withdraw, invalid payloads, and stale/current recovery
BeaconDirectory support for parser output that is zero, one, or many records
small generic Concord wait-for-valid helper, only if repeated proposal loops prove it useful
```

Modify:

```text
Python service consumers use BeaconDirectory plus pure service descriptor helpers
Python service consumers propose ConcordAgreementSpec directly for service use
provider implementations keep Concord.participant(...) and pure ServiceUseTerms validation
controller hardware/action discovery moves away from candidates_exact(...) fallback paths
docs describe BeaconDirectory and direct Concord proposal, not ServiceDirectory or service-use indexes
TypeScript and Rust mirrors follow the same public contract shape
```

Remove:

```text
Python ServiceDirectory, ServiceResolver, ServiceSelectionPolicy, and NewestServiceSelectionPolicy
service-use index APIs, buckets, scope records, deterministic pointer reuse, and tests
cross-language ServiceUseLeaseManager / ServiceQuery / service-use index mirrors
compatibility aliases, deprecated wrappers, dual discovery APIs, and exact-scan fallback paths
```

The utility boundary should be:

```text
generic lifecycle helpers live in deckr.beacon or deckr.concord
domain/profile packages provide pure parsers, selectors, terms builders, and validators
applications compose those pieces for their policy
```

That keeps boilerplate low without making services special. If repeated
boilerplate appears in services, hardware, actions, and future feature families,
the fix should be a parameterized Beacon or Concord utility. If boilerplate is
specific to one profile's payload or terms schema, it belongs beside that
profile's models as a pure helper.

Good generic utility candidates:

```text
runtime-owned BeaconDirectory registry keyed by feature id and stable
  parser/profile identity
BeaconDirectory parser/predicate/selector helpers
BeaconDirectory support for both one descriptor per advertisement and
  fan-out advertisements that produce zero or many domain records
Concord agreement wait helpers with caller-supplied terminal statuses
Concord participant helpers that accept current_sessions and accept_contract callbacks
```

The registry recommendation is a performance recommendation, not a convenience
wrapper for per-call lookups. A runtime should start one long-lived
BeaconDirectory for a feature/profile and reuse its local parsed view from
request handlers. It should not create a directory, replay a watch, or scan a
bucket inside each command, view read, action resolution, or hardware claim
attempt.

The directory API must not assume every Beacon advertisement maps to exactly one
domain record. Service advertisements normally parse into one descriptor.
Hardware advertisements can expose multiple device candidates. Action-provider
advertisements can expose multiple action descriptors. A generic directory can
model this either by accepting a parser that returns a collection, or by storing
parsed advertisement payloads and letting the domain selector fan out locally.

Good domain/profile helpers:

```text
parse candidate payload into a descriptor
select a descriptor from a collection
build profile terms from a descriptor and requested scope
validate proposed terms against current session and application policy
derive protected view keys and prefixes
```

Avoid utilities that combine discovery, terms construction, Concord proposal,
contract reuse, and provider acceptance into one domain-specific lifecycle
manager. Those helpers are hard to reuse across feature families and tend to
recreate hidden authority paths.

Performance guardrails:

```text
No raw KV key scans on runtime lookup paths.
No exact-candidate fallback from normal discovery.
No raw bucket items(...), materialized-bucket items_exact(...), or
  equivalent prefix listing inside service/action/hardware resolution.
No caller-facing Concord participant/profile search to decide which service to use.
No service-use scope index or deterministic scope pointer for contract reuse.
No per-request materialized view startup.
No public API that asks callers to choose between "cached" and "exact" lookup
  paths unless there is a strict diagnostic or maintenance requirement.
```

Hot-path discovery should wait for a long-lived Beacon view to become current
and then resolve locally. If that view is unavailable, surface unavailability or
let the surrounding lifecycle wait; do not recover by listing exact Beacon keys.
Concord lookup in hot paths should use agreement handles, exact known contract
pointers, or participant managers. Any filtered contract indexes needed to make
that fast should stay behind those abstractions rather than becoming application
integration APIs.
Maintenance, diagnostics, reapers, and cold-start materialization may perform
bounded scans, but feature resolution must not.

Python action SDK service surface:

Python action authors should receive `self.services`, not raw
`self.core.beacon`, `self.core.concord`, or `self.core.kv_bucket(...)`. The
runtime-owned `DeckrServices` handle keeps one `BeaconDirectory` per service
protocol, proposes scoped Concord service-use contracts, sends service commands,
and reads or watches fenced service views. It is intentionally not a
ServiceDirectory replacement and does not expose raw Beacon, Concord, endpoint,
or KV handles.

Availability probes are descriptor checks against the long-lived directory. They
must not open repeated one-second Concord proposals:

```python
async def openhab_probe(services: DeckrServices) -> None:
    client = OpenHABServiceClient(services)
    await client.require_available(
        "openhab-home",
        operations={"ensureItems", "sendCommand"},
        views={"items"},
    )
```

Interactive commands should name both budgets. The service-use budget covers
discovery plus Concord validity and should usually be at least 30 seconds. The
request budget covers only the command RPC after authority exists:

```python
reply = await SonosServiceClient(self.services).command(
    "sonos-home",
    "playMusicItem",
    {"zone": self.zone_name, "playRef": dict(play_ref)},
    service_use_timeout_seconds=30.0,
    request_timeout_seconds=12.0,
)
```

View watchers should normally let lifecycle cancellation bound setup and retry.
Do not reuse command RPC timeouts as watcher setup timeouts:

```python
async for view in SonosServiceClient(self.services).watch_view(
    "sonos-home",
    sonos_zone_view_ref("sonos-home", self.zone_name),
):
    if view is None:
        break
    await self._apply_view(view)
```

Landed Python cleanup:

```text
deckr-action-provider-runtime-python exposes DeckrServices as context.services
and self.services. DeckrRuntimeCore is no longer exported as an SDK type.

deckr-plugin-openhab, deckr-plugin-sonos, and deckr-plugin-kaj action code use
self.services. Their service clients no longer use _DIRECTORY_CACHE,
ServiceDirectory, ServiceResolver, SERVICE_USE_INDEX_BUCKET_POLICY, or
acquire_service_use_lease.

OpenHAB/Sonos providers keep Concord.participant(...) with service-use terms
validation and current service session evidence.
```

Remove these public service-specific discovery helpers in the breaking branch:

```text
src/deckr/services/directory.py
from deckr.services import ServiceDirectory
from deckr.services import ServiceResolver
from deckr.services import ServiceSelectionPolicy
from deckr.services import NewestServiceSelectionPolicy
```

Remove any remaining Python service-use index and request-lifecycle surface in
the breaking branch. Some of these symbols may already be gone in Python; do not
reintroduce them:

```text
from deckr.services import SERVICE_USE_INDEX_BUCKET_POLICY
from deckr.services import SERVICE_USE_INDEX_SCHEMA_ID
from deckr.services import ServiceUseRequest
from deckr.services import ServiceUseScopeIndexRecord
from deckr.services import acquire_service_use_lease
from deckr.services import service_use_scope_index_key
deckr_service_use_index_v1 references
```

Remove or replace the corresponding cross-language mirrors in the same
contract-breaking pass:

```text
typescript/deckr ServiceUseLeaseManager
typescript/deckr SERVICE_USE_INDEX_SCHEMA_ID
typescript/deckr ServiceUseScopeIndexRecord
typescript/deckr validateServiceUseScopeIndexRecord(...)
typescript/deckr serviceUseScopeIndexKey(...)
typescript/deckr DEFAULT_SERVICE_USE_INDEX_STORE_NAME
rust/deckr SERVICE_USE_INDEX_SCHEMA_ID
rust/deckr DEFAULT_SERVICE_USE_INDEX_STORE_NAME
rust/deckr service_use_index_store_policy(...)
rust/deckr ServiceDirectory
rust/deckr ServiceResolver
rust/deckr ServiceQuery
rust/deckr ServiceUseScopeIndexRecord
rust/deckr service_use_scope_index_key(...)
```

Remove the matching imports and `__all__` exports from `deckr.services`. Remove
tests whose only purpose is deterministic service-use index behavior:

```text
scope-index reuse
stale pointer replacement
client-session mismatch replacement
missing-token replacement through the index
terms-hash mismatch replacement
CAS retry behavior for the service-use index
```

Replacement discovery code should be structured like this:

```python
directory = BeaconDirectory(
    deckr.beacon,
    PROTOCOL.feature_id,
    lambda candidate: parse_service_descriptor(candidate, PROTOCOL),
    log_label="ExampleService",
)
directory.start(task_group)
await directory.wait_ready()

descriptor = await directory.wait_for(
    lambda item: (
        item.backend_status != ServiceBackendStatus.UNAVAILABLE
        and {"presence.report"}.issubset(item.supported_operations)
        and {"status"}.issubset(set(item.views))
    ),
    select=newest_service_descriptor,
)
```

Replacement consumer proposal code should be structured like this:

```python
terms = service_use_terms(
    descriptor,
    client_endpoint=client_endpoint,
    operations={"presence.report"},
    views={"status"},
)

agreement = await deckr.concord.propose(
    ConcordAgreementSpec(
        participants=(client_endpoint, str(descriptor.endpoint)),
        local_participant=client_endpoint,
        local_session_id=client_session_id,
        profile=descriptor.use_profile,
        terms=terms,
    ),
    start_soon=task_group.start_soon,
)
```

Replacement provider acceptance code should be structured like this:

```python
participant = deckr.concord.participant(
    participant=service_endpoint,
    session_id=service_session_id,
    profile=PROTOCOL.use_profile,
    current_sessions=current_sessions_for_service,
    accept_contract=accept_service_use_terms,
    log_label="ExampleService",
)
participant.start(task_group)
```

Replace deleted index/discovery coverage with:

```text
BeaconDirectory replay and live update behavior
domain parser rejects invalid Beacon payloads
consumer proposes opaque Concord contracts directly
provider accepts through Concord.participant(...)
stale service session terms are rejected
shutdown/drain cancels or releases managed Concord contracts as intended
```

Keep the service domain models and helpers that describe payloads, terms, and
protected views:

```text
ServiceProtocol
ServiceAdvertisementPayload
ServiceDescriptor
ServiceUseTerms
ServiceUseLease
parse_service_descriptor(...)
service_descriptor_from_terms(...)
service_use_terms(...)
service_view_key(...)
service_view_prefix(...)
ServiceViewStore and view authorization helpers
```
