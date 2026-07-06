from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.actions.endpoints import action_provider_address
from deckr.concord import ConcordConflict, ContractValidity, ContractValidityStatus
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import endpoint_address, service_address
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


class _TaskGroup:
    def __init__(self) -> None:
        self.started = []

    def start_soon(self, func, *args, name=None) -> None:
        del name
        self.started.append((func, args))


class _Endpoint:
    address = action_provider_address("python-dev.deckr.demo")
    session_id = "client-session"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request(self, **kwargs):
        self.requests.append(kwargs)
        return service_command_reply_message(
            sender=service_address("demo-home"),
            sender_session_id="service-session",
            recipient=endpoint_address("action_provider", "python-dev.deckr.demo"),
            recipient_session_id=self.session_id,
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


class _Agreement:
    def __init__(self, *, statuses=None, wait: anyio.Event | None = None) -> None:
        self.contract = SimpleNamespace(
            contract_id="contract-1",
            generation=1,
            profile="dev.deckr.demo.use.v1",
        )
        self._statuses = list(statuses or [ContractValidityStatus.VALID])
        self._statuses_last = self._statuses[-1]
        self._wait = wait
        self.cancelled: list[str | None] = []
        self.closed = False

    async def refresh(self):
        if self._wait is not None and not self._wait.is_set():
            await self._wait.wait()
        status = self._statuses.pop(0) if self._statuses else self._statuses_last
        self._statuses_last = status
        return ContractValidity(status)

    async def cancel(self, reason: str | None = None) -> bool:
        self.cancelled.append(reason)
        return True

    async def aclose(self) -> None:
        self.closed = True


class _ConcordConflictAgreement(_Agreement):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    async def refresh(self):
        raise ConcordConflict(self.message)


class _Concord:
    def __init__(
        self,
        agreement: _Agreement,
        *,
        propose_wait: anyio.Event | None = None,
    ) -> None:
        self.agreement = agreement
        self._propose_wait = propose_wait
        self.proposals = []

    async def propose(self, spec, *, start_soon=None):
        self.proposals.append((spec, start_soon))
        if self._propose_wait is not None and not self._propose_wait.is_set():
            await self._propose_wait.wait()
        return self.agreement


class _UnavailableDirectory:
    def is_current(self) -> bool:
        return False

    def resolve(self, *args, **kwargs):
        raise KvUnavailable("directory unavailable")

    async def wait_for(self, *args, **kwargs):
        raise KvUnavailable("directory unavailable")


class _ReadyDirectory:
    def __init__(self, descriptor: ServiceDescriptor) -> None:
        self.descriptor = descriptor
        self.wait_calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    def is_current(self) -> bool:
        return True

    def resolve(self, predicate=None, *, select=None):
        del select
        if predicate is not None and not predicate(self.descriptor):
            return None
        return self.descriptor

    async def wait_for(self, predicate=None, *, select=None, timeout=None):
        self.wait_calls.append({"select": select, "timeout": timeout})
        if predicate is not None and not predicate(self.descriptor):
            raise TimeoutError
        return self.descriptor


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


def _view_storage_key(
    view: ServiceViewRef,
    *,
    contract_id: str = "contract-1",
    generation: int = 1,
) -> str:
    return f"{view.key}.contract.{encode_key_token(contract_id)}.{generation}"


def _view_payload(
    view: ServiceViewRef,
    payload: dict[str, Any],
    *,
    contract_id: str = "contract-1",
    generation: int = 1,
) -> dict[str, Any]:
    return {
        **payload,
        "viewKey": view.key,
        "serviceId": "demo-home",
        "serviceNamespace": "dev.deckr.demo.service",
        "sessionId": "service-session",
        "contractId": contract_id,
        "generation": generation,
    }


def _services(
    *,
    agreement: _Agreement | None = None,
    concord: _Concord | None = None,
    task_group: Any | None = None,
    buckets: dict[str, MemoryJsonKvBucket] | None = None,
) -> DeckrServices:
    bucket_map = buckets if buckets is not None else {}

    def kv_bucket_for(policy):
        bucket = bucket_map.get(policy.bucket)
        if bucket is None:
            bucket = MemoryJsonKvBucket(bucket=policy.bucket)
            bucket_map[policy.bucket] = bucket
        return bucket

    return DeckrServices(
        endpoint=_Endpoint(),
        beacon=SimpleNamespace(),
        concord=concord or _Concord(agreement or _Agreement()),
        task_group=task_group or _TaskGroup(),
        kv_bucket_for=kv_bucket_for,
    )


def test_directory_reuses_managed_view() -> None:
    task_group = _TaskGroup()
    services = _services(task_group=task_group)

    first = services.directory(_protocol())
    second = services.directory(_protocol())

    assert first is second
    assert len(task_group.started) == 1
    assert not hasattr(services, "beacon")
    assert not hasattr(services, "concord")


def test_resolve_descriptor_translates_directory_unavailable() -> None:
    services = _services()
    services._directories[_directory_key(_protocol())] = _UnavailableDirectory()  # noqa: SLF001

    with pytest.raises(ServiceUnavailable) as exc_info:
        services.resolve_descriptor(_protocol())

    assert exc_info.value.code == "service_discovery_pending"


@pytest.mark.asyncio
async def test_wait_for_descriptor_translates_directory_unavailable() -> None:
    services = _services()
    services._directories[_directory_key(_protocol())] = _UnavailableDirectory()  # noqa: SLF001

    with pytest.raises(ServiceUnavailable) as exc_info:
        await services.wait_for_descriptor(_protocol())

    assert exc_info.value.code == "beacon_unavailable"


@pytest.mark.asyncio
async def test_read_view_translates_kv_unavailable() -> None:
    def kv_bucket_for(_policy):
        raise KvUnavailable("view store unavailable")

    services = DeckrServices(
        endpoint=_Endpoint(),
        beacon=SimpleNamespace(),
        concord=_Concord(_Agreement()),
        task_group=_TaskGroup(),
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
    services = _services()
    protocol = _protocol()
    services._directories[_directory_key(protocol)] = _ReadyDirectory(_descriptor())  # noqa: SLF001

    async with services.use_matching(
        protocol,
        operations={"play"},
        predicate=lambda descriptor: descriptor.service_id == "demo-home",
        timeout_seconds=10.0,
    ) as lease:
        assert lease.descriptor.service_id == "demo-home"
        assert lease.terms.allowed_operations == ("play",)


@pytest.mark.asyncio
async def test_use_without_timeout_waits_until_contract_valid() -> None:
    ready = anyio.Event()
    agreement = _Agreement(wait=ready)
    async with anyio.create_task_group() as tg:
        services = _services(agreement=agreement, task_group=tg)

        async def release() -> None:
            await anyio.sleep(0.05)
            ready.set()

        tg.start_soon(release)
        async with services.use(_descriptor(), operations={"play"}) as lease:
            assert lease.descriptor.service_id == "demo-home"
            assert lease.terms.allowed_operations == ("play",)

        tg.cancel_scope.cancel()

    assert agreement.cancelled == ["service_use_closed"]
    assert agreement.closed


@pytest.mark.asyncio
async def test_use_explicit_timeout_cancels_pending_contract() -> None:
    agreement = _Agreement(statuses=[ContractValidityStatus.NOT_YET_FULFILLED])
    services = _services(agreement=agreement)

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            operations={"play"},
            timeout_seconds=0.01,
        ):
            pass

    assert exc_info.value.code == "contract_timeout"
    assert agreement.cancelled == ["contract_timeout"]
    assert agreement.closed


@pytest.mark.asyncio
async def test_use_explicit_timeout_covers_proposal_wait() -> None:
    wait = anyio.Event()
    concord = _Concord(_Agreement(), propose_wait=wait)
    services = _services(concord=concord)

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            operations={"play"},
            timeout_seconds=0.01,
        ):
            pass

    assert exc_info.value.code == "contract_timeout"
    assert exc_info.value.diagnostics["contractId"] is None
    assert len(concord.proposals) == 1
    assert not concord.agreement.cancelled
    assert not concord.agreement.closed


@pytest.mark.parametrize(
    ("message", "expected_code"),
    (
        ("Concord contract is cancelled", "contract_cancelled"),
        ("Concord contract 'abc' is cancelled", "contract_cancelled"),
        ("Concord contract is missing", "contract_missing_contract"),
        ("Concord contract 'abc' is missing", "contract_missing_contract"),
        ("Concord participant token is missing", "contract_missing_token"),
        ("Concord participant token is invalid", "contract_invalid_token"),
        ("Concord participant token changed owner", "contract_invalid_token"),
    ),
)
@pytest.mark.asyncio
async def test_use_translates_terminal_protocol_conflict_during_negotiation(
    message: str,
    expected_code: str,
) -> None:
    agreement = _ConcordConflictAgreement(message)
    services = _services(agreement=agreement)

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            operations={"play"},
            timeout_seconds=1.0,
        ):
            pass

    assert exc_info.value.code == expected_code
    assert exc_info.value.diagnostics["reason"] == message
    assert agreement.closed


@pytest.mark.asyncio
async def test_use_translates_unknown_protocol_conflict_to_service_use_conflict() -> None:
    agreement = _ConcordConflictAgreement("Concord participant is already attached")
    services = _services(agreement=agreement)

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(
            _descriptor(),
            operations={"play"},
            timeout_seconds=1.0,
        ):
            pass

    assert exc_info.value.code == "service_use_conflict"
    assert exc_info.value.diagnostics["reason"] == (
        "Concord participant is already attached"
    )
    assert agreement.closed


@pytest.mark.parametrize(
    ("message", "expected_code", "expected_status"),
    (
        (
            "Concord contract is cancelled",
            "contract_cancelled",
            ContractValidityStatus.CANCELLED.value,
        ),
        (
            "Concord contract is missing",
            "contract_missing_contract",
            ContractValidityStatus.MISSING_CONTRACT.value,
        ),
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
    lease = ServiceUseLease(
        agreement=_ConcordConflictAgreement(message),
        descriptor=descriptor,
        terms=service_use_terms(
            descriptor,
            client_endpoint=_Endpoint.address,
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
    agreement = _Agreement()
    endpoint = _Endpoint()
    services = _services(agreement=agreement)
    services._endpoint = endpoint  # noqa: SLF001

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
    assert endpoint.requests[0]["timeout"] == 12.0
    assert endpoint.requests[0]["contract"] == {
        "contractId": "contract-1",
        "generation": 1,
    }


@pytest.mark.asyncio
async def test_read_view_uses_authorized_service_view_store() -> None:
    buckets = {"demo_views": MemoryJsonKvBucket(bucket="demo_views")}
    view = ServiceViewRef("demo_views", "zones/demo-home/Kitchen")
    await buckets["demo_views"].put(
        _view_storage_key(view),
        _view_payload(view, {"volume": 12}),
    )
    async with anyio.create_task_group() as tg:
        services = _services(task_group=tg, buckets=buckets)
        async with services.use(_descriptor(), views={"zones"}) as lease:
            payload = await services.read_view(lease, view)
        tg.cancel_scope.cancel()

    assert payload is not None
    assert payload["volume"] == 12


@pytest.mark.asyncio
async def test_watch_view_refreshes_lease_before_delivering_changes() -> None:
    buckets = {"demo_views": MemoryJsonKvBucket(bucket="demo_views")}
    view = ServiceViewRef("demo_views", "zones/demo-home/Kitchen")
    await buckets["demo_views"].put(
        _view_storage_key(view),
        _view_payload(view, {"volume": 12}),
    )
    agreement = _Agreement(
        statuses=[
            ContractValidityStatus.VALID,
            ContractValidityStatus.VALID,
            ContractValidityStatus.SESSION_MISMATCH,
        ]
    )
    async with anyio.create_task_group() as tg:
        services = _services(agreement=agreement, task_group=tg, buckets=buckets)
        async with services.use(_descriptor(), views={"zones"}) as lease:
            changes = services.watch_view(lease, view)
            assert await anext(changes) == {
                "viewKey": view.key,
                "serviceId": "demo-home",
                "serviceNamespace": "dev.deckr.demo.service",
                "sessionId": "service-session",
                "contractId": "contract-1",
                "generation": 1,
                "volume": 12,
            }
            await buckets["demo_views"].put(
                _view_storage_key(view),
                _view_payload(view, {"volume": 13}),
            )
            with pytest.raises(ServiceUnavailable) as exc_info:
                await anext(changes)
            await changes.aclose()
        tg.cancel_scope.cancel()

    assert exc_info.value.code == "contract_session_mismatch"


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
