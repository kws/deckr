from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import Beacon, BeaconAdvertisementSpec, BeaconDirectory
from deckr.concord import (
    ConcordManagedContract,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import entity_subject, service_address
from deckr.services import (
    AuthorizedServiceMessage,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceExchangePattern,
    ServiceMessageDefinition,
    ServiceMessageDirection,
    ServiceMessageIntent,
    ServiceOperationDefinition,
    ServicePayloadSchema,
    ServiceProtocol,
    ServiceUseAuthorizationError,
    ServiceViewEntry,
    ServiceViewFamily,
    ServiceViewFamilyDefinition,
    ServiceViewReadContext,
    ServiceViewRef,
    ServiceViewStore,
    ServiceViewWriteContext,
    ServiceViewWriter,
    UnsupportedServiceScope,
    authorize_service_message,
    newest_service_descriptor,
    parse_service_descriptor,
    service_view_key,
)
from deckr.services.messages import ServiceMessageBody, service_message
from deckr.substrates.nats_kv import (
    KvChange,
    KvConflict,
    KvEntry,
    KvWatchBarrier,
    kv_value,
)
from deckr.testing import MemoryJsonKvBucket


def _protocol(
    service_id: str = "openhab-home",
    *,
    operations: tuple[str, ...] = ("setItemScope", "sendCommand"),
) -> ServiceProtocol:
    del service_id
    return ServiceProtocol(
        namespace="dev.deckr.openhab.service",
        feature_id="dev.deckr.openhab.feature",
        advertisement_profile="dev.deckr.openhab.service.advertisement.v1",
        use_profile="dev.deckr.openhab.service_use.v1",
        operations={name: ServiceOperationDefinition() for name in operations},
        messages={
            name: ServiceMessageDefinition(
                operation=name,
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            )
            for name in operations
        },
        view_families={
            "items": ServiceViewFamilyDefinition(
                storeName="deckr_openhab_service_view_v1",
                writer=ServiceViewWriter.SERVICE,
            )
        },
    )


def _memory_beacon() -> Beacon:
    return Beacon(MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300))


async def _publish_service_advertisement(
    beacon: Beacon,
    protocol: ServiceProtocol,
    *,
    service_id: str = "openhab-home",
    session_id: str = "service-session",
    advertisement_id: str = "ad-1",
    payload: Mapping[str, Any] | None = None,
    feature_id: str | None = None,
):
    return await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=feature_id or protocol.feature_id,
            endpoint=service_address(service_id),
            session_id=session_id,
            advertisement_id=advertisement_id,
            payload=(
                dict(payload)
                if payload is not None
                else protocol.advertisement_payload(
                    service_id=service_id,
                    session_id=session_id,
                    backend_status=ServiceBackendStatus.AVAILABLE,
                ).to_dict()
            ),
        )
    )


async def _descriptor(
    beacon: Beacon,
    protocol: ServiceProtocol,
) -> ServiceDescriptor:
    candidates = beacon.candidates(protocol.feature_id)
    descriptors = [
        descriptor
        for candidate in candidates
        if (descriptor := parse_service_descriptor(candidate, protocol)) is not None
    ]
    descriptor = newest_service_descriptor(descriptors)
    assert descriptor is not None
    return descriptor


async def _service_view_context(*, contract_id: str = "service-contract-1"):
    beacon = _memory_beacon()
    protocol = _protocol()
    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_ready()
        await _publish_service_advertisement(beacon, protocol)
        descriptor = await _descriptor(beacon, protocol)
        task_group.cancel_scope.cancel()
    lease = _FakeServiceUseLease(
        descriptor=descriptor,
        contract=_contract_handle(contract_id=contract_id),
    )
    view_ref = ServiceViewRef(
        "deckr_openhab_service_view_v1",
        service_view_key("openhab-home", "items", "Kitchen Light"),
    )
    return protocol, lease, view_ref


def _contract_handle(
    *,
    contract_id: str = "service-contract-1",
    generation: int = 1,
) -> ContractHandle:
    return ContractHandle(
        key=f"{contract_id}:{generation}",
        contract_id=contract_id,
        generation=generation,
        participants=tuple(
            sorted(
                (
                    action_provider_address("provider-main"),
                    service_address("openhab-home"),
                ),
                key=str,
            )
        ),
        attached_participants=tuple(
            sorted(
                (
                    action_provider_address("provider-main"),
                    service_address("openhab-home"),
                ),
                key=str,
            )
        ),
        revision=1,
        state=ContractState.OPEN,
        profile=_protocol().use_profile,
    )


def _contract_pointer(contract: ContractHandle) -> ContractPointer:
    return ContractPointer(contractId=contract.contract_id, generation=contract.generation)


def _service_view_storage_key(view: ServiceViewRef, contract: ContractHandle) -> str:
    pointer = _contract_pointer(contract)
    return (
        f"{view.key}.contract.{encode_key_token(pointer.contract_id)}."
        f"{pointer.generation}"
    )


def _service_view_payload(
    view: ServiceViewRef,
    lease: _FakeServiceUseLease,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pointer = _contract_pointer(lease.contract)
    return {
        **dict(payload or {"item": "Kitchen Light", "state": "ON"}),
        "viewKey": view.key,
        "serviceId": lease.descriptor.service_id,
        "serviceNamespace": lease.descriptor.namespace,
        "serviceEndpoint": str(lease.descriptor.endpoint),
        "serviceSessionId": lease.descriptor.session_id,
        "consumerEndpoint": str(lease.consumer_endpoint),
        "consumerSessionId": lease.consumer_session_id,
        "writer": "service",
        "contractId": pointer.contract_id,
        "generation": pointer.generation,
    }


def _service_view_write_context(
    lease: _FakeServiceUseLease,
) -> ServiceViewWriteContext:
    return _view_write_context(lease, ServiceViewWriter.SERVICE)


def _view_write_context(
    lease: _FakeServiceUseLease,
    writer: ServiceViewWriter,
) -> ServiceViewWriteContext:
    return ServiceViewWriteContext(
        writer=writer,
        service_id=lease.descriptor.service_id,
        service_namespace=lease.descriptor.namespace,
        service_endpoint=lease.descriptor.endpoint,
        service_session_id=lease.descriptor.session_id,
        consumer_endpoint=lease.consumer_endpoint,
        consumer_session_id=lease.consumer_session_id,
        contract=_contract_pointer(lease.contract),
        views=lease.descriptor.views,
    )


def _view_read_context(
    lease: _FakeServiceUseLease,
    reader: ServiceViewWriter = ServiceViewWriter.CONSUMER,
) -> ServiceViewReadContext:
    return ServiceViewReadContext(
        reader=reader,
        service_id=lease.descriptor.service_id,
        service_namespace=lease.descriptor.namespace,
        service_endpoint=lease.descriptor.endpoint,
        service_session_id=lease.descriptor.session_id,
        consumer_endpoint=lease.consumer_endpoint,
        consumer_session_id=lease.consumer_session_id,
        contract=_contract_pointer(lease.contract),
        views=lease.descriptor.views,
    )


def _contract_record(
    contract: ContractHandle,
) -> ContractRecord:
    return ContractRecord(
        contract_id=contract.contract_id,
        generation=contract.generation,
        participants=contract.participants,
        attached_participants=contract.attached_participants,
        state=contract.state,
        profile=contract.profile,
        terms=None,
        terms_hash=None,
    )


def _managed_contract(
    contract: ContractHandle,
    *,
    status: ContractValidityStatus = ContractValidityStatus.VALID,
) -> ConcordManagedContract:
    record = _contract_record(contract)
    service_token = ParticipantHandle(
        key=f"{contract.contract_id}:{contract.generation}:service",
        contract_id=contract.contract_id,
        generation=contract.generation,
        participant=service_address("openhab-home"),
        session_id="service-session",
        token_id="service-token",
        revision=1,
        refresh_seq=1,
        ttl_seconds=30,
    )
    consumer_token = ParticipantHandle(
        key=f"{contract.contract_id}:{contract.generation}:consumer",
        contract_id=contract.contract_id,
        generation=contract.generation,
        participant=action_provider_address("provider-main"),
        session_id="provider-session",
        token_id="consumer-token",
        revision=1,
        refresh_seq=1,
        ttl_seconds=30,
    )
    return ConcordManagedContract(
        contract=contract,
        record=record,
        validity=ContractValidity(
            status,
            contract=record if status == ContractValidityStatus.VALID else None,
            tokens={
                str(service_token.participant): service_token,
                str(consumer_token.participant): consumer_token,
            },
        ),
        token=service_token,
    )


def _service_message(
    contract: ContractHandle,
    *,
    name: str = "setItemScope",
    namespace: str = "dev.deckr.openhab.service",
    sender=None,
) -> Any:
    sender = sender or action_provider_address("provider-main")
    return service_message(
        sender=sender,
        sender_session_id="provider-session",
        recipient=service_address("openhab-home"),
        recipient_session_id="service-session",
        subject=entity_subject(
            "service",
            serviceId="openhab-home",
            namespace=namespace,
            name=name,
        ),
        body=ServiceMessageBody(
            serviceNamespace=namespace,
            name=name,
            intent=ServiceMessageIntent.COMMAND,
            exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
            params={},
        ),
        contract=_contract_pointer(contract),
    )


class _FakeServiceParticipant:
    participant = service_address("openhab-home")
    session_id = "service-session"

    def __init__(self, managed: tuple[ConcordManagedContract, ...]) -> None:
        self._managed = managed
        self.reconcile_calls = 0
        self.validate_calls: list[tuple[ContractHandle, dict[str, str]]] = []

    @property
    def managed_contracts(self) -> tuple[ConcordManagedContract, ...]:
        return self._managed

    async def reconcile(self, *, reason: str = "manual reconcile"):
        del reason
        self.reconcile_calls += 1
        return self._managed

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        self.validate_calls.append((contract, dict(current_sessions or {})))
        for managed in self._managed:
            if managed.contract.key == contract.key:
                return managed.validity
        return ContractValidity(ContractValidityStatus.MISSING_CONTRACT)


async def _receive_service_change(stream):
    with anyio.fail_after(1):
        return await anext(stream)


async def _assert_no_service_change(stream) -> None:
    received = None
    with anyio.move_on_after(0.05) as scope:
        received = await anext(stream)
    assert scope.cancel_called, f"unexpected service view change: {received!r}"


async def _eventually_descriptor_count(
    directory: BeaconDirectory[ServiceDescriptor],
    count: int,
) -> None:
    with anyio.fail_after(1):
        while len(directory.records()) != count:
            await anyio.sleep(0)


class _CountingBeacon:
    def __init__(self, beacon: Beacon) -> None:
        self._beacon = beacon
        self.candidate_calls = 0
        self.exact_candidate_calls = 0

    def candidates(self, *args, **kwargs):
        self.candidate_calls += 1
        return self._beacon.candidates(*args, **kwargs)

    async def candidates_exact(self, *args, **kwargs):
        self.exact_candidate_calls += 1
        return await self._beacon.candidates_exact(*args, **kwargs)

    def watch(self, *args, **kwargs):
        return self._beacon.watch(*args, **kwargs)

    def is_current(self) -> bool:
        return self._beacon.is_current()

    async def wait_current(self) -> None:
        await self._beacon.wait_current()


@pytest.mark.asyncio
async def test_parse_service_descriptor_validates_profile_identity() -> None:
    beacon = _memory_beacon()
    protocol = _protocol()
    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        await beacon.wait_ready()
        await _publish_service_advertisement(beacon, protocol)
        candidate = beacon.candidates(protocol.feature_id)[0]

        descriptor = parse_service_descriptor(candidate, protocol)
        assert descriptor is not None
        assert descriptor.namespace == protocol.namespace
        assert descriptor.endpoint == service_address("openhab-home")

        wrong_feature_protocol = _protocol()
        object.__setattr__(wrong_feature_protocol, "feature_id", protocol.namespace)
        assert parse_service_descriptor(candidate, wrong_feature_protocol) is None

        payload = protocol.advertisement_payload(
            service_id="openhab-home",
            session_id="service-session",
            backend_status=ServiceBackendStatus.AVAILABLE,
        ).to_dict()
        payload["profile"] = "wrong-profile"
        await _publish_service_advertisement(
            beacon,
            protocol,
            advertisement_id="ad-2",
            payload=payload,
        )
        wrong_profile = [
            item
            for item in beacon.candidates(protocol.feature_id)
            if item.advertisement.advertisement_id == "ad-2"
        ][0]
        assert parse_service_descriptor(wrong_profile, protocol) is None

        payload = protocol.advertisement_payload(
            service_id="openhab-home",
            session_id="service-session",
            backend_status=ServiceBackendStatus.AVAILABLE,
        ).to_dict()
        payload["serviceNamespace"] = "wrong-namespace"
        await _publish_service_advertisement(
            beacon,
            protocol,
            advertisement_id="ad-3",
            payload=payload,
        )
        wrong_namespace = [
            item
            for item in beacon.candidates(protocol.feature_id)
            if item.advertisement.advertisement_id == "ad-3"
        ][0]
        assert parse_service_descriptor(wrong_namespace, protocol) is None
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_descriptors_resolve_from_beacon_directory() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)
    counting_beacon = _CountingBeacon(beacon)
    protocol = _protocol()
    directory = BeaconDirectory(
        counting_beacon,
        protocol.feature_id,
        lambda candidate: parse_service_descriptor(candidate, protocol),
        log_label="ServiceTest",
    )

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        directory.start(task_group)
        await directory.wait_ready()
        assert directory.is_current()
        assert directory.records() == ()
        assert counting_beacon.exact_candidate_calls == 0

        await _publish_service_advertisement(beacon, protocol)
        await _publish_service_advertisement(
            beacon,
            protocol,
            service_id="openhab-backup",
            session_id="backup-session",
            advertisement_id="ad-2",
        )
        await _eventually_descriptor_count(directory, 2)

        descriptor = directory.resolve(
            lambda item: (
                item.service_id == "openhab-backup"
                and item.namespace == protocol.namespace
                and item.use_profile == protocol.use_profile
                and "sendCommand" in item.supported_operations
                and "items" in item.views
                and item.endpoint == service_address("openhab-backup")
                and item.session_id == "backup-session"
            ),
            select=newest_service_descriptor,
        )

        assert descriptor is not None
        assert descriptor.service_id == "openhab-backup"
        assert descriptor.views["items"].key_prefix == "views.openhab-backup.items."
        assert directory.resolve(lambda item: "missing" in item.supported_operations) is None
        assert directory.resolve(lambda item: "missing" in item.views) is None
        assert directory.resolve(lambda item: item.endpoint == service_address("missing")) is None
        assert counting_beacon.exact_candidate_calls == 0
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_directory_tracks_beacon_events_without_stale_descriptors() -> None:
    raw = MemoryJsonKvBucket(bucket="beacon", ttl_seconds=300)
    beacon = Beacon(raw)
    protocol = _protocol()
    directory = BeaconDirectory(
        beacon,
        protocol.feature_id,
        lambda candidate: parse_service_descriptor(candidate, protocol),
        log_label="ServiceTest",
    )

    async with anyio.create_task_group() as task_group:
        beacon.start(task_group)
        directory.start(task_group)
        await directory.wait_ready()

        advertisement = await _publish_service_advertisement(beacon, protocol)
        await _eventually_descriptor_count(directory, 1)

        await advertisement.update(payload={"invalid": "service-payload"})
        await _eventually_descriptor_count(directory, 0)

        await advertisement.update(
            payload=protocol.advertisement_payload(
                service_id="openhab-home",
                session_id="service-session",
                backend_status=ServiceBackendStatus.DEGRADED,
            ).to_dict()
        )
        await _eventually_descriptor_count(directory, 1)
        assert directory.records()[0].backend_status == ServiceBackendStatus.DEGRADED

        await advertisement.withdraw()
        await _eventually_descriptor_count(directory, 0)

        task_group.cancel_scope.cancel()


def test_service_protocol_allows_operation_only_declarations() -> None:
    protocol = ServiceProtocol(
        namespace="dev.deckr.test.service",
        feature_id="dev.deckr.test.feature",
        advertisement_profile="dev.deckr.test.advertisement.v1",
        use_profile="dev.deckr.test.use.v1",
        operations={"doThing": ServiceOperationDefinition()},
        messages={},
        view_families={
            "items": ServiceViewFamilyDefinition(
                storeName="deckr_test_view_v1",
                writer=ServiceViewWriter.SERVICE,
            )
        },
    )

    assert tuple(protocol.operations) == ("doThing",)
    assert not protocol.messages


def test_service_protocol_validates_message_operation_references() -> None:
    with pytest.raises(ValueError, match="unknown operation"):
        ServiceProtocol(
            namespace="dev.deckr.test.service",
            feature_id="dev.deckr.test.feature",
            advertisement_profile="dev.deckr.test.advertisement.v1",
            use_profile="dev.deckr.test.use.v1",
            operations={"doThing": ServiceOperationDefinition()},
            messages={
                "otherThing": ServiceMessageDefinition(
                    operation="missingThing",
                    intent=ServiceMessageIntent.COMMAND,
                    exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                    direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
                )
            },
            view_families={
                "items": ServiceViewFamilyDefinition(
                    storeName="deckr_test_view_v1",
                    writer=ServiceViewWriter.SERVICE,
                )
            },
        )


def test_service_protocol_allows_many_messages_per_operation() -> None:
    protocol = ServiceProtocol(
        namespace="dev.deckr.test.service",
        feature_id="dev.deckr.test.feature",
        advertisement_profile="dev.deckr.test.advertisement.v1",
        use_profile="dev.deckr.test.use.v1",
        operations={"volume": ServiceOperationDefinition()},
        messages={
            "setVolume": ServiceMessageDefinition(
                operation="volume",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            ),
            "adjustVolume": ServiceMessageDefinition(
                operation="volume",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            ),
        },
        view_families={},
    )

    assert protocol.messages["setVolume"].operation == "volume"
    assert protocol.messages["adjustVolume"].operation == "volume"


def test_service_protocol_requires_directional_operations() -> None:
    with pytest.raises(ValueError, match="require a declared operation"):
        ServiceProtocol(
            namespace="dev.deckr.test.service",
            feature_id="dev.deckr.test.feature",
            advertisement_profile="dev.deckr.test.advertisement.v1",
            use_profile="dev.deckr.test.use.v1",
            operations={"doThing": ServiceOperationDefinition()},
            messages={
                "doThing": ServiceMessageDefinition(
                    intent=ServiceMessageIntent.COMMAND,
                    exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                    direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
                )
            },
            view_families={},
        )

    protocol = ServiceProtocol(
        namespace="dev.deckr.test.service",
        feature_id="dev.deckr.test.feature",
        advertisement_profile="dev.deckr.test.advertisement.v1",
        use_profile="dev.deckr.test.use.v1",
        operations={},
        messages={
            "stateChanged": ServiceMessageDefinition(
                intent=ServiceMessageIntent.EVENT,
                exchangePattern=ServiceExchangePattern.ONE_WAY,
                direction=ServiceMessageDirection.SERVICE_TO_CONSUMER,
            )
        },
        view_families={},
    )
    assert protocol.messages["stateChanged"].operation is None


def test_service_payload_schema_requires_wire_safe_json_schema_contract() -> None:
    schema = ServicePayloadSchema(
        schemaId="dev.deckr.demo.payload.v1",
        schema={"type": "object"},
    )
    assert schema.to_dict() == {
        "schemaId": "dev.deckr.demo.payload.v1",
        "schema": {"type": "object"},
    }

    with pytest.raises(ValueError, match="must not be empty"):
        ServicePayloadSchema(
            schemaId="dev.deckr.demo.payload.v1",
            schema={},
        )

    with pytest.raises(ValueError, match="contract keyword"):
        ServicePayloadSchema(
            schemaId="dev.deckr.demo.payload.v1",
            schema={"description": "not a contract"},
        )

    with pytest.raises(ValueError, match="NaN or Infinity"):
        ServicePayloadSchema(
            schemaId="dev.deckr.demo.payload.v1",
            schema={"type": "number", "maximum": float("nan")},
        )


def test_service_view_family_definition_requires_writer() -> None:
    with pytest.raises(ValueError, match="writer"):
        ServiceViewFamilyDefinition(storeName="deckr_test_view_v1")


@pytest.mark.asyncio
async def test_authorize_service_message_matches_exact_contract_pointer() -> None:
    protocol, lease, _view_ref = await _service_view_context()
    managed = _managed_contract(lease.contract)
    participant = _FakeServiceParticipant((managed,))
    request = _service_message(
        lease.contract,
        name="setItemScope",
        namespace=protocol.namespace,
    )

    result = await authorize_service_message(
        participant,
        request,
        service_id="openhab-home",
        protocol=protocol,
        name="setItemScope",
    )

    assert isinstance(result, AuthorizedServiceMessage)
    assert result.contract == lease.contract
    assert result.record == managed.record
    assert result.body.name == "setItemScope"
    assert result.view_write_context.writer == ServiceViewWriter.SERVICE
    assert result.view_write_context.consumer_session_id == "provider-session"
    assert participant.reconcile_calls == 0
    assert participant.validate_calls == [
        (
            lease.contract,
            {str(action_provider_address("provider-main")): "provider-session"},
        )
    ]


@pytest.mark.asyncio
async def test_authorize_service_message_rejects_unmanaged_contract_pointer() -> None:
    protocol, lease, _view_ref = await _service_view_context()
    managed = _managed_contract(lease.contract)
    participant = _FakeServiceParticipant((managed,))
    other_contract = _contract_handle(contract_id="service-contract-2")
    request = _service_message(
        other_contract,
        name="setItemScope",
        namespace=protocol.namespace,
    )

    with pytest.raises(ServiceUseAuthorizationError) as exc_info:
        await authorize_service_message(
            participant,
            request,
            service_id="openhab-home",
            protocol=protocol,
            name="setItemScope",
        )

    assert exc_info.value.code == "contract_not_managed"
    assert participant.reconcile_calls == 1
    assert participant.validate_calls == []


@pytest.mark.asyncio
async def test_authorize_service_message_rejects_sender_not_named_by_contract() -> None:
    protocol, lease, _view_ref = await _service_view_context()
    managed = _managed_contract(lease.contract)
    participant = _FakeServiceParticipant((managed,))
    request = _service_message(
        lease.contract,
        sender=action_provider_address("other-provider"),
        namespace=protocol.namespace,
    )

    with pytest.raises(ServiceUseAuthorizationError) as exc_info:
        await authorize_service_message(
            participant,
            request,
            service_id="openhab-home",
            protocol=protocol,
            name="setItemScope",
        )

    assert exc_info.value.code == "scope_mismatch"
    assert participant.validate_calls == [
        (
            lease.contract,
            {str(action_provider_address("other-provider")): "provider-session"},
        )
    ]


@pytest.mark.asyncio
async def test_authorize_service_message_rejects_subject_scope_mismatch() -> None:
    protocol, lease, _view_ref = await _service_view_context()
    managed = _managed_contract(lease.contract)
    participant = _FakeServiceParticipant((managed,))
    request = _service_message(
        lease.contract,
        name="setItemScope",
        namespace=protocol.namespace,
    ).model_copy(
        update={
            "subject": entity_subject(
                "service",
                serviceId="other-service",
                namespace=protocol.namespace,
                name="setItemScope",
            )
        }
    )

    with pytest.raises(ServiceUseAuthorizationError) as exc_info:
        await authorize_service_message(
            participant,
            request,
            service_id="openhab-home",
            protocol=protocol,
            name="setItemScope",
        )

    assert exc_info.value.code == "scope_mismatch"
    assert participant.validate_calls == []


@pytest.mark.asyncio
async def test_authorize_service_message_rejects_name_outside_protocol() -> None:
    protocol, lease, _view_ref = await _service_view_context()
    protocol = _protocol(operations=("setItemScope",))
    managed = _managed_contract(lease.contract)
    participant = _FakeServiceParticipant((managed,))
    request = _service_message(
        lease.contract,
        name="sendCommand",
        namespace=protocol.namespace,
    )

    with pytest.raises(ServiceUseAuthorizationError) as exc_info:
        await authorize_service_message(
            participant,
            request,
            service_id="openhab-home",
            protocol=protocol,
            name="sendCommand",
        )

    assert exc_info.value.code == "scope_mismatch"


@pytest.mark.asyncio
async def test_service_view_store_uses_explicit_lease_scope() -> None:
    _protocol, lease, view_ref = await _service_view_context()
    view_store = ServiceViewStore(bucket=MemoryJsonKvBucket(bucket=view_ref.store_name))

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            context=_service_view_write_context(lease),
        )

        current = await view_store.get(_view_read_context(lease), view_ref)
        assert current is not None
        assert isinstance(current, ServiceViewEntry)
        assert current.value["state"] == "ON"

        async with view_store.watch(_view_read_context(lease), view_ref) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry == created
            updated = await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                context=_service_view_write_context(lease),
                revision=created.revision,
            )
            change = await _receive_service_change(changes)
            assert change.entry == updated

        with pytest.raises(KvConflict):
            await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "STALE"},
                context=_service_view_write_context(lease),
                revision=created.revision,
            )

        unauthorized = ServiceViewRef(
            "deckr_openhab_service_view_v1",
            "views.openhab-home.items-unrelated.Kitchen",
        )
        with pytest.raises(UnsupportedServiceScope):
            await view_store.get(_view_read_context(lease), unauthorized)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_enforces_declared_writer() -> None:
    _protocol, lease, view_ref = await _service_view_context()
    consumer_descriptor = ServiceDescriptor(
        candidate=lease.descriptor.candidate,
        service_id=lease.descriptor.service_id,
        namespace=lease.descriptor.namespace,
        endpoint=lease.descriptor.endpoint,
        session_id=lease.descriptor.session_id,
        advertisement_profile=lease.descriptor.advertisement_profile,
        use_profile=lease.descriptor.use_profile,
        supported_operations=lease.descriptor.supported_operations,
        supported_messages=lease.descriptor.supported_messages,
        views={
            "items": ServiceViewFamily(
                storeName=view_ref.store_name,
                keyPrefix=service_view_key("openhab-home", "items") + ".",
                writer=ServiceViewWriter.CONSUMER,
            )
        },
        backend_status=lease.descriptor.backend_status,
        diagnostics=lease.descriptor.diagnostics,
    )
    consumer_writer_lease = _FakeServiceUseLease(
        descriptor=consumer_descriptor,
        contract=lease.contract,
    )
    view_store = ServiceViewStore(bucket=MemoryJsonKvBucket(bucket=view_ref.store_name))

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        with pytest.raises(UnsupportedServiceScope):
            await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                context=_view_write_context(lease, ServiceViewWriter.CONSUMER),
            )

        with pytest.raises(UnsupportedServiceScope):
            await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                context=_view_write_context(
                    consumer_writer_lease,
                    ServiceViewWriter.SERVICE,
                ),
            )

        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            context=_view_write_context(
                consumer_writer_lease,
                ServiceViewWriter.CONSUMER,
            ),
        )
        assert created.writer == ServiceViewWriter.CONSUMER
        assert (
            await view_store.get(
                _view_read_context(consumer_writer_lease, ServiceViewWriter.SERVICE),
                view_ref,
            )
            == created
        )

        async with view_store.watch(
            _view_read_context(consumer_writer_lease, ServiceViewWriter.SERVICE),
            view_ref,
        ) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry == created
            updated = await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                context=_view_write_context(
                    consumer_writer_lease,
                    ServiceViewWriter.CONSUMER,
                ),
                revision=created.revision,
            )
            change = await _receive_service_change(changes)
            assert change.entry == updated

        with pytest.raises(UnsupportedServiceScope):
            await view_store.get(_view_read_context(consumer_writer_lease), view_ref)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_ignores_old_fenced_entries() -> None:
    _protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    await raw.put(
        _service_view_storage_key(view_ref, lease.contract),
        {
            "viewKey": view_ref.key,
            "serviceId": lease.descriptor.service_id,
            "serviceNamespace": lease.descriptor.namespace,
            "sessionId": lease.descriptor.session_id,
            "contractId": lease.contract.contract_id,
            "generation": lease.contract.generation,
            "item": "Kitchen Light",
            "state": "STALE",
        },
    )
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        assert await view_store.get(_view_read_context(lease), view_ref) is None

        current = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            context=_service_view_write_context(lease),
        )
        assert await view_store.get(_view_read_context(lease), view_ref) == current
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_logs_exact_write_after_view_observation(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="deckr.services.views")
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        async with view_store.watch(_view_read_context(lease), view_ref) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry is None
            created = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                context=_service_view_write_context(lease),
            )
            applied = await _receive_service_change(changes)
            assert applied.entry == created

        assert "Service view write" in caplog.text
        assert "payload_hash=" in caplog.text
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_hides_removals_for_never_visible_fenced_entry() -> None:
    protocol, lease, view_ref = await _service_view_context()
    other_lease = _FakeServiceUseLease(
        descriptor=lease.descriptor,
        contract=_contract_handle(contract_id="service-contract-2"),
    )
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        async with view_store.watch(_view_read_context(lease), view_ref) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry is None
            first_hidden = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                context=_service_view_write_context(other_lease),
            )

            await view_store.delete(
                view=view_ref,
                context=_service_view_write_context(other_lease),
                revision=first_hidden.revision,
            )

            second_hidden = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                context=_service_view_write_context(other_lease),
            )

            await raw.expire(second_hidden.storage_key)
            with anyio.fail_after(1):
                while (
                    view_store._observed_revision[second_hidden.storage_key]
                    <= second_hidden.revision
                ):
                    await anyio.sleep(0)
            await _assert_no_service_change(changes)

        assert await view_store.get(_view_read_context(lease), view_ref) is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_get_waits_while_materialized_view_stale() -> None:
    _protocol, lease, view_ref = await _service_view_context()
    raw = _RecoveringServiceViewBucket(bucket=view_ref.store_name)
    raw.add(
        _service_view_storage_key(view_ref, lease.contract),
        _service_view_payload(view_ref, lease),
    )
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        assert await view_store.get(_view_read_context(lease), view_ref) is not None

        raw.pause_next_watch()
        raw.close_current_watch()
        with anyio.fail_after(1):
            await raw.wait_next_watch_paused()

        with anyio.move_on_after(0.05) as scope:
            await view_store.get(_view_read_context(lease), view_ref)
        assert scope.cancelled_caught

        raw.remove_without_publish(_service_view_storage_key(view_ref, lease.contract))
        raw.resume_next_watch()
        with anyio.fail_after(1):
            while await view_store.get(_view_read_context(lease), view_ref) is not None:
                await anyio.sleep(0)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_resnapshots_after_changed_key_overflow() -> None:
    _protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_current()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            context=_service_view_write_context(lease),
        )
        async with view_store.watch(_view_read_context(lease), view_ref) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry == created
            await view_store._state.publish(  # noqa: SLF001
                f"unrelated-{index}" for index in range(257)
            )
            resnapshot = await _receive_service_change(changes)
            assert resnapshot.entry == created
            assert resnapshot.resnapshot_required
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_delete_updates_cache_immediately() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            context=_service_view_write_context(lease),
        )

        async with view_store.watch(_view_read_context(lease), view_ref) as changes:
            initial = await _receive_service_change(changes)
            assert initial.entry == created
            await view_store.delete(
                view=view_ref,
                context=_service_view_write_context(lease),
                revision=created.revision,
            )
            change = await _receive_service_change(changes)

        assert change.entry is None
        assert await view_store.get(_view_read_context(lease), view_ref) is None
        tg.cancel_scope.cancel()


class _FakeServiceUseLease:
    def __init__(
        self,
        *,
        descriptor: ServiceDescriptor,
        contract: ContractHandle | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.contract = contract or _contract_handle()
        self.consumer_endpoint = action_provider_address("provider-main")
        self.consumer_session_id = "provider-session"

    async def refresh(self) -> None:
        return None


class _RecoveringServiceViewBucket:
    def __init__(self, *, bucket: str) -> None:
        self.bucket = bucket
        self._revision = 0
        self._entries: dict[str, KvEntry] = {}
        self._close_events: list[anyio.Event] = []
        self._pause_next_watch = False
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

    def add(self, key: str, value: Mapping[str, Any]) -> KvEntry:
        self._revision += 1
        entry = KvEntry(self.bucket, key, kv_value(value), self._revision)
        self._entries[key] = entry
        return entry

    def remove_without_publish(self, key: str) -> None:
        if key in self._entries:
            self._revision += 1
            self._entries.pop(key)

    def close_current_watch(self) -> None:
        self._close_events[-1].set()

    def pause_next_watch(self) -> None:
        self._pause_next_watch = True
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

    async def wait_next_watch_paused(self) -> None:
        await self._watch_paused.wait()

    def resume_next_watch(self) -> None:
        self._resume_watch.set()

    async def get(self, key: str) -> KvEntry | None:
        return self._entries.get(key)

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[
        anyio.abc.ObjectReceiveStream[KvChange | KvWatchBarrier]
    ]:
        if self._pause_next_watch:
            self._pause_next_watch = False
            self._watch_paused.set()
            await self._resume_watch.wait()
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[
            KvChange | KvWatchBarrier
        ](100)
        snapshot = tuple(
            entry for key, entry in sorted(self._entries.items()) if key.startswith(prefix)
        )
        barrier = KvWatchBarrier(self._revision)

        async def run() -> None:
            for entry in snapshot:
                await send.send(
                    KvChange(self.bucket, entry.key, entry.revision, "put", entry)
                )
            await send.send(barrier)
            await close_event.wait()
            await send.aclose()

        async with send, receive, anyio.create_task_group() as task_group:
            task_group.start_soon(run)
            yield receive
            task_group.cancel_scope.cancel()
