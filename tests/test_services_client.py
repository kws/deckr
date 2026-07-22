from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from deckr._concord._models import ParticipantHandle
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    ConcordAgreementState,
    ConcordConflict,
    ConcordConflictCode,
    ConcordLifecyclePhase,
    ConcordManagedContract,
    ConcordUnavailable,
    ConcordUnavailableCode,
    ContractHandle,
    ContractRecord,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
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
    ServiceViewFamily,
    ServiceViewFamilyDefinition,
    ServiceViewRef,
    ServiceViewWriter,
    service_message_ends_service_use,
    service_unavailable_ends_service_use,
)
from deckr.services.messages import service_message, service_response_message
from deckr.services.runtime import ServiceUseLease
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


def _descriptor(
    service_id: str = "demo-home",
    *,
    session_id: str = "service-session",
) -> ServiceDescriptor:
    return ServiceDescriptor(
        candidate=None,
        service_id=service_id,
        namespace="dev.deckr.demo.service",
        endpoint=service_address(service_id),
        session_id=session_id,
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
                keyPrefix=f"zones/{service_id}/",
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
    local_authority: bool = True,
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
        local_authority=local_authority,
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


def _contract() -> ContractHandle:
    participants = tuple(sorted((_CLIENT_ADDRESS, service_address("demo-home")), key=str))
    return ContractHandle(
        key="contract-1:1",
        contract_id="contract-1",
        generation=1,
        participants=participants,
        attached_participants=participants,
        revision=1,
        state=ContractState.OPEN,
        profile="dev.deckr.demo.use.v1",
    )


def _agreement(
    validity: ContractValidity | None = None,
    *,
    enter_error: BaseException | None = None,
    wait_forever: bool = False,
    phase_override: ConcordLifecyclePhase | None = None,
) -> SimpleNamespace:
    contract = _contract()
    validity = validity or ContractValidity(
        ContractValidityStatus.VALID,
        contract=ContractRecord(
            contract_id=contract.contract_id,
            generation=contract.generation,
            participants=contract.participants,
            attached_participants=contract.attached_participants,
            state=contract.state,
            profile=contract.profile,
        ),
    )
    phase = phase_override or (
        ConcordLifecyclePhase.UNAVAILABLE
        if validity.status == ContractValidityStatus.UNAVAILABLE
        else ConcordLifecyclePhase.READY
        if validity.status
        in {
            ContractValidityStatus.VALID,
            ContractValidityStatus.NOT_YET_FULFILLED,
        }
        else ConcordLifecyclePhase.TERMINAL
    )
    state = ConcordAgreementState(
        version=1,
        phase=phase,
        current=phase != ConcordLifecyclePhase.UNAVAILABLE,
        contract=contract,
        validity=validity,
        local_authority=phase == ConcordLifecyclePhase.READY,
        reason=validity.reason,
    )
    agreement = SimpleNamespace(
        contract=contract,
        state=state,
        cancel=AsyncMock(return_value=True),
        aclose=AsyncMock(),
    )
    agreement.__aenter__ = AsyncMock(
        side_effect=enter_error,
        return_value=agreement,
    )

    async def wait_for_state(predicate):
        if wait_forever or not predicate(state):
            await anyio.sleep_forever()
        return state

    agreement.wait_for_state = AsyncMock(side_effect=wait_for_state)
    return agreement


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


class _RaceAgreement:
    def __init__(self, descriptor: ServiceDescriptor, contract_id: str) -> None:
        participants = tuple(
            sorted((_CLIENT_ADDRESS, descriptor.endpoint), key=str)
        )
        self.contract = ContractHandle(
            key=f"contracts.{contract_id}.1",
            contract_id=contract_id,
            generation=1,
            participants=participants,
            attached_participants=participants,
            revision=1,
            state=ContractState.OPEN,
            profile=descriptor.use_profile,
        )
        record = ContractRecord(
            contractId=contract_id,
            generation=1,
            participants=participants,
            attachedParticipants=participants,
            state=ContractState.OPEN,
            profile=descriptor.use_profile,
            participantSessions={
                str(_CLIENT_ADDRESS): "client-session",
                str(descriptor.endpoint): descriptor.session_id,
            },
        )
        self.state = ConcordAgreementState(
            version=1,
            phase=ConcordLifecyclePhase.READY,
            current=True,
            contract=self.contract,
            validity=ContractValidity(
                ContractValidityStatus.VALID,
                contract=record,
            ),
            local_authority=True,
        )
        self._changed = anyio.Event()
        self.cancel_reasons: list[str | None] = []
        self.closed = False

    async def wait_for_state(self, predicate):
        if predicate(self.state):
            return self.state
        await self._changed.wait()
        return self.state

    def lose(self) -> None:
        self.state = ConcordAgreementState(
            version=self.state.version + 1,
            phase=ConcordLifecyclePhase.TERMINAL,
            current=True,
            contract=self.contract,
            validity=ContractValidity(
                ContractValidityStatus.CANCELLED,
                reason="lost",
            ),
            local_authority=False,
            reason="lost",
        )
        self._changed.set()

    async def cancel(self, reason: str | None = None) -> bool:
        self.cancel_reasons.append(reason)
        self.lose()
        return True

    async def aclose(self) -> None:
        self.closed = True
        self.lose()


def _race_lease(
    descriptor: ServiceDescriptor,
    contract_id: str,
) -> ServiceUseLease:
    return ServiceUseLease(
        agreement=_RaceAgreement(descriptor, contract_id),
        descriptor=descriptor,
        consumer_endpoint=_CLIENT_ADDRESS,
        consumer_session_id="client-session",
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
        await services._read_view(  # noqa: SLF001 - exact failure injection
            _lease(),
            ServiceViewRef("demo_views", "zones/demo-home/Kitchen"),
        )

    assert exc_info.value.code == "service_view_unavailable"


@pytest.mark.asyncio
async def test_use_matching_resolves_descriptor_and_negotiates() -> None:
    protocol = _protocol()
    agreement = _agreement()
    concord = SimpleNamespace(propose=Mock(return_value=agreement))
    async def watch_records():
        yield (_descriptor(),)
        await anyio.sleep_forever()

    directory = SimpleNamespace(watch_records=watch_records)
    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
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

        endpoint, spec = concord.propose.call_args.args
        assert endpoint.address == _CLIENT_ADDRESS
        assert spec.terms is None
        agreement.cancel.assert_awaited_once_with("service_use_closed")
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_use_matching_races_rejection_then_acceptance() -> None:
    protocol = _protocol()
    rejected = _descriptor("rejected")
    accepted = _descriptor("accepted")

    async def watch_records():
        yield (rejected, accepted)
        await anyio.sleep_forever()

    services = _services(
        endpoint=SimpleNamespace(
            address=_CLIENT_ADDRESS,
            session_id="client-session",
        ),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
        watch_records=watch_records
    )

    async def propose(descriptor, *, timeout_seconds):
        del timeout_seconds
        if descriptor is rejected:
            raise ServiceUnavailable("contract_rejected", "rejected")
        return _race_lease(descriptor, "accepted-contract")

    services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
    winner = await services._race_service_candidates(  # noqa: SLF001
        protocol,
        predicate=None,
        diagnostics=None,
    )

    assert winner.descriptor is accepted
    await services._close_active_service_use_lease(  # noqa: SLF001
        winner,
        reason="test_complete",
    )


@pytest.mark.asyncio
async def test_use_matching_admits_candidate_arriving_while_pending() -> None:
    protocol = _protocol()
    unavailable = _descriptor("unavailable")
    later = _descriptor("later", session_id="later-session")
    later_arrived = anyio.Event()

    async def watch_records():
        yield (unavailable,)
        await later_arrived.wait()
        yield (unavailable, later)
        await anyio.sleep_forever()

    services = _services(
        endpoint=SimpleNamespace(
            address=_CLIENT_ADDRESS,
            session_id="client-session",
        ),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
        watch_records=watch_records
    )

    async def propose(descriptor, *, timeout_seconds):
        del timeout_seconds
        if descriptor is unavailable:
            later_arrived.set()
            raise ServiceUnavailable("contract_unavailable", "offline")
        return _race_lease(descriptor, "later-contract")

    services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
    winner = await services._race_service_candidates(  # noqa: SLF001
        protocol,
        predicate=None,
        diagnostics=None,
    )

    assert winner.descriptor is later
    await services._close_active_service_use_lease(  # noqa: SLF001
        winner,
        reason="test_complete",
    )


@pytest.mark.asyncio
async def test_beacon_withdrawal_after_acceptance_does_not_end_authority() -> None:
    protocol = _protocol()
    descriptor = _descriptor("withdrawn-after-acceptance")
    withdraw = anyio.Event()
    withdrawal_observed = anyio.Event()

    async def watch_records():
        yield (descriptor,)
        await withdraw.wait()
        withdrawal_observed.set()
        yield ()
        await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
            concord=SimpleNamespace(),
            task_group=task_group,
        )
        services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
            watch_records=watch_records
        )
        services._propose_service_use = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
            return_value=_race_lease(descriptor, "accepted-before-withdrawal")
        )

        async with services.use_matching(protocol, timeout_seconds=1) as service:
            accepted = service.state
            withdraw.set()
            # The discovery task is deliberately gone once Concord wins; a
            # later Beacon withdrawal cannot become lifecycle authority.
            with anyio.move_on_after(0.05):
                await withdrawal_observed.wait()
            assert service.state == accepted
            assert service.state.phase.value == "ready"
            assert service.state.local_authority
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_use_matching_retries_only_transient_candidate_failure() -> None:
    protocol = _protocol()
    descriptor = _descriptor("transient")
    attempts = 0

    async def watch_records():
        yield (descriptor,)
        await anyio.sleep_forever()

    services = _services(
        endpoint=SimpleNamespace(
            address=_CLIENT_ADDRESS,
            session_id="client-session",
        ),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
        watch_records=watch_records
    )

    async def propose(candidate, *, timeout_seconds):
        nonlocal attempts
        del timeout_seconds
        assert candidate is descriptor
        attempts += 1
        if attempts == 1:
            raise ServiceUnavailable("contract_unavailable", "broker outage")
        return _race_lease(candidate, "fresh-after-outage")

    services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
    winner = await services._race_service_candidates(  # noqa: SLF001
        protocol,
        predicate=None,
        diagnostics=None,
    )

    assert attempts == 2
    assert winner.contract.contract_id == "fresh-after-outage"
    await services._close_active_service_use_lease(  # noqa: SLF001
        winner,
        reason="test_complete",
    )


@pytest.mark.asyncio
async def test_use_matching_waits_for_losing_attempt_cleanup_before_ready() -> None:
    protocol = _protocol()
    winner_descriptor = _descriptor("winner")
    loser_descriptor = _descriptor("loser")
    loser_started = anyio.Event()
    loser_cleanup_started = anyio.Event()
    release_loser_cleanup = anyio.Event()

    async def watch_records():
        yield (loser_descriptor, winner_descriptor)
        await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
            concord=SimpleNamespace(),
            task_group=task_group,
        )
        services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
            watch_records=watch_records
        )

        async def propose(descriptor, *, timeout_seconds):
            del timeout_seconds
            if descriptor is loser_descriptor:
                loser_started.set()
                try:
                    await anyio.sleep_forever()
                finally:
                    with anyio.CancelScope(shield=True):
                        loser_cleanup_started.set()
                        await release_loser_cleanup.wait()
                raise AssertionError("unreachable")
            await loser_started.wait()
            return _race_lease(descriptor, "winner-contract")

        services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
        entered = anyio.Event()

        async def use_service() -> None:
            async with services.use_matching(protocol, timeout_seconds=2) as service:
                assert service.descriptor is winner_descriptor
                entered.set()

        task_group.start_soon(use_service)
        with anyio.fail_after(1):
            await loser_cleanup_started.wait()
        assert not entered.is_set()
        release_loser_cleanup.set()
        with anyio.fail_after(1):
            await entered.wait()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_use_matching_reconnects_with_fresh_contract_after_winner_loss() -> None:
    protocol = _protocol()
    descriptor = _descriptor("recovering")
    proposals: list[ServiceUseLease] = []
    allow_successor = anyio.Event()

    async def watch_records():
        yield (descriptor,)
        await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
            concord=SimpleNamespace(),
            task_group=task_group,
        )
        services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
            watch_records=watch_records
        )

        async def propose(candidate, *, timeout_seconds):
            del timeout_seconds
            lease = _race_lease(candidate, f"contract-{len(proposals) + 1}")
            proposals.append(lease)
            if len(proposals) > 1:
                await allow_successor.wait()
            return lease

        services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
        async with services.use_matching(protocol, timeout_seconds=1) as service:
            assert service.state.phase.value == "ready"
            first = proposals[0]
            first.agreement.lose()
            reconnecting = await service.wait_for_state(
                lambda state: state.phase.value == "reconnecting",
                timeout_seconds=1,
            )
            assert not reconnecting.local_authority
            with anyio.fail_after(1):
                while len(proposals) < 2:
                    await anyio.sleep(0)
            assert first.agreement.closed
            allow_successor.set()
            ready = await service.wait_for_state(
                lambda state: state.phase.value == "ready" and state.version > 1,
                timeout_seconds=1,
            )
            assert ready.local_authority
            assert proposals[0].contract.contract_id != proposals[1].contract.contract_id
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_unpublished_request_waits_for_successor_without_using_predecessor() -> (
    None
):
    protocol = _protocol()
    descriptor = _descriptor("request-recovery")
    proposals: list[ServiceUseLease] = []

    async def watch_records():
        yield (descriptor,)
        await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
            concord=SimpleNamespace(),
            task_group=task_group,
        )
        services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
            watch_records=watch_records
        )

        async def propose(candidate, *, timeout_seconds):
            del timeout_seconds
            lease = _race_lease(candidate, f"request-contract-{len(proposals) + 1}")
            proposals.append(lease)
            return lease

        request = AsyncMock(
            return_value=ServiceMessageBody(
                serviceNamespace=descriptor.namespace,
                name="play",
                intent=ServiceMessageIntent.COMMAND,
                exchangePattern=ServiceExchangePattern.REQUEST_REPLY,
                status=ServiceMessageStatus.OK,
            )
        )
        services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
        services._request = request  # type: ignore[method-assign]  # noqa: SLF001

        async with services.use_matching(protocol, timeout_seconds=1) as service:
            predecessor = proposals[0]
            predecessor.agreement.lose()
            response = await service.request("play", timeout_seconds=1)

            assert response.status == ServiceMessageStatus.OK
            assert len(proposals) >= 2
            used_lease = request.await_args.args[0]
            assert used_lease is proposals[1]
            assert used_lease.contract.contract_id != predecessor.contract.contract_id
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_published_request_failure_is_never_replayed_on_successor() -> None:
    protocol = _protocol()
    descriptor = _descriptor("ambiguous-request")
    proposals: list[ServiceUseLease] = []

    async def watch_records():
        yield (descriptor,)
        await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        services = _services(
            endpoint=SimpleNamespace(
                address=_CLIENT_ADDRESS,
                session_id="client-session",
            ),
            concord=SimpleNamespace(),
            task_group=task_group,
        )
        services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
            watch_records=watch_records
        )

        async def propose(candidate, *, timeout_seconds):
            del timeout_seconds
            lease = _race_lease(candidate, f"ambiguous-contract-{len(proposals) + 1}")
            proposals.append(lease)
            return lease

        published = AsyncMock(
            side_effect=ServiceUnavailable(
                "request_outcome_ambiguous",
                "The request may already have been published",
            )
        )
        services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
        services._request = published  # type: ignore[method-assign]  # noqa: SLF001

        async with services.use_matching(protocol, timeout_seconds=1) as service:
            with pytest.raises(ServiceUnavailable) as exc_info:
                await service.request("play", timeout_seconds=1)

            assert exc_info.value.code == "request_outcome_ambiguous"
            published.assert_awaited_once()
            assert len(proposals) == 1
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_use_matching_rejects_sixty_fifth_candidate() -> None:
    protocol = _protocol()
    descriptors = tuple(_descriptor(f"service-{index}") for index in range(65))

    async def watch_records():
        yield descriptors
        await anyio.sleep_forever()

    services = _services(
        endpoint=SimpleNamespace(
            address=_CLIENT_ADDRESS,
            session_id="client-session",
        ),
        concord=SimpleNamespace(),
    )
    services._directories[_directory_key(protocol)] = SimpleNamespace(  # noqa: SLF001
        watch_records=watch_records
    )

    async def propose(_descriptor, *, timeout_seconds):
        del timeout_seconds
        await anyio.sleep_forever()

    services._propose_service_use = propose  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(ServiceUnavailable) as exc_info:
        await services._race_service_candidates(  # noqa: SLF001
            protocol,
            predicate=None,
            diagnostics=None,
        )

    assert exc_info.value.code == "service_candidate_limit_exceeded"


@pytest.mark.asyncio
async def test_aclose_closes_active_service_use_lease_once() -> None:
    agreement = _agreement()
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )
    context = services.use(_descriptor())

    service = await context.__aenter__()
    assert service.descriptor.service_id == "demo-home"

    await services.aclose()

    agreement.cancel.assert_awaited_once_with("service_use_closed")
    agreement.aclose.assert_awaited_once()

    await context.__aexit__(None, None, None)

    agreement.cancel.assert_awaited_once()
    agreement.aclose.assert_awaited_once()

    with pytest.raises(ServiceUnavailable) as exc_info:
        await service.request("play")
    assert exc_info.value.code == "service_use_closed"


@pytest.mark.asyncio
async def test_use_explicit_timeout_cancels_pending_contract() -> None:
    agreement = _agreement(
        ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED),
        wait_forever=True,
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
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
    agreement = _agreement(wait_forever=True)
    concord = SimpleNamespace(propose=Mock(return_value=agreement))
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
    assert exc_info.value.diagnostics["contractId"] == "contract-1"
    concord.propose.assert_called_once()
    agreement.cancel.assert_awaited_once_with("contract_timeout")
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_timeout_cleans_failed_enter_before_contract_creation() -> None:
    agreement = _agreement()
    agreement.state = ConcordAgreementState(
        version=0,
        phase=ConcordLifecyclePhase.STARTING,
        current=False,
        contract=None,
        validity=None,
        local_authority=False,
    )
    del agreement.contract

    async def block_enter():
        await anyio.sleep_forever()

    agreement.__aenter__.side_effect = block_enter
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(_descriptor(), timeout_seconds=0.01):
            pass

    assert exc_info.value.code == "contract_timeout"
    assert exc_info.value.diagnostics["contractId"] is None
    agreement.cancel.assert_not_awaited()
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_translates_concord_unavailable_during_entry() -> None:
    agreement = _agreement(
        enter_error=ConcordUnavailable(
            ConcordUnavailableCode.SOURCE_STALE,
            "Concord view is unavailable",
        )
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(_descriptor(), timeout_seconds=1.0):
            pass

    assert exc_info.value.code == "contract_unavailable"
    assert exc_info.value.diagnostics["reason"] == "Concord view is unavailable"


@pytest.mark.asyncio
async def test_use_fails_closed_when_attach_unavailable_has_cached_pending_validity() -> (
    None
):
    agreement = _agreement(
        ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED),
        phase_override=ConcordLifecyclePhase.UNAVAILABLE,
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(_descriptor(), timeout_seconds=1.0):
            pass

    assert exc_info.value.code == "contract_unavailable"
    agreement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_use_translates_agreement_closure_during_setup() -> None:
    agreement = _agreement()
    agreement.wait_for_state.side_effect = anyio.ClosedResourceError()
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )

    with pytest.raises(ServiceUnavailable) as exc_info:
        async with services.use(_descriptor(), timeout_seconds=1.0):
            pass

    assert exc_info.value.code == "service_use_closed"
    assert service_unavailable_ends_service_use(exc_info.value)
    agreement.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("conflict_code", "message", "expected_code"),
    (
        (
            ConcordConflictCode.CONTRACT_CANCELLED,
            "misleading diagnostic: token is valid",
            "contract_cancelled",
        ),
        (
            ConcordConflictCode.CONTRACT_MISSING,
            "misleading diagnostic: contract is cancelled",
            "contract_missing_contract",
        ),
        (
            ConcordConflictCode.TOKEN_INVALID,
            "misleading diagnostic: contract is missing",
            "contract_invalid_token",
        ),
        (
            ConcordConflictCode.TOKEN_IDENTITY_MISMATCH,
            "misleading diagnostic: participant is attached",
            "contract_invalid_token",
        ),
    ),
)
@pytest.mark.asyncio
async def test_use_translates_terminal_protocol_conflict_during_negotiation(
    conflict_code: ConcordConflictCode,
    message: str,
    expected_code: str,
) -> None:
    agreement = _agreement(
        enter_error=ConcordConflict(conflict_code, message),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
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
    agreement = _agreement(
        enter_error=ConcordConflict(
            ConcordConflictCode.PARTICIPANT_ALREADY_ATTACHED,
            message,
        ),
    )
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
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
    ("conflict_code", "message", "expected_code", "expected_status"),
    (
        (
            ConcordConflictCode.TOKEN_MISSING,
            "Concord participant token is missing",
            "contract_missing_token",
            ContractValidityStatus.MISSING_TOKEN.value,
        ),
    ),
)
@pytest.mark.asyncio
async def test_service_use_lease_refresh_preserves_terminal_protocol_conflicts(
    conflict_code: ConcordConflictCode,
    message: str,
    expected_code: str,
    expected_status: str,
) -> None:
    del conflict_code
    descriptor = _descriptor()
    agreement = _agreement(
        ContractValidity(
            ContractValidityStatus.MISSING_TOKEN,
            reason=message,
        )
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
async def test_service_use_lease_refresh_fails_closed_when_unavailable() -> None:
    descriptor = _descriptor()
    agreement = _agreement()
    lease = ServiceUseLease(
        agreement=agreement,
        descriptor=descriptor,
        consumer_endpoint=_CLIENT_ADDRESS,
        consumer_session_id="client-session",
    )

    agreement.state = ConcordAgreementState(
        version=2,
        phase=ConcordLifecyclePhase.UNAVAILABLE,
        current=False,
        contract=None,
        validity=ContractValidity(ContractValidityStatus.UNAVAILABLE),
        local_authority=False,
    )
    with pytest.raises(ServiceUnavailable) as exc_info:
        await lease.refresh()

    assert exc_info.value.code == "contract_unavailable"
    assert service_unavailable_ends_service_use(exc_info.value)


@pytest.mark.asyncio
async def test_service_use_lease_refresh_raises_unavailable_without_valid_lease() -> None:
    descriptor = _descriptor()
    agreement = _agreement(ContractValidity(ContractValidityStatus.UNAVAILABLE))
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
    agreement = _agreement()

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
        concord=SimpleNamespace(propose=Mock(return_value=agreement)),
    )

    async with services.use(
        _descriptor(),
        timeout_seconds=30.0,
    ) as service:
        reply = await service.request(
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
    assert participant.validate_calls == []


@pytest.mark.asyncio
async def test_managed_service_contract_rejects_lost_local_authority() -> None:
    managed = _managed_service_contract(local_authority=False)
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
        await channel.send("stateChanged", event={"state": "ready"})

    assert exc_info.value.code == "invalid_service_contract"
    endpoint.send.assert_not_awaited()


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
    assert participant.validate_calls == []


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
async def test_managed_service_contract_does_not_repair_missing_state_before_send() -> None:
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

    with pytest.raises(ServiceUnavailable) as exc_info:
        await channel.send("stateChanged")
    assert exc_info.value.code == "contract_not_managed"
    assert participant.reconcile_calls == 0
    assert participant.validate_calls == []
    endpoint.send.assert_not_awaited()


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
    assert participant.reconcile_calls == 0
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

    assert participant.validate_calls == []
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

    body = await services._authorize_inbound_message(lease, message)  # noqa: SLF001

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
        await services._authorize_inbound_message(lease, wrong_subject)  # noqa: SLF001
    assert exc_info.value.code == "invalid_service_response"

    wrong_session = message.model_copy(update={"sender_session_id": "old-session"})
    with pytest.raises(ServiceUnavailable) as exc_info:
        await services._authorize_inbound_message(lease, wrong_session)  # noqa: SLF001
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
        await services._authorize_inbound_message(  # noqa: SLF001
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
        yield SimpleNamespace(entry=SimpleNamespace(value=current_payload))
        yield SimpleNamespace(entry=SimpleNamespace(value=changed_payload))

    @asynccontextmanager
    async def watch(_context, _view):
        yield changes()

    store = SimpleNamespace(watch=watch)
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._view_stores["demo_views"] = store  # noqa: SLF001

    stream = services._watch_view(lease, view)  # noqa: SLF001
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

    observed_watch: list[tuple[object, ServiceViewRef]] = []

    async def changes():
        yield SimpleNamespace(entry=SimpleNamespace(value=current_payload))
        await anyio.sleep_forever()
        yield None

    @asynccontextmanager
    async def watch(context, watched_view):
        observed_watch.append((context, watched_view))
        yield changes()

    store = SimpleNamespace(watch=watch)
    services = _services(
        endpoint=SimpleNamespace(address=_CLIENT_ADDRESS, session_id="client-session"),
        concord=SimpleNamespace(),
    )
    services._view_stores["demo_views"] = store  # noqa: SLF001
    lease = _lease()

    stream = services._watch_view(lease, view)  # noqa: SLF001
    assert await anext(stream) == current_payload
    with anyio.move_on_after(0.05) as scope:
        await anext(stream)
    await stream.aclose()

    assert scope.cancel_called
    read_context, watched_view = observed_watch[0]
    assert read_context.reader is ServiceViewWriter.CONSUMER
    assert watched_view == view
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

    access = services._view_access(lease, "demo_views")  # noqa: SLF001

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
    assert service_unavailable_ends_service_use(
        ServiceUnavailable("contract_unavailable", "contract view unavailable")
    )
    assert service_unavailable_ends_service_use(
        ServiceUnavailable("service_use_closed", "managed capability closed")
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
