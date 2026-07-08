from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    ConcordConflict,
    ConcordManagedContract,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import entity_subject, service_address
from deckr.services import (
    DeckrServices,
    ManagedServiceContract,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceError,
    ServiceExchangePattern,
    ServiceMessageBody,
    ServiceMessageDefinition,
    ServiceMessageDirection,
    ServiceMessageIntent,
    ServiceMessageStatus,
    ServiceOperationDefinition,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceViewFamily,
    ServiceViewFamilyDefinition,
    ServiceViewRef,
    ServiceViewWriter,
    service_message_ends_service_use,
    service_unavailable_ends_service_use,
)
from deckr.services.messages import service_message, service_response_message
from deckr.substrates.nats_kv import KvUnavailable

_CLIENT_ADDRESS = action_provider_address("python-dev.deckr.demo")


def _protocol() -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.demo.service",
        feature_id="dev.deckr.demo.service.v1",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        operations={"play": ServiceOperationDefinition()},
        messages={
            "play": ServiceMessageDefinition(
                operation="play",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            ),
        },
        view_families={
            "zones": ServiceViewFamilyDefinition(
                storeName="demo_views",
                writer=ServiceViewWriter.SERVICE,
            ),
        },
    )


def _directory_key(protocol: ServiceProtocol) -> tuple[str, str, str, str]:
    return (
        protocol.namespace,
        protocol.feature_id,
        protocol.advertisement_profile,
        protocol.use_profile,
    )


def _descriptor() -> ServiceDescriptor:
    return ServiceDescriptor(
        candidate=None,
        service_id="demo-home",
        namespace="dev.deckr.demo.service",
        endpoint=service_address("demo-home"),
        session_id="service-session",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        supported_operations=frozenset({"play"}),
        supported_messages={
            "play": ServiceMessageDefinition(
                operation="play",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.CONSUMER_TO_SERVICE,
            ),
        },
        views={
            "zones": ServiceViewFamily(
                storeName="demo_views",
                keyPrefix="zones/demo-home/",
                writer=ServiceViewWriter.SERVICE,
            ),
        },
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={},
    )


def _service_to_consumer_protocol() -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.demo.service",
        feature_id="dev.deckr.demo.service.v1",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        operations={"collectDiagnostics": ServiceOperationDefinition()},
        messages={
            "stateChanged": ServiceMessageDefinition(
                intent=ServiceMessageIntent.EVENT,
                exchangePattern=ServiceExchangePattern.ONE_WAY,
                direction=ServiceMessageDirection.SERVICE_TO_CONSUMER,
            ),
            "collectDiagnostics": ServiceMessageDefinition(
                operation="collectDiagnostics",
                intent=ServiceMessageIntent.QUERY,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                direction=ServiceMessageDirection.SERVICE_TO_CONSUMER,
            ),
        },
        view_families={},
    )


def _service_to_consumer_descriptor() -> ServiceDescriptor:
    protocol = _service_to_consumer_protocol()
    return ServiceDescriptor(
        candidate=None,
        service_id="demo-home",
        namespace=protocol.namespace,
        endpoint=service_address("demo-home"),
        session_id="service-session",
        advertisement_profile=protocol.advertisement_profile,
        use_profile=protocol.use_profile,
        supported_operations=frozenset(protocol.operations),
        supported_messages=protocol.messages,
        views={},
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={},
    )


def _managed_service_contract(
    *,
    status: ContractValidityStatus = ContractValidityStatus.VALID,
    service_session_id: str = "service-session",
    consumer_session_id: str = "client-session",
) -> ConcordManagedContract:
    participants = tuple(sorted((service_address("demo-home"), _CLIENT_ADDRESS), key=str))
    contract = ContractHandle(
        key="contract-1:1",
        contract_id="contract-1",
        generation=1,
        participants=participants,
        attached_participants=participants,
        revision=1,
        state=ContractState.OPEN,
        profile="dev.deckr.demo.use.v1",
    )
    record = ContractRecord(
        contract_id=contract.contract_id,
        generation=contract.generation,
        participants=participants,
        attached_participants=participants,
        state=contract.state,
        profile=contract.profile,
    )
    service_token = ParticipantHandle(
        key="contract-1:1:service",
        contract_id=contract.contract_id,
        generation=contract.generation,
        participant=service_address("demo-home"),
        session_id=service_session_id,
        token_id="service-token",
        revision=1,
        refresh_seq=1,
        ttl_seconds=30,
    )
    consumer_token = ParticipantHandle(
        key="contract-1:1:consumer",
        contract_id=contract.contract_id,
        generation=contract.generation,
        participant=_CLIENT_ADDRESS,
        session_id=consumer_session_id,
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
            reason=None if status == ContractValidityStatus.VALID else status.value,
        ),
        token=service_token,
    )


class _FakeManagedServiceParticipant:
    participant = service_address("demo-home")
    session_id = "service-session"

    def __init__(
        self,
        managed: tuple[ConcordManagedContract, ...],
        *,
        after_reconcile: tuple[ConcordManagedContract, ...] | None = None,
    ) -> None:
        self._managed = managed
        self._after_reconcile = after_reconcile
        self.reconcile_calls = 0
        self.validate_calls: list[tuple[ContractHandle, dict[str, str]]] = []

    def managed_contract(
        self,
        contract: ContractHandle,
    ) -> ConcordManagedContract | None:
        for managed in self._managed:
            if managed.contract.key == contract.key:
                return managed
        return None

    async def reconcile(
        self,
        *,
        reason: str = "manual reconcile",
    ) -> tuple[ConcordManagedContract, ...]:
        del reason
        self.reconcile_calls += 1
        if self._after_reconcile is not None:
            self._managed = self._after_reconcile
        return self._managed

    async def validate(
        self,
        contract: ContractHandle,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        self.validate_calls.append((contract, dict(current_sessions or {})))
        managed = self.managed_contract(contract)
        if managed is None:
            return ContractValidity(
                ContractValidityStatus.MISSING_CONTRACT,
                reason="missing",
            )
        return managed.validity


def _contract() -> SimpleNamespace:
    return SimpleNamespace(
        contract_id="contract-1",
        generation=1,
        profile="dev.deckr.demo.use.v1",
    )


def _lease(
    descriptor: ServiceDescriptor | None = None,
    *,
    refresh: AsyncMock | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        descriptor=descriptor or _descriptor(),
        contract=_contract(),
        refresh=refresh or AsyncMock(),
    )


def _services(
    *,
    endpoint,
    concord,
    task_group=None,
    kv_bucket_for=None,
) -> DeckrServices:
    return DeckrServices(
        endpoint=endpoint,
        beacon=SimpleNamespace(),
        concord=concord,
        task_group=task_group or SimpleNamespace(start_soon=Mock()),
        kv_bucket_for=kv_bucket_for or Mock(),
    )


def test_directory_reuses_managed_view() -> None:
    task_group = SimpleNamespace(start_soon=Mock())
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
        task_group=task_group,
    )

    first = services.directory(_protocol())
    second = services.directory(_protocol())

    assert first is second
    task_group.start_soon.assert_called_once()
    assert not hasattr(services, "beacon")
    assert not hasattr(services, "concord")


def test_resolve_descriptor_translates_directory_unavailable() -> None:
    protocol = _protocol()
    directory = SimpleNamespace(
        is_current=Mock(return_value=False),
        resolve=Mock(side_effect=AssertionError("resolve should not run")),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = directory  # noqa: SLF001

    with pytest.raises(ServiceUnavailable) as exc_info:
        services.resolve_descriptor(protocol)

    assert exc_info.value.code == "service_discovery_pending"
    directory.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_descriptor_translates_directory_unavailable() -> None:
    protocol = _protocol()
    directory = SimpleNamespace(
        wait_for=AsyncMock(side_effect=KvUnavailable("directory unavailable")),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = directory  # noqa: SLF001

    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.wait_for_descriptor(protocol)

    assert exc_info.value.code == "beacon_unavailable"
    directory.wait_for.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_view_translates_kv_unavailable() -> None:
    def kv_bucket_for(_policy):
        raise KvUnavailable("view store unavailable")

    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
        kv_bucket_for=kv_bucket_for,
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.read_view(
            _lease(),
            ServiceViewRef("demo_views", "zones/demo-home/Kitchen"),
        )

    assert exc_info.value.code == "service_view_unavailable"


@pytest.mark.asyncio
async def test_use_matching_resolves_descriptor_and_negotiates() -> None:
    protocol = _protocol()
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(return_value=ContractValidity(ContractValidityStatus.VALID)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    concord = SimpleNamespace(propose=AsyncMock(return_value=agreement))
    task_group = SimpleNamespace(start_soon=Mock())
    directory = SimpleNamespace(wait_for=AsyncMock(return_value=_descriptor()))
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=concord,
        task_group=task_group,
    )
    services._directories[_directory_key(protocol)] = directory  # noqa: SLF001

    async with services.use_matching(
        protocol,
        predicate=lambda descriptor: descriptor.service_id == "demo-home",
        timeout_seconds=10.0,
    ) as lease:
        assert lease.descriptor.service_id == "demo-home"

    directory.wait_for.assert_awaited_once()
    assert directory.wait_for.await_args.kwargs["timeout"] <= 10.0
    spec = concord.propose.await_args.args[0]
    assert spec.local_participant == _CLIENT_ADDRESS
    assert spec.terms is None
    agreement.cancel.assert_awaited_once_with("service_use_closed")


@pytest.mark.asyncio
async def test_aclose_closes_active_service_use_lease_once() -> None:
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(return_value=ContractValidity(ContractValidityStatus.VALID)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=AsyncMock(return_value=agreement)),
    )
    context = services.use(_descriptor())

    lease = await context.__aenter__()
    assert lease.descriptor.service_id == "demo-home"

    await services.aclose()

    agreement.cancel.assert_awaited_once_with("service_use_closed")
    agreement.aclose.assert_awaited_once()

    await context.__aexit__(None, None, None)

    agreement.cancel.assert_awaited_once()
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_explicit_timeout_cancels_pending_contract() -> None:
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(
            return_value=ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED)
        ),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=AsyncMock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            timeout_seconds=0.01,
        ):
            pass

    assert exc_info.value.code == "contract_timeout"
    agreement.cancel.assert_awaited_once_with("contract_timeout")
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_explicit_timeout_covers_proposal_wait() -> None:
    agreement = SimpleNamespace(cancel=AsyncMock(), aclose=AsyncMock())
    wait_forever = anyio.Event()

    async def propose(_spec, *, start_soon=None):
        del start_soon
        await wait_forever.wait()
        return agreement

    concord = SimpleNamespace(propose=AsyncMock(side_effect=propose))
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=concord,
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            timeout_seconds=0.01,
        ):
            pass

    assert exc_info.value.code == "contract_timeout"
    assert exc_info.value.diagnostics["contractId"] is None
    concord.propose.assert_awaited_once()
    agreement.cancel.assert_not_awaited()
    agreement.aclose.assert_not_awaited()


@pytest.mark.parametrize(
    ("message", "expected_code"),
    (
        ("Concord contract is cancelled", "contract_cancelled"),
        ("Concord contract 'abc' is cancelled", "contract_cancelled"),
        ("Concord contract is missing", "contract_missing_contract"),
        ("Concord contract 'abc' is missing", "contract_missing_contract"),
        ("Concord participant token is invalid", "contract_invalid_token"),
        ("Concord participant token changed owner", "contract_invalid_token"),
    ),
)
@pytest.mark.asyncio
async def test_use_translates_terminal_protocol_conflict_during_negotiation(
    message: str,
    expected_code: str,
) -> None:
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(side_effect=ConcordConflict(message)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=AsyncMock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            timeout_seconds=1.0,
        ):
            pass

    assert exc_info.value.code == expected_code
    assert exc_info.value.diagnostics["reason"] == message
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_translates_unknown_protocol_conflict_to_service_use_conflict() -> None:
    message = "Concord participant is already attached"
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(side_effect=ConcordConflict(message)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=AsyncMock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            timeout_seconds=1.0,
        ):
            pass

    assert exc_info.value.code == "service_use_conflict"
    assert exc_info.value.diagnostics["reason"] == message
    agreement.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("message", "expected_code", "expected_status"),
    (
        (
            "Concord participant token is missing",
            "contract_missing_token",
            ContractValidityStatus.MISSING_TOKEN.value,
        ),
    ),
)
@pytest.mark.asyncio
async def test_service_use_lease_refresh_preserves_terminal_protocol_conflicts(
    message: str,
    expected_code: str,
    expected_status: str,
) -> None:
    descriptor = _descriptor()
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(side_effect=ConcordConflict(message)),
    )
    lease = ServiceUseLease(
        agreement=agreement,
        descriptor=descriptor,
        consumer_endpoint=_CLIENT_ADDRESS,
        consumer_session_id="client-session",
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await lease.refresh()

    assert exc_info.value.code == expected_code
    assert exc_info.value.diagnostics["status"] == expected_status
    assert exc_info.value.diagnostics["reason"] == message


@pytest.mark.asyncio
async def test_service_use_lease_refresh_preserves_valid_lease_when_unavailable() -> None:
    descriptor = _descriptor()
    agreement = SimpleNamespace(
        contract=_contract(),
        valid=True,
        refresh=AsyncMock(
            return_value=ContractValidity(ContractValidityStatus.UNAVAILABLE)
        ),
    )
    lease = ServiceUseLease(
        agreement=agreement,
        descriptor=descriptor,
        consumer_endpoint=_CLIENT_ADDRESS,
        consumer_session_id="client-session",
    )

    await lease.refresh()

    agreement.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_use_lease_refresh_raises_unavailable_without_valid_lease() -> None:
    descriptor = _descriptor()
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(
            return_value=ContractValidity(ContractValidityStatus.UNAVAILABLE)
        ),
    )
    lease = ServiceUseLease(
        agreement=agreement,
        descriptor=descriptor,
        consumer_endpoint=_CLIENT_ADDRESS,
        consumer_session_id="client-session",
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await lease.refresh()

    assert exc_info.value.code == "contract_unavailable"
    assert exc_info.value.diagnostics["status"] == ContractValidityStatus.UNAVAILABLE.value


@pytest.mark.asyncio
async def test_service_message_timeout_is_separate_from_service_use() -> None:
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(return_value=ContractValidity(ContractValidityStatus.VALID)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )

    async def request(**kwargs):
        return service_response_message(
            sender=service_address("demo-home"),
            sender_session_id="service-session",
            recipient=_CLIENT_ADDRESS,
            recipient_session_id="client-session",
            subject=kwargs["subject"],
            in_reply_to="request-message",
            contract=kwargs["contract"],
            body=ServiceMessageBody(
                serviceNamespace="dev.deckr.demo.service",
                name=kwargs["body"]["name"],
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                status=ServiceMessageStatus.OK,
                result={"ok": True},
            ),
        )

    endpoint = SimpleNamespace(
        address=_CLIENT_ADDRESS,
        session_id="client-session",
        request=AsyncMock(side_effect=request),
    )
    services = _services(
        endpoint=endpoint,
        concord=SimpleNamespace(propose=AsyncMock(return_value=agreement)),
    )

    async with services.use(
        _descriptor(),
        timeout_seconds=30.0,
    ) as lease:
        reply = await services.request(
            lease,
            "play",
            {"zone": "Kitchen"},
            timeout_seconds=12.0,
        )

    assert reply.status == ServiceMessageStatus.OK
    assert endpoint.request.await_args.kwargs["timeout"] == 12.0
    assert endpoint.request.await_args.kwargs["contract"] == {
        "contractId": "contract-1",
        "generation": 1,
    }


@pytest.mark.asyncio
async def test_managed_service_contract_sends_service_to_consumer_message() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant((managed,))
    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        send=AsyncMock(return_value="sent"),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    assert await channel.send("stateChanged", event={"state": "ready"}) == "sent"

    assert endpoint.send.await_args.kwargs["recipient"] == _CLIENT_ADDRESS
    assert endpoint.send.await_args.kwargs["recipient_session_id"] == "client-session"
    assert endpoint.send.await_args.kwargs["message_type"] == "serviceMessage"
    assert endpoint.send.await_args.kwargs["body"] == {
        "serviceNamespace": "dev.deckr.demo.service",
        "name": "stateChanged",
        "intent": "event",
        "exchangePattern": "one_way",
        "params": {},
        "event": {"state": "ready"},
    }
    assert participant.reconcile_calls == 0
    assert participant.validate_calls == [
        (managed.contract, {str(_CLIENT_ADDRESS): "client-session"})
    ]


@pytest.mark.asyncio
async def test_managed_service_contract_request_validates_response_metadata() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant((managed,))

    async def request(**kwargs):
        return service_response_message(
            sender=_CLIENT_ADDRESS,
            sender_session_id="client-session",
            recipient=service_address("demo-home"),
            recipient_session_id="service-session",
            subject=kwargs["subject"],
            in_reply_to="request-message",
            contract=kwargs["contract"],
            body=ServiceMessageBody(
                serviceNamespace="dev.deckr.demo.service",
                name="collectDiagnostics",
                intent=ServiceMessageIntent.QUERY,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                status=ServiceMessageStatus.OK,
                result={"ok": True},
            ),
        )

    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        request=AsyncMock(side_effect=request),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    reply = await channel.request("collectDiagnostics", timeout_seconds=3.0)

    assert reply.status is ServiceMessageStatus.OK
    assert endpoint.request.await_args.kwargs["timeout"] == 3.0
    assert participant.validate_calls == [
        (managed.contract, {str(_CLIENT_ADDRESS): "client-session"})
    ]


@pytest.mark.asyncio
async def test_managed_service_contract_rejects_response_subject_mismatch() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant((managed,))

    async def request(**kwargs):
        return service_response_message(
            sender=_CLIENT_ADDRESS,
            sender_session_id="client-session",
            recipient=service_address("demo-home"),
            recipient_session_id="service-session",
            subject=entity_subject(
                "service",
                serviceId="demo-home",
                namespace="dev.deckr.demo.service",
                name="stateChanged",
            ),
            in_reply_to="request-message",
            contract=kwargs["contract"],
            body=ServiceMessageBody(
                serviceNamespace="dev.deckr.demo.service",
                name="collectDiagnostics",
                intent=ServiceMessageIntent.QUERY,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                status=ServiceMessageStatus.OK,
                result={"ok": True},
            ),
        )

    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        request=AsyncMock(side_effect=request),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await channel.request("collectDiagnostics")

    assert exc_info.value.code == "invalid_service_response"


@pytest.mark.asyncio
async def test_managed_service_contract_reconciles_once_before_send() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant((), after_reconcile=(managed,))
    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        send=AsyncMock(return_value="sent"),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    assert await channel.send("stateChanged") == "sent"
    assert participant.reconcile_calls == 1
    assert participant.validate_calls == [
        (managed.contract, {str(_CLIENT_ADDRESS): "client-session"})
    ]


@pytest.mark.asyncio
async def test_managed_service_contract_fails_closed_when_contract_missing() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant(())
    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        send=AsyncMock(),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await channel.send("stateChanged")

    assert exc_info.value.code == "contract_not_managed"
    assert participant.reconcile_calls == 1
    assert endpoint.send.await_count == 0


@pytest.mark.parametrize(
    "status",
    [
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.SESSION_MISMATCH,
    ],
)
@pytest.mark.asyncio
async def test_managed_service_contract_fails_closed_when_contract_invalid(
    status: ContractValidityStatus,
) -> None:
    managed = _managed_service_contract(status=status)
    participant = _FakeManagedServiceParticipant((managed,))
    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
        send=AsyncMock(),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_service_to_consumer_protocol(),
        service_id="demo-home",
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await channel.send("stateChanged")

    assert exc_info.value.code == f"contract_{status.value}"
    assert endpoint.send.await_count == 0


@pytest.mark.asyncio
async def test_managed_service_view_access_validates_before_operations() -> None:
    managed = _managed_service_contract()
    participant = _FakeManagedServiceParticipant((managed,))
    endpoint = SimpleNamespace(
        address=service_address("demo-home"),
        session_id="service-session",
    )
    store = SimpleNamespace(
        get=AsyncMock(return_value=None),
        put=AsyncMock(return_value=SimpleNamespace(value={"ok": True})),
    )
    channel = ManagedServiceContract(
        endpoint=endpoint,
        participant=participant,
        contract=managed.contract,
        protocol=_protocol(),
        service_id="demo-home",
    )
    view = ServiceViewRef("demo_views", "views.demo-home.zones.Kitchen")
    access = channel.view_access(store)

    assert await access.read(view) is None
    await access.put(view, {"ok": True})

    assert participant.validate_calls == [
        (managed.contract, {str(_CLIENT_ADDRESS): "client-session"}),
        (managed.contract, {str(_CLIENT_ADDRESS): "client-session"}),
    ]
    assert store.get.await_args.args[0].reader is ServiceViewWriter.SERVICE
    assert store.put.await_args.kwargs["context"].writer is ServiceViewWriter.SERVICE


@pytest.mark.asyncio
async def test_authorize_inbound_message_validates_service_to_consumer_scope() -> None:
    descriptor = _service_to_consumer_descriptor()
    lease = _lease(descriptor)
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    message = service_message(
        sender=descriptor.endpoint,
        sender_session_id=descriptor.session_id,
        recipient=_CLIENT_ADDRESS,
        recipient_session_id="client-session",
        subject=entity_subject(
            "service",
            serviceId=descriptor.service_id,
            namespace=descriptor.namespace,
            name="stateChanged",
        ),
        body=ServiceMessageBody(
            serviceNamespace=descriptor.namespace,
            name="stateChanged",
            intent=ServiceMessageIntent.EVENT,
            exchangePattern=ServiceExchangePattern.ONE_WAY,
            event={"state": "ready"},
        ),
        contract=ContractPointer(contractId="contract-1", generation=1),
    )

    body = await services.authorize_inbound_message(lease, message)

    assert body.name == "stateChanged"

    wrong_subject = message.model_copy(
        update={
            "subject": entity_subject(
                "service",
                serviceId=descriptor.service_id,
                namespace=descriptor.namespace,
                name="collectDiagnostics",
            )
        }
    )
    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.authorize_inbound_message(lease, wrong_subject)
    assert exc_info.value.code == "invalid_service_response"

    wrong_session = message.model_copy(update={"sender_session_id": "old-session"})
    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.authorize_inbound_message(lease, wrong_session)
    assert exc_info.value.code == "invalid_service_response"

    consumer_to_service = message.model_copy(
        update={
            "body": ServiceMessageBody(
                serviceNamespace="dev.deckr.demo.service",
                name="play",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                params={},
            ).to_dict()
        }
    )
    consumer_to_service_lease = _lease(_descriptor())
    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.authorize_inbound_message(
            consumer_to_service_lease,
            consumer_to_service,
        )
    assert exc_info.value.code == "unsupported_service_message_direction"


@pytest.mark.asyncio
async def test_watch_view_refreshes_lease_before_delivering_changes() -> None:
    view = ServiceViewRef("demo_views", "zones/demo-home/Kitchen")
    current_payload = {
        "viewKey": view.key,
        "serviceId": "demo-home",
        "serviceNamespace": "dev.deckr.demo.service",
        "sessionId": "service-session",
        "contractId": "contract-1",
        "generation": 1,
        "volume": 12,
    }
    changed_payload = {**current_payload, "volume": 13}
    lease = _lease(
        refresh=AsyncMock(
            side_effect=[
                None,
                None,
                ServiceUnavailable(
                    "contract_session_mismatch",
                    "session mismatch",
                ),
            ]
        ),
    )

    async def changes():
        yield SimpleNamespace(entry=SimpleNamespace(value=changed_payload))

    @asynccontextmanager
    async def watch(_context, _view):
        yield changes()

    store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(value=current_payload)),
        watch=watch,
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._view_stores["demo_views"] = store  # noqa: SLF001

    stream = services.watch_view(lease, view)
    assert await anext(stream) == current_payload
    with pytest.raises(ServiceUnavailable) as exc_info:
        await anext(stream)
    await stream.aclose()

    assert exc_info.value.code == "contract_session_mismatch"
    assert lease.refresh.await_count == 3


@pytest.mark.asyncio
async def test_watch_view_does_not_emit_idle_duplicate_payloads() -> None:
    view = ServiceViewRef("demo_views", "zones/demo-home/Kitchen")
    current_payload = {
        "viewKey": view.key,
        "serviceId": "demo-home",
        "serviceNamespace": "dev.deckr.demo.service",
        "sessionId": "service-session",
        "contractId": "contract-1",
        "generation": 1,
        "volume": 12,
    }

    async def changes():
        await anyio.sleep_forever()
        yield None

    @asynccontextmanager
    async def watch(_context, _view):
        yield changes()

    store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(value=current_payload)),
        watch=watch,
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._view_stores["demo_views"] = store  # noqa: SLF001
    lease = _lease()

    stream = services.watch_view(lease, view)
    assert await anext(stream) == current_payload
    with anyio.move_on_after(0.05) as scope:
        await anext(stream)
    await stream.aclose()

    assert scope.cancel_called
    read_context = store.get.await_args.args[0]
    assert read_context.reader is ServiceViewWriter.CONSUMER
    assert store.get.await_args.args[1] == view
    assert lease.refresh.await_count == 2


@pytest.mark.asyncio
async def test_view_access_refreshes_lease_before_operations() -> None:
    view = ServiceViewRef("demo_views", "zones/demo-home/Kitchen")
    lease = _lease()
    store = SimpleNamespace(
        get=AsyncMock(return_value=None),
        put=AsyncMock(return_value=SimpleNamespace(value={"ok": True})),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._view_stores["demo_views"] = store  # noqa: SLF001

    access = services.view_access(lease, "demo_views")

    assert await access.read(view) is None
    await access.put(view, {"ok": True})

    assert lease.refresh.await_count == 2
    assert store.get.await_args.args[0].reader is ServiceViewWriter.CONSUMER
    assert store.put.await_args.kwargs["context"].writer is ServiceViewWriter.CONSUMER


def test_service_unavailable_helper_classifies_service_use_loss() -> None:
    assert service_unavailable_ends_service_use(
        ServiceUnavailable("contract_not_managed", "not managed")
    )
    assert service_unavailable_ends_service_use(
        ServiceUnavailable("contract_missing_contract", "missing")
    )
    assert service_unavailable_ends_service_use(
        ServiceUnavailable("service_use_conflict", "conflict")
    )
    assert service_unavailable_ends_service_use(
        ServiceUnavailable(
            "service_message_failed",
            "failed",
            {"reason": "contract_not_managed"},
        )
    )
    assert not service_unavailable_ends_service_use(
        ServiceUnavailable("contract_unavailable", "contract view unavailable")
    )
    assert not service_unavailable_ends_service_use(
        ServiceUnavailable("service_backend_unavailable", "backend down")
    )


def test_service_message_helper_matches_service_unavailable_helper() -> None:
    unavailable = ServiceUnavailable("contract_missing_token", "missing token")
    reply = ServiceMessageBody(
        serviceNamespace="dev.deckr.demo.service",
        name="play",
        intent=ServiceMessageIntent.COMMAND,
        exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
        status=ServiceMessageStatus.UNAVAILABLE,
        error=ServiceError(
            code="service_use_contract_invalid",
            message="missing token",
            diagnostics={"status": "missing_token"},
        ),
    )
    ordinary = ServiceMessageBody(
        serviceNamespace="dev.deckr.demo.service",
        name="play",
        intent=ServiceMessageIntent.COMMAND,
        exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
        status=ServiceMessageStatus.UNAVAILABLE,
        error=ServiceError(
            code="service_backend_unavailable",
            message="backend down",
        ),
    )

    assert service_unavailable_ends_service_use(unavailable)
    assert service_message_ends_service_use(reply)
    assert not service_message_ends_service_use(ordinary)
