# Protocol Runtime Reference

This document contains implementation APIs, examples, tooling, and conformance
observations. It does not define or qualify protocol requirements. Every runtime
must conform to the [Beacon and Concord specification](beacon-concord.md) and
the [NATS substrate specification](nats-bus.md), including terminal authority
loss on missed required heartbeats.

The current artifact-generation models are in the Python modules `deckr.beacon`,
`deckr.concord`, `deckr.hardware.profiles`, and `deckr.action_runtime`.
Application usage is covered in the [usage guide](usage.md).

## Lane API Example

Components acquire endpoint sessions with:

```python
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

## Beacon API Example

The runtime-facing Python API is `deckr.beacon.Beacon`.

```python
async with deckr.endpoint("hardware_manager:mirabox-main") as endpoint:
    async with deckr.beacon.advertise(
        endpoint,
        BeaconAdvertisementSpec(
            feature_id="dev.deckr.hardware",
            payload=payload,
        ),
    ):
        candidates = deckr.beacon.candidates("dev.deckr.hardware")
```

## Implementation Parity Status

Cross-runtime status:
`deckr-adapter-elgato-node` and the Rust manager implementations are still behind
the managed Beacon/Concord lifecycle contracts used by the Python runtime, so they are
not yet considered parity-complete for this contract profile.

## Concord API And Test Examples

The implementation-level Python API for core runtime, hardware/service
infrastructure, and conformance tests is `deckr.concord.Concord`. Ordinary
service consumers should use `deckr.services` instead of constructing Concord
agreements directly.

```python
concord = deckr.concord
async with deckr.endpoint("controller:main") as endpoint:
    async with concord.propose(
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
            lambda current: current.valid or current.phase.value == "terminal",
            timeout_seconds=10,
        )
        if not state.valid:
            raise RuntimeError(state.reason or "hardware claim was rejected")
```

The remote runtime accepts candidate contracts through its own endpoint-owned
participant context and an immutable batch policy:

```python
def select_claims(candidates):
    return ConcordParticipantSelection(
        candidate.contract.pointer
        for candidate in candidates
        if candidate.validity.status.value == "not_yet_fulfilled"
    )

async with manager_deckr.endpoint("hardware_manager:mirabox-main") as endpoint:
    policy = ConcordParticipantPolicy(
        profile="dev.deckr.profile.hardware_claim.v1",
        select=select_claims,
    )
    async with manager_deckr.concord.participant(endpoint, policy) as participant:
        await participant.wait_for_state(lambda state: state.current)
```

Tests use the shipped `deckr.testing` harness instead of constructing normal
Concord runtime stores directly. Test endpoints provide the same immutable
address/session evidence as a real `EndpointSession`:

```python
from dataclasses import dataclass

import anyio

from deckr.testing import ConcordRuntimeHarness

@dataclass(frozen=True)
class TestEndpoint:
    address: EndpointAddress
    session_id: str

harness = ConcordRuntimeHarness()
async with anyio.create_task_group() as task_group:
    harness.concord.start(task_group)
    await harness.concord.wait_current()
    endpoint = TestEndpoint(
        address=parse_endpoint_address("controller:main"),
        session_id="controller-session",
    )
    async with harness.concord.propose(
        endpoint,
        ConcordAgreementSpec(
            participants=(endpoint.address, "hardware_manager:mirabox-main"),
            profile="dev.deckr.profile.hardware_claim.v1",
            terms=terms,
        ),
    ) as agreement:
        assert agreement.state.local_authority
        assert agreement.contract.contract_id
    task_group.cancel_scope.cancel()
```

Maintenance and reaper tests use the dedicated
`deckr.concord_maintenance.ConcordMaintenance` capability with three exact/raw
stores. `ConcordMaintenanceHarness` packages those stores and exposes its
dedicated `.maintenance` capability for focused tests, but it does not add
maintenance state to `ConcordRuntimeHarness` or normal `Concord` readiness.

## Service View Store Example

```python
from deckr.services import ServiceViewStore
from deckr.substrates.nats_kv import KvBucketPolicy

beacon = deckr.beacon
concord = deckr.concord
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
views.start(task_group)
```

## Bucket Conformance Observations

The real-NATS cross-runtime gate compares the complete fresh stream configs
after normalizing only bucket names and subjects. With the current clients, the
observed Python/TypeScript difference set is exactly `allow_direct` and Deckr
creation `metadata`. Both creators currently resolve to file storage, but
storage remains deployment-owned rather than a Deckr lifecycle policy field.

## Artifact Generation

Regenerate the bundle with:

```bash
uv run python scripts/generate_contract_artifacts.py
```

## State Reporting

Summarize current Beacon/Concord KV contents:

```bash
uv run --extra nats python scripts/nats_state_report.py --url nats://127.0.0.1:4222
```
