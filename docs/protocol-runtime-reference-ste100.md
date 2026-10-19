# Runtime API and Operations Reference

## 1 Purpose and status

**Status: Review draft. Non-normative.**

This document gives runtime API examples, configuration instructions, test tools,
and procedures for operations and troubleshooting. Its audience is runtime
maintainers, infrastructure developers, test authors, and operators.

This draft is the proposed replacement for the
[existing runtime reference](protocol-runtime-reference.md). Replacement will
follow review. Section 8 lists the related documentation changes for that step.

This document does not define, relax, or qualify protocol requirements.
The specifications define those requirements for every implementation.
If an example disagrees with a specification, use the specification and correct
the example.

The specifications establish intended behavior. Source code can confirm API
names and arguments. A passing implementation test does not prove that the
implementation satisfies the specification.

| Document | Purpose |
| --- | --- |
| [Beacon specification](beacon-ste100.md) | Defines discovery requirements. |
| [Concord specification](concord-ste100.md) | Defines agreement and authority requirements. |
| [NATS substrate specification](nats-ste100.md) | Defines transport, storage, connection, and resource lifecycle requirements. |
| [Usage guide](usage.md) | Shows application developers how to use managed service APIs. |
| This reference | Shows infrastructure APIs, configuration, tests, and operational procedures. |

## 2 Technical terms

Technical terms have the same meanings as in the specifications.
API names, field names, identifiers, and code values keep their exact spelling.

| Term | Meaning |
| --- | --- |
| Authority | Permission to do a protected operation through a Concord agreement. |
| Contract pointer | The pair `contractId` and `generation` that identifies one Concord contract instance. |
| Currentness | The source state that shows if its observations are up to date. |
| Domain policy | Application decisions about permitted work, resource preferences, or capacity within the applicable profile rules. |
| Endpoint session | One runtime instance of an endpoint. |
| Exact read | A read of a specified key directly from the store. |
| Heartbeat | A required renewal or watch liveness signal. Beacon and Concord have separate heartbeat rules. |
| JetStream consumer | A broker resource that supplies stream observations to a client. |
| KV bucket | A NATS JetStream store that holds values at keys. KV means key-value. |
| Local authority | Authority held by a local runtime for one agreement. Stored records alone do not prove this authority. |
| Materialized view | A parsed, indexed representation of observations from local sources. |
| Participant policy | Runtime infrastructure that selects contract participation from domain decisions and protocol state. |
| TTL | Time to live. The interval after the last write before the store expires a record. |

Computer operations use technical verbs such as **parse**, **publish**,
**subscribe**, and **validate**. To publish is to send a message or record.
To validate is to check data against the applicable rules.

## 3 API examples

### 3.1 Common prerequisites

The examples use Python. Run shell commands from the `deckr` repository root.
Use the [README](../README.md#requirements) for installation requirements.

Except for the test harness, the examples need a running Deckr runtime and a
NATS broker with JetStream enabled. The variable `deckr` represents a runtime
inside its `async with Deckr(...)` context. The variable `manager_deckr`
represents the remote manager runtime.

Run asynchronous fragments inside an asynchronous function. Supply each named
input before execution. Keep each context open while its owned operation is
necessary.

Ordinary service consumers use `deckr.services(endpoint)` and the
[managed service examples](usage.md#consumer-context).
Service providers use `deckr.service_provider(...)`.
Use `deckr.hardware_manager(...)` for the managed hardware-manager lifecycle.
Direct Beacon, Concord, and service-view APIs are for infrastructure
implementations and conformance tests.

The [Concord context requirements](concord-ste100.md#166-managed-providers-and-hardware-managers)
assign lifecycle work to the managed runtime. The following table summarizes
that boundary. It does not add protocol requirements.

| Responsibility | Owner |
| --- | --- |
| Advertisement records, currentness, refresh, and discovery | Beacon runtime. |
| Contract and token validation, attachment, renewal, authority loss, and cleanup | Concord runtime. |
| Profile data validation, hardware claim rules, service negotiation, and protected-operation fences | The applicable profile and managed runtime. |
| Device facts, backend behavior, requested operations, and domain preferences | Application code, within the profile rules. |
| Transport delivery, broker connection, and KV resource cleanup | NATS substrate. |

Applications supply intent and domain facts through managed APIs.
They do not maintain protocol authority by examining token records or validity
transitions. Core infrastructure enforces the protocol consequences of domain
decisions.

### 3.2 Send a lane message

Prerequisites:

- The runtime has the `actions` lane and its lane contract.
- The receiving controller accepts the example extension type and schema.
- The controller recognizes the example context ID, `ctx`.

```python
from deckr.contracts.messages import entity_subject

async with deckr.endpoint("action_provider:clock") as endpoint:
    await endpoint.send(
        lane="actions",
        recipient="controller:main",
        subject=entity_subject("extension", contextId="ctx"),
        message_type="actionExtension",
        body={
            "extensionType": "dev.deckr.example.ping",
            "extensionSchemaId": "dev.deckr.example.ping.v1",
            "data": {},
        },
    )
```

The endpoint supplies the sender address and session ID.
The extension identifiers are example values. A completed send does not prove
that the receiver processed the message.

### 3.3 Publish a Beacon advertisement

This fragment is for Beacon and hardware-profile infrastructure.
Hardware backends use the managed context in section 3.5.

Prerequisite: `devices` maps device IDs to valid `HardwareAdvertisementDevice`
values for this manager. The
[hardware profile](beacon-ste100.md#9-beacon-hardware-profile) defines its identity checks.

```python
from deckr.beacon import BeaconAdvertisementSpec
from deckr.hardware.profiles import HardwareBeaconPayload

async with deckr.endpoint("hardware_manager:mirabox-main") as endpoint:
    payload = HardwareBeaconPayload(
        manager_id=endpoint.address.endpoint_id,
        manager_endpoint=endpoint.address,
        session_id=endpoint.session_id,
        devices=devices,
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    async with deckr.beacon.advertise(
        endpoint,
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.hardware",
            payload=payload,
        ),
    ):
        await deckr.beacon.wait_current()
        candidates = deckr.beacon.candidates("dev.deckr.hardware")
```

The advertisement context owns publication, refresh, and withdrawal.
The runtime gets the TTL and refresh interval from the bucket policy.
Candidates give discovery information. They do not give agreement authority.

### 3.4 Propose a Concord agreement

This fragment is for Concord and hardware-profile infrastructure.
Managed service consumers use `services.use(...)` or `services.use_matching(...)`.

Prerequisites:

- `terms` is a valid `HardwareClaimTerms` value for the named controller and manager.
- `manager_session_id` identifies the selected manager session.
- The remote manager has an active managed hardware context.

Use `deckr.hardware.profiles` for hardware terms and profile helpers.
Use the selected manager session as proposal evidence. Later Beacon changes do
not change the session fence of an accepted agreement.

```python
from deckr.concord import ConcordAgreementSpec, ConcordLifecyclePhase

async with deckr.endpoint("controller:main") as endpoint:
    async with deckr.concord.propose(
        endpoint,
        ConcordAgreementSpec(
            participants=(endpoint.address, "hardware_manager:mirabox-main"),
            profile="dev.deckr.profile.hardware_claim.v1",
            terms=terms,
            participant_sessions={
                "hardware_manager:mirabox-main": manager_session_id,
            },
        ),
    ) as agreement:
        state = await agreement.wait_for_state(
            lambda current: (
                current.valid or current.phase == ConcordLifecyclePhase.TERMINAL
            ),
            timeout_seconds=10,
        )
        if not state.valid:
            raise RuntimeError(state.reason or "hardware claim was rejected")
        # Use the agreement inside this context while local authority is valid.
```

The proposal gets the controller session from `endpoint`.
`state.valid` describes that state snapshot. It does not promise future authority.
The profile runtime enforces authority for each protected operation.
Application callbacks supply device behavior without implementing these checks.

### 3.5 Supply hardware behavior through a managed context

The managed hardware context owns advertisement publication, participant
selection, authorization, and cleanup under the
[Concord context requirements](concord-ste100.md#166-managed-providers-and-hardware-managers).
The hardware backend supplies device facts and device behavior.

Prerequisites:

- `device_descriptors` maps device IDs to valid `DeviceDescriptor` values for this manager.
- `handle_command(message)` is an asynchronous handler for device commands.
- `reset_device(device_id)` is an asynchronous handler for device reset.
- `stop_requested` is an `anyio.Event` that the application sets during shutdown.

The command handler returns `True` for a handled command, `False` for stale
device state, or `None` for an unsupported command.

```python
async with manager_deckr.endpoint("hardware_manager:mirabox-main") as endpoint:
    async with manager_deckr.hardware_manager(
        endpoint,
        manager_id="mirabox-main",
        devices=device_descriptors,
        command_handler=handle_command,
        reset_handler=reset_device,
    ):
        await stop_requested.wait()
```

Device availability and permitted device behavior are domain inputs.
Contract identity, participant sessions, token validity, and local authority are
protocol concerns. The runtime checks those conditions before command execution.

An agreement can change from pending to valid without a change in domain consent.
The [participant policy requirements](concord-ste100.md#133-agreement-intent-and-participant-policy)
assign continued participation to runtime infrastructure.
Application code does not filter `not_yet_fulfilled` and `valid` candidates to
keep an agreement active.

### 3.6 Open a service-view store

This example is for service infrastructure. Ordinary consumers use the
[managed view API](usage.md#reading-and-watching-views).

Prerequisites:

- The service protocol declares the example bucket for its view family.
- The deployment permits that bucket and its TTL policy.
- Service infrastructure obtains authority and view fences through its managed capability.

```python
import anyio

from deckr.services import ServiceViewStore
from deckr.substrates.nats_kv import KvBucketPolicy

views = ServiceViewStore(
    bucket=deckr.kv_bucket(
        KvBucketPolicy(
            bucket="com_example_service_view_v1",
            ttl_seconds=30,
            allow_write_ttl=True,
            description="service view KV",
        )
    )
)
async with anyio.create_task_group() as task_group:
    views.start(task_group)
    try:
        await views.wait_current()
        # Perform authorized view operations here.
    finally:
        await views.aclose()
        task_group.cancel_scope.cancel()
```

The example uses a private, versioned bucket name.
Generic `kv_bucket(...)` access rejects reserved Beacon and Concord buckets.
The [service-use profile](concord-ste100.md#162-service-use) defines view fences.
Opening a view store does not establish service-use authority.

## 4 Configuration

### 4.1 Broker connection retries

Set the maximum retry interval in the runtime substrate configuration:

```toml
[deckr.runtime.substrate]
reconnect_max_interval_seconds = 60
```

Use a finite positive number. The default is 60 seconds.
`NatsSubstrate` and `SupervisedNatsSubstrate` accept the same option.
The [connection requirements](nats-ste100.md#4-broker-connection-lifecycle)
define initial retries, reconnection backoff, cancellation, and shutdown.

This option does not set the supervised broker process startup deadline.
Transport retries do not extend Concord heartbeat deadlines or restore
terminated local authority.

### 4.2 Bucket policy

Use an explicit `KvBucketPolicy` for each private bucket.
Use the managed APIs for reserved protocol buckets.

The [bucket policy checks](nats-ste100.md#72-bucket-policy-checks) define the
complete shared comparison. They cover `max_age`, `max_msgs_per_subject`,
`allow_msg_ttl`, and `subject_delete_marker_ttl`.

If the comparison fails, investigate the deployment configuration.
The specification requires rejection of an incompatible bucket without changes
to the existing stream. Settings such as `allow_direct`, `storage`, and creation
`metadata` are outside that comparison.

Client differences in other fields do not change the required checks.
Section 5.3 describes how to record implementation observations.

## 5 Testing

### 5.1 Concord runtime harness

Use `deckr.testing.ConcordRuntimeHarness` for focused Concord tests.
It supplies stores in memory and the runtime capability.
It does not need a NATS broker.

This complete example uses a test profile and two immutable test endpoints.
It checks the core agreement lifecycle after attachment.
The selector is a core test fixture that selects every test candidate.
Concord performs protocol validation. This fixture does not define application
admission policy.

```python
from dataclasses import dataclass

import anyio

from deckr.concord import (
    ConcordAgreementSpec,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
)
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.testing import ConcordRuntimeHarness

@dataclass(frozen=True)
class TestEndpoint:
    address: EndpointAddress
    session_id: str

def select_test_contracts(candidates):
    return ConcordParticipantSelection(
        [candidate.contract.pointer for candidate in candidates]
    )

async def main():
    harness = ConcordRuntimeHarness()
    controller = TestEndpoint(
        parse_endpoint_address("controller:main"), "controller-session"
    )
    manager = TestEndpoint(
        parse_endpoint_address("hardware_manager:mirabox-main"), "manager-session"
    )
    profile = "org.example.runtime_test.v1"
    policy = ConcordParticipantPolicy(profile=profile, select=select_test_contracts)
    spec = ConcordAgreementSpec(
        profile=profile,
        participants=(controller.address, manager.address),
        participant_sessions={manager.address: manager.session_id},
    )
    async with anyio.create_task_group() as task_group:
        harness.concord.start(task_group)
        try:
            await harness.concord.wait_current()
            async with harness.concord.participant(manager, policy) as participant:
                async with harness.concord.propose(controller, spec) as agreement:
                    state = await agreement.wait_for_state(
                        lambda current: current.valid, timeout_seconds=2
                    )
                    await participant.wait_for_state(
                        lambda current: any(
                            item.contract.pointer == state.contract.pointer
                            and item.validity.valid
                            and item.local_authority
                            for item in current.contracts
                        ),
                        timeout_seconds=2,
                    )
                    assert agreement.state.valid
        finally:
            await harness.concord.aclose()
            task_group.cancel_scope.cancel()

anyio.run(main)
```

Core conformance tests cover attachment, continued participation after an
agreement becomes valid, session mismatch, authority loss, and context cleanup.
They also cover termination while domain consent remains unchanged.
Application policy tests cover domain decisions, such as requested operations
and resource preferences. Applications do not test or implement token renewal
as part of their domain policy.

### 5.2 Maintenance harness

Maintenance tests use `deckr.concord_maintenance.ConcordMaintenance` with three
exact stores: contracts, tokens, and maintenance markers.
`deckr.testing.ConcordMaintenanceHarness` supplies these stores and exposes
`.maintenance`.

Maintenance state is separate from normal Concord readiness and validity.
`ConcordRuntimeHarness` does not expose a maintenance capability.

### 5.3 Implementation evidence

This reference does not claim current parity between Python, TypeScript, Rust,
or downstream components. It does not contain undated broker comparison results.

For an implementation-status report, record these details:

- The test date and repository revisions.
- The runtime, client, and NATS server versions.
- The command and deployment configuration.
- The result and a link to the retained test output.
- Any normalization applied before comparison.
- The relevant specification requirement for each failure.

Keep observations about complete stream configurations separate from the
required bucket policy comparison. An observed client difference does not create
a protocol exception.

## 6 Operations

### 6.1 Generate contract artifacts

Prerequisite: the repository development dependencies are installed.

Regenerate the contract bundle:

```bash
uv run python scripts/generate_contract_artifacts.py
```

Review changes under `contract/v1/` before you commit them.
The Python source models include `deckr.beacon`, `deckr.concord`,
`deckr.hardware.profiles`, and `deckr.action_runtime`.
Generated artifacts and source models do not change specification requirements.

### 6.2 Report stored state

Prerequisites: the broker is accessible and the protocol buckets exist.

Run the state report:

```bash
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```

The report reads Beacon advertisements, Concord contracts, and participant
tokens. It shows keys, revisions, and selected record fields, including token
and advertisement TTL values.

The report does not inspect local lifecycle state or prove live authority.
Its reads do not form one atomic snapshot. An empty result can also indicate
a failed key listing. Compare the report with runtime state and broker diagnostics.

### 6.3 Inspect broker buckets

Prerequisites: the NATS CLI is installed and has access to the broker.
Replace the example server address for other deployments.

Inspect the shared buckets:

```bash
nats kv info deckr_beacon_advertisement_v1 --server nats://127.0.0.1:4222
nats kv info deckr_concord_contract_v1 --server nats://127.0.0.1:4222
nats kv info deckr_concord_token_v1 --server nats://127.0.0.1:4222
nats kv info deckr_concord_maintenance_v1 --server nats://127.0.0.1:4222
```

List protocol records and maintenance markers:

```bash
nats kv ls deckr_beacon_advertisement_v1 'advertisements.by_feature.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_contract_v1 'contracts.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_token_v1 'contracts.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_maintenance_v1 'stale.>' --server nats://127.0.0.1:4222
nats kv ls deckr_concord_maintenance_v1 'cleanup.>' --server nats://127.0.0.1:4222
```

The [Beacon store](beacon-ste100.md#5-advertisement-store) and
[Concord stores](concord-ste100.md#5-concord-stores) define bucket names and record meanings.
Maintenance markers do not give runtime authority.

## 7 Troubleshooting

### 7.1 Discovery

If a runtime cannot find hardware, actions, or services, use this procedure:

1. Check the requested Beacon feature ID.
2. Check the advertisement endpoint and session ID.
3. Check the payload against its profile.
4. Check Beacon currentness before you use discovery results.
5. Check the advertisement TTL and refresh observations for record expiry.
6. For service traffic, check that the host retains the core `services` lane contract.
7. Check that component declarations agree with that lane contract.

Use `deckr.hardware.profiles`, `deckr.action_runtime`, or the service protocol
module for profile details.
Beacon withdrawal, expiry, or loss of currentness affects discovery only.
These conditions alone do not terminate an existing Concord agreement.

### 7.2 Agreements and local authority

This procedure is for runtime maintainers. Applications use managed capability
state and domain errors. They do not inspect Concord records to maintain authority.

If an expected agreement is unavailable, check local state before stored records:

1. Identify the affected contract ID, generation, endpoint, and session ID.
2. Read `agreement.state.phase`, `current`, `local_authority`, `valid`, and `reason`.
3. For a participant context, read `participant.state` and the affected entry in `managed_contracts`.
4. Check that entry's `validity` and `local_authority`.
5. Check contract-source and token-source currentness at the time of failure.
6. Check required token renewal results and deadlines in the runtime diagnostics.
7. Check authority-watch heartbeats and their liveness deadlines.
8. Check the contract and every participant token for record validity and identity.
9. Compare session IDs, token ownership, terms hashes, and the contract pointer with the held agreement.
10. Compare recorded domain decisions with the core participant-selection diagnostics.

If a pending agreement becomes valid, unchanged domain consent still permits
participation. Cancellation caused only by that transition is a core lifecycle
defect. Correct the runtime or its profile integration. Do not add validity-status
bookkeeping to application code.

`Concord.validate(...)` uses cached observations.
`Concord.validate_exact(...)` checks stored records directly for infrastructure
diagnostics. Neither result, by itself, proves that a local lease still has authority.

Under [Concord section 9](concord-ste100.md#9-permanent-loss-of-authority), a
missed required heartbeat permanently terminates affected local authority.
Loss of contract-source or token-source currentness has the same effect,
even when exact reads still work.

The specification requires the runtime to stop protected operations immediately.
This action does not wait for an application callback or an operator.
Failed remote cleanup can leave contracts and tokens in storage. Their presence
does not let the affected runtime continue until TTL expiry.

If local authority has terminated, verify the runtime recovery behavior:

1. Check that the runtime has stopped protected operations through the affected agreement.
2. Record the termination reason and affected identities.
3. Check transport recovery and source currentness before new negotiation.
4. Check that successor negotiation uses a new opaque contract ID and generation 1.
5. Check that protected operations resume only through valid authority from the new agreement.

Reconnection, successful reads, renewed watch delivery, and stored tokens cannot
restore terminated local authority. The managed service API owns successor
negotiation for its consumers. Applications decide whether to continue the
requested work through that API. They do not recreate Concord tokens or contracts.

### 7.3 Lane delivery

If lane messages do not arrive, use this procedure:

1. Validate the `DeckrMessage` envelope against its lane contract.
2. Check the sender and recipient endpoint families.
3. Check the direct recipient session when `recipientSessionId` is set.
4. Check message expiry.
5. For protected traffic, check the envelope `contract` against the receiving profile's live agreement.
6. Check local authority as described in section 7.2.
7. Check that the NATS subject and header hints agree with the envelope.
8. For replies, check `inReplyTo`, the contract pointer, and the acceptance predicate.

Subjects and headers do not replace envelope or authority validation.

### 7.4 JetStream consumers

KV watches and list operations use JetStream consumers.
Normal Beacon, Concord, and service-view reads use runtime-owned materialized
caches. Maintenance uses temporary exact scans.
The [resource lifecycle requirements](nats-ste100.md#10-broker-resource-lifecycle)
define cleanup responsibilities.

Prerequisite: the NATS monitoring endpoint is available for inspection.

If broker load or consumer counts continue to increase, use this procedure:

1. Open `/jsz?accounts=true&streams=true&consumers=true&config=true` on the configured monitoring endpoint.
2. Compare consumers with active subscriptions against consumers without active subscriptions.
3. Check consumers that report `push_bound=false` for an expected owner and lifetime.
4. Inspect Beacon and Concord KV streams for consumers that remain after their owners close.
5. For Beacon, inspect `KV_deckr_beacon_advertisement_v1` and filters under `$KV.deckr_beacon_advertisement_v1.>`.
6. Check repeated snapshot, reconciliation, watch, and list operations.
7. Check that each temporary list consumer closes before its maintenance scan returns.
8. Check that consumer counts stabilize during repeated reads, recovery, and maintenance.
9. Check that temporary consumers disappear after completion or runtime shutdown.

An unbound consumer alone does not prove a leak.
Compare ownership and consumer lifetime across repeated observations.
Use the [state report](#62-report-stored-state) with monitoring results to
distinguish record expiry from consumers that remain after cleanup.

## 8 Review and replacement

This draft keeps the existing runtime reference available during review.
After approval, complete these documentation changes:

1. Replace references to the old runtime document with links to this document and its section anchors.
2. Update the README protocol links to `beacon-ste100.md`, `concord-ste100.md`, and `nats-ste100.md`.
3. Mark `beacon-concord.md` and `nats-bus.md` as superseded by the corresponding STE100 specifications.
4. Remove or qualify the older documents' normative declarations so that they cannot compete with the replacement specifications.
5. Mark the old runtime reference as superseded, or remove it after its incoming links have been updated.

Language reference:
[ASD-STE100 Simplified Technical English, Issue 9](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf).
