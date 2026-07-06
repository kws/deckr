from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from deckr.actions.endpoints import action_provider_address
from deckr.concord import ConcordConflict, ContractValidity, ContractValidityStatus
from deckr.contracts.messages import service_address
from deckr.services import (
    DeckrServices,
    ServiceBackendStatus,
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    ServiceDescriptor,
    ServiceError,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceViewFamily,
    ServiceViewFamilyDefinition,
    ServiceViewRef,
    service_command_reply_ends_service_use,
    service_unavailable_ends_service_use,
    service_use_terms,
)
from deckr.services.messages import service_command_reply_message
from deckr.substrates.nats_kv import KvUnavailable

_CLIENT_ADDRESS = action_provider_address("python-dev.deckr.demo")


def _protocol() -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.demo.service",
        feature_id="dev.deckr.demo.service.v1",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        operations=("play",),
        view_families={
            "zones": ServiceViewFamilyDefinition(storeName="demo_views"),
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
        views={
            "zones": ServiceViewFamily(
                storeName="demo_views",
                keyPrefix="zones/demo-home/",
            ),
        },
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={},
    )


def _contract() -> SimpleNamespace:
    return SimpleNamespace(
        contract_id="contract-1",
        generation=1,
        profile="dev.deckr.demo.use.v1",
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
            SimpleNamespace(),
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
        operations={"play"},
        predicate=lambda descriptor: descriptor.service_id == "demo-home",
        timeout_seconds=10.0,
    ) as lease:
        assert lease.descriptor.service_id == "demo-home"
        assert lease.terms.allowed_operations == ("play",)

    directory.wait_for.assert_awaited_once()
    assert directory.wait_for.await_args.kwargs["timeout"] <= 10.0
    spec = concord.propose.await_args.args[0]
    assert spec.local_participant == _CLIENT_ADDRESS
    assert spec.terms["allowedOperations"] == ("play",)
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
    context = services.use(_descriptor(), operations={"play"})

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
            operations={"play"},
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
            operations={"play"},
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
            operations={"play"},
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
            operations={"play"},
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
        terms=service_use_terms(
            descriptor,
            client_endpoint=_CLIENT_ADDRESS,
            operations={"play"},
        ),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        await lease.refresh()

    assert exc_info.value.code == expected_code
    assert exc_info.value.diagnostics["status"] == expected_status
    assert exc_info.value.diagnostics["reason"] == message


@pytest.mark.asyncio
async def test_command_request_timeout_is_separate_from_service_use() -> None:
    agreement = SimpleNamespace(
        contract=_contract(),
        refresh=AsyncMock(return_value=ContractValidity(ContractValidityStatus.VALID)),
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )

    async def request(**kwargs):
        return service_command_reply_message(
            sender=service_address("demo-home"),
            sender_session_id="service-session",
            recipient=_CLIENT_ADDRESS,
            recipient_session_id="client-session",
            subject=kwargs["subject"],
            in_reply_to="request-message",
            contract=kwargs["contract"],
            body=ServiceCommandReplyBody(
                serviceNamespace="dev.deckr.demo.service",
                operation=kwargs["body"]["operation"],
                status=ServiceCommandStatus.OK,
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
        operations={"play"},
        timeout_seconds=30.0,
    ) as lease:
        reply = await services.command(
            lease,
            "play",
            {"zone": "Kitchen"},
            timeout_seconds=12.0,
        )

    assert reply.status == ServiceCommandStatus.OK
    assert endpoint.request.await_args.kwargs["timeout"] == 12.0
    assert endpoint.request.await_args.kwargs["contract"] == {
        "contractId": "contract-1",
        "generation": 1,
    }


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
    lease = SimpleNamespace(
        refresh=AsyncMock(
            side_effect=ServiceUnavailable(
                "contract_session_mismatch",
                "session mismatch",
            )
        )
    )

    async def changes():
        yield SimpleNamespace(entry=SimpleNamespace(value=changed_payload))

    @asynccontextmanager
    async def watch(_lease, _view):
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
    lease.refresh.assert_awaited_once()


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
            "service_command_failed",
            "failed",
            {"reason": "contract_not_managed"},
        )
    )
    assert not service_unavailable_ends_service_use(
        ServiceUnavailable("service_backend_unavailable", "backend down")
    )


def test_command_reply_helper_matches_service_unavailable_helper() -> None:
    unavailable = ServiceUnavailable("contract_missing_token", "missing token")
    reply = ServiceCommandReplyBody(
        serviceNamespace="dev.deckr.demo.service",
        operation="play",
        status=ServiceCommandStatus.UNAVAILABLE,
        error=ServiceError(
            code="service_use_contract_invalid",
            message="missing token",
            diagnostics={"status": "missing_token"},
        ),
    )
    ordinary = ServiceCommandReplyBody(
        serviceNamespace="dev.deckr.demo.service",
        operation="play",
        status=ServiceCommandStatus.UNAVAILABLE,
        error=ServiceError(
            code="service_backend_unavailable",
            message="backend down",
        ),
    )

    assert service_unavailable_ends_service_use(unavailable)
    assert service_command_reply_ends_service_use(reply)
    assert not service_command_reply_ends_service_use(ordinary)
