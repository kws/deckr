"""Managed service-use client for Deckr service consumers."""

from __future__ import annotations

import logging
import random
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Hashable,
    Mapping,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import Any

import anyio

from deckr.beacon import Beacon, BeaconDirectory
from deckr.concord import (
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordLifecyclePhase,
    ConcordManagedContract,
    ConcordParticipant,
    ConcordUnavailable,
    ContractHandle,
    ContractValidityStatus,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import (
    SERVICES_LANE,
    DeckrMessage,
    EndpointTarget,
    entity_subject,
)
from deckr.core.util.anyio import CoalescedStateBroadcaster
from deckr.lanes import EndpointSession
from deckr.services.messages import (
    SERVICE_MESSAGE,
    ServiceExchangePattern,
    ServiceMessageBody,
    ServiceMessageDirection,
    service_body,
)
from deckr.services.runtime import (
    ServiceDescriptor,
    ServiceManagedContractContext,
    ServiceMessageDefinition,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceViewReadContext,
    ServiceViewRef,
    ServiceViewWriteContext,
    ServiceViewWriter,
    parse_service_descriptor,
    service_managed_contract_context,
    service_use_negotiation_terminal_status,
    terminal_concord_conflict_status,
)
from deckr.services.views import (
    ManagedServiceViewAccess,
    ServiceViewEntry,
    ServiceViewStore,
    ServiceViewWatchChange,
    ServiceViewWatchSnapshot,
)
from deckr.substrates.nats_kv import KvBucketPolicy, KvUnavailable, NatsJsonKvBucket

logger = logging.getLogger(__name__)

_DirectoryKey = tuple[str, str, str, str]
_MAX_MATCHING_SERVICE_CANDIDATES = 64


class ManagedServicePhase(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    UNAVAILABLE = "unavailable"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ManagedServiceFailure:
    code: str
    message: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "diagnostics",
            MappingProxyType(dict(self.diagnostics)),
        )


@dataclass(frozen=True, slots=True)
class ManagedServiceState:
    version: int
    phase: ManagedServicePhase
    descriptor: ServiceDescriptor | None
    local_authority: bool
    failure: ManagedServiceFailure | None = None


ManagedServiceStatePredicate = Callable[[ManagedServiceState], bool]


class ManagedService:
    """Opaque capability whose state follows current Concord authority."""

    def __init__(
        self,
        client: DeckrServices,
        lease: ServiceUseLease | None = None,
        *,
        reconnect: bool = False,
    ) -> None:
        self._client = client
        self._lease = lease
        self._reconnect = reconnect
        self._closed = False
        self._close_callback: Callable[[], Awaitable[None]] | None = None
        self._close_lock = anyio.Lock()
        self._close_done = anyio.Event()
        self._close_started = False
        self._state_bus = CoalescedStateBroadcaster[str](current=lease is not None)
        self._state = ManagedServiceState(
            version=0,
            phase=(
                ManagedServicePhase.READY
                if lease is not None
                else ManagedServicePhase.CONNECTING
            ),
            descriptor=lease.descriptor if lease is not None else None,
            local_authority=lease is not None,
        )

    @property
    def state(self) -> ManagedServiceState:
        return self._state

    @property
    def descriptor(self) -> ServiceDescriptor:
        descriptor = self._state.descriptor
        if descriptor is None:
            raise ServiceUnavailable(
                "service_connecting",
                "Managed service has not selected a descriptor",
            )
        return descriptor

    @asynccontextmanager
    async def watch_state(
        self,
    ) -> AsyncIterator[AsyncIterator[ManagedServiceState]]:
        async with self._state_bus.subscribe(
            lambda _version, _current: self._state
        ) as subscription:

            async def stream() -> AsyncIterator[ManagedServiceState]:
                yield subscription.initial
                async for _change in subscription:
                    try:
                        yield await self._state_bus.capture(
                            lambda _version, _current: self._state
                        )
                    except anyio.ClosedResourceError:
                        return

            yield stream()

    async def wait_for_state(
        self,
        predicate: ManagedServiceStatePredicate,
        *,
        timeout_seconds: float | None = None,
    ) -> ManagedServiceState:
        async def wait() -> ManagedServiceState:
            async with self._state_bus.subscribe(
                lambda _version, _current: self._state
            ) as subscription:
                state = subscription.initial
                if predicate(state):
                    return state
                async for _change in subscription:
                    state = await self._state_bus.capture(
                        lambda _version, _current: self._state
                    )
                    if predicate(state):
                        return state
                    if state.phase == ManagedServicePhase.CLOSED:
                        raise anyio.ClosedResourceError
            raise anyio.ClosedResourceError

        if timeout_seconds is None:
            return await wait()
        with anyio.fail_after(timeout_seconds):
            return await wait()

    async def aclose(self) -> None:
        """Close this managed capability and its current authority exactly once."""

        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                if self._close_started:
                    first = False
                else:
                    first = True
                    self._close_started = True
            if not first:
                await self._close_done.wait()
                return
            try:
                callback = self._close_callback
                if callback is None:
                    await self._mark_closed()
                else:
                    await callback()
            finally:
                self._close_done.set()

    async def send(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        event: Mapping[str, Any] | None = None,
    ) -> Any:
        lease = self._require_ready()
        return await self._client._send(lease, name, params, event=event)

    async def request(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody:
        deadline: float | None = None
        lease = self._lease
        while True:
            if lease is None or self._closed:
                if deadline is None:
                    deadline = _deadline(timeout_seconds)
                lease = await self._wait_for_ready_lease(
                    _remaining_timeout(deadline)
                )
            try:
                # This is a local state check and contains no transport await.
                # A failure therefore proves the request has not been
                # published and is the one case that may wait for a successor.
                await lease.refresh()
            except ServiceUnavailable:
                if not self._reconnect:
                    raise
                if deadline is None:
                    deadline = _deadline(timeout_seconds)
                lease = await self._wait_for_replacement_lease(
                    lease,
                    _remaining_timeout(deadline),
                )
                continue
            request_timeout = (
                timeout_seconds
                if deadline is None
                else _remaining_timeout(deadline)
            )
            break
        return await self._client._request(
            lease,
            name,
            params,
            timeout_seconds=request_timeout,
        )

    async def create_view(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        lease = self._require_ready()
        return await self._client._create_view(lease, view, payload, ttl=ttl)

    async def put_view(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        lease = self._require_ready()
        return await self._client._put_view(
            lease,
            view,
            payload,
            revision=revision,
            ttl=ttl,
        )

    async def update_view(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        lease = self._require_ready()
        return await self._client._update_view(
            lease,
            view,
            payload,
            revision=revision,
            ttl=ttl,
        )

    async def delete_view(
        self,
        view: ServiceViewRef,
        *,
        revision: int | None = None,
    ) -> None:
        lease = self._require_ready()
        await self._client._delete_view(lease, view, revision=revision)

    async def read_view(self, view: ServiceViewRef) -> Mapping[str, Any] | None:
        lease = await self._wait_for_ready_lease(None)
        return await self._client._read_view(lease, view)

    async def watch_view(
        self,
        view: ServiceViewRef,
    ) -> AsyncIterator[Mapping[str, Any] | None]:
        lease = await self._wait_for_ready_lease(None)
        async for payload in self._client._watch_view(lease, view):
            if self._lease is not lease:
                raise ServiceUnavailable(
                    "contract_local_authority_lost",
                    "Service view watch lost its original Concord authority",
                )
            yield payload

    async def authorize_inbound_message(
        self,
        message: DeckrMessage,
        *,
        name: str | None = None,
    ) -> ServiceMessageBody:
        lease = self._require_ready()
        return await self._client._authorize_inbound_message(
            lease,
            message,
            name=name,
        )

    def view_access(self, store_name: str) -> ManagedServiceViewAccess:
        lease = self._require_ready()
        return self._client._view_access(lease, store_name)

    async def _refresh(self) -> None:
        await self._require_ready().refresh()

    async def _wait_for_authority_loss(self) -> ServiceUnavailable:
        lease = self._require_ready()
        try:
            await lease.agreement.wait_for_state(lambda state: not state.valid)
        except anyio.ClosedResourceError:
            return ServiceUnavailable(
                "service_use_closed",
                "Managed service capability closed while waiting for authority",
                {"serviceId": self.descriptor.service_id},
            )
        try:
            await lease.refresh()
        except ServiceUnavailable as exc:
            return exc
        return ServiceUnavailable(
            "contract_local_authority_lost",
            "Managed service capability lost local authority",
            {"serviceId": self.descriptor.service_id},
        )

    def _authority_pointer(self) -> ContractPointer:
        return _lease_pointer(self._require_ready())

    async def _set_lease(
        self,
        lease: ServiceUseLease,
    ) -> None:
        async with self._state_bus.lock:
            if self._closed:
                raise anyio.ClosedResourceError
            self._lease = lease
            self._publish_locked(
                ManagedServicePhase.READY,
                descriptor=lease.descriptor,
                local_authority=True,
            )

    async def _set_disconnected(
        self,
        lease: ServiceUseLease,
        failure: ServiceUnavailable,
    ) -> bool:
        async with self._state_bus.lock:
            if self._lease is not lease:
                return False
            self._lease = None
            self._publish_locked(
                ManagedServicePhase.RECONNECTING,
                descriptor=lease.descriptor,
                local_authority=False,
                failure=_managed_failure(failure),
            )
            return True

    async def _set_unavailable(self, failure: ServiceUnavailable) -> None:
        async with self._state_bus.lock:
            self._lease = None
            self._publish_locked(
                ManagedServicePhase.UNAVAILABLE,
                descriptor=self._state.descriptor,
                local_authority=False,
                failure=_managed_failure(failure),
            )

    async def _mark_closed(self) -> None:
        async with self._state_bus.lock:
            if self._state.phase == ManagedServicePhase.CLOSED:
                return
            self._closed = True
            self._lease = None
            self._publish_locked(
                ManagedServicePhase.CLOSING,
                descriptor=self._state.descriptor,
                local_authority=False,
            )
            self._publish_locked(
                ManagedServicePhase.CLOSED,
                descriptor=self._state.descriptor,
                local_authority=False,
            )
        await self._state_bus.aclose()

    def _require_ready(self) -> ServiceUseLease:
        lease = self._lease
        if self._closed or lease is None:
            raise ServiceUnavailable(
                (
                    "service_use_closed"
                    if self._closed
                    else "service_reconnecting"
                ),
                (
                    "Managed service capability is closed"
                    if self._closed
                    else "Managed service has no current Concord authority"
                ),
                (
                    {"serviceId": self._state.descriptor.service_id}
                    if self._state.descriptor is not None
                    else None
                ),
            )
        return lease

    async def _wait_for_ready_lease(
        self,
        timeout_seconds: float | None,
    ) -> ServiceUseLease:
        if self._closed:
            raise ServiceUnavailable(
                "service_use_closed",
                "Managed service capability is closed",
            )
        lease = self._lease
        if lease is not None and not self._closed:
            return lease
        try:
            state = await self.wait_for_state(
                lambda current: current.phase
                in {
                    ManagedServicePhase.READY,
                    ManagedServicePhase.UNAVAILABLE,
                    ManagedServicePhase.CLOSING,
                    ManagedServicePhase.CLOSED,
                },
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as exc:
            raise ServiceUnavailable(
                "service_reconnect_timeout",
                "Timed out waiting for replacement Concord authority",
            ) from exc
        if state.phase != ManagedServicePhase.READY or self._lease is None:
            failure = state.failure
            raise ServiceUnavailable(
                failure.code if failure is not None else "service_use_closed",
                (
                    failure.message
                    if failure is not None
                    else "Managed service capability is closed"
                ),
                failure.diagnostics if failure is not None else None,
            )
        return self._lease

    async def _wait_for_replacement_lease(
        self,
        predecessor: ServiceUseLease,
        timeout_seconds: float | None,
    ) -> ServiceUseLease:
        """Wait only when a request failed before it was published."""

        try:
            state = await self.wait_for_state(
                lambda current: (
                    self._lease is not predecessor
                    and current.phase
                    in {
                        ManagedServicePhase.READY,
                        ManagedServicePhase.UNAVAILABLE,
                        ManagedServicePhase.CLOSING,
                        ManagedServicePhase.CLOSED,
                    }
                ),
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as exc:
            raise ServiceUnavailable(
                "service_reconnect_timeout",
                "Timed out waiting for replacement Concord authority",
            ) from exc
        if state.phase != ManagedServicePhase.READY or self._lease is None:
            failure = state.failure
            raise ServiceUnavailable(
                failure.code if failure is not None else "service_use_closed",
                (
                    failure.message
                    if failure is not None
                    else "Managed service capability is closed"
                ),
                failure.diagnostics if failure is not None else None,
            )
        return self._lease

    def _publish_locked(
        self,
        phase: ManagedServicePhase,
        *,
        descriptor: ServiceDescriptor | None,
        local_authority: bool,
        failure: ManagedServiceFailure | None = None,
    ) -> None:
        self._state = ManagedServiceState(
            version=self._state_bus.version + 1,
            phase=phase,
            descriptor=descriptor,
            local_authority=local_authority,
            failure=failure,
        )
        self._state_bus.publish_locked(("service",), current=local_authority)


class DeckrServices:
    """Managed surface for service discovery, authority, requests, and views."""

    def __init__(
        self,
        *,
        endpoint: EndpointSession,
        beacon: Beacon,
        concord: Concord,
        task_group: anyio.abc.TaskGroup,
        kv_bucket_for: Callable[[KvBucketPolicy], NatsJsonKvBucket],
    ) -> None:
        self._endpoint = endpoint
        self._beacon = beacon
        self._concord = concord
        self._task_group = task_group
        self._kv_bucket_for = kv_bucket_for
        self._directories: dict[_DirectoryKey, BeaconDirectory[ServiceDescriptor]] = {}
        self._view_stores: dict[str, ServiceViewStore] = {}
        self._active_service_use_leases: dict[int, ServiceUseLease] = {}
        self._shared_managers: dict[Hashable, Any] = {}

    def directory(
        self,
        protocol: ServiceProtocol,
    ) -> BeaconDirectory[ServiceDescriptor]:
        """Return the managed directory for one service protocol."""

        key = _directory_key(protocol)
        directory = self._directories.get(key)
        if directory is None:
            directory = BeaconDirectory(
                self._beacon,
                protocol.feature_id,
                lambda candidate: parse_service_descriptor(candidate, protocol),
                log_label=f"DeckrServices[{protocol.namespace}]",
            )
            directory.start(self._task_group)
            self._directories[key] = directory
        return directory

    def resolve_descriptor(
        self,
        protocol: ServiceProtocol,
        predicate: Callable[[ServiceDescriptor], bool] | None = None,
        *,
        select: Callable[[Collection[ServiceDescriptor]], ServiceDescriptor | None]
        | None = None,
        unavailable_code: str = "service_discovery_pending",
        unavailable_message: str = "Service discovery is not current yet",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> ServiceDescriptor | None:
        """Resolve a service descriptor from a current managed directory view."""

        directory = self.directory(protocol)
        try:
            if not directory.is_current():
                raise KvUnavailable("service discovery is not current")
            return directory.resolve(predicate, select=select)
        except (
            KvUnavailable,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as exc:
            raise ServiceUnavailable(
                unavailable_code,
                unavailable_message,
                diagnostics,
            ) from exc

    async def wait_for_descriptor(
        self,
        protocol: ServiceProtocol,
        predicate: Callable[[ServiceDescriptor], bool] | None = None,
        *,
        select: Callable[[Collection[ServiceDescriptor]], ServiceDescriptor | None]
        | None = None,
        timeout_seconds: float | None = None,
        unavailable_code: str = "beacon_unavailable",
        unavailable_message: str = "Service discovery is unavailable",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> ServiceDescriptor:
        """Wait for a descriptor and expose discovery failures as service errors."""

        try:
            return await self.directory(protocol).wait_for(
                predicate,
                select=select,
                timeout=timeout_seconds,
            )
        except (
            KvUnavailable,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as exc:
            raise ServiceUnavailable(
                unavailable_code,
                unavailable_message,
                diagnostics,
            ) from exc

    @asynccontextmanager
    async def use_matching(
        self,
        protocol: ServiceProtocol,
        *,
        predicate: Callable[[ServiceDescriptor], bool] | None = None,
        timeout_seconds: float | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[ManagedService]:
        """Race matching endpoint sessions and reconnect after authority loss."""

        service = ManagedService(self, reconnect=True)
        started = anyio.Event()
        done = anyio.Event()
        runner_scope: anyio.CancelScope | None = None

        async def run() -> None:
            nonlocal runner_scope
            try:
                with anyio.CancelScope() as scope:
                    runner_scope = scope
                    started.set()
                    await self._run_matching_service(
                        service,
                        protocol,
                        predicate,
                        diagnostics,
                    )
            finally:
                done.set()

        async def close() -> None:
            await service._mark_closed()
            if runner_scope is not None:
                runner_scope.cancel()
            with anyio.move_on_after(5, shield=True):
                await done.wait()

        service._close_callback = close  # noqa: SLF001
        self._task_group.start_soon(run)
        await started.wait()
        try:
            try:
                state = await service.wait_for_state(
                    lambda current: current.phase
                    in {
                        ManagedServicePhase.READY,
                        ManagedServicePhase.UNAVAILABLE,
                        ManagedServicePhase.CLOSED,
                    },
                    timeout_seconds=timeout_seconds,
                )
            except TimeoutError as exc:
                raise ServiceUnavailable(
                    "contract_timeout",
                    "Timed out waiting for a matching service contract",
                    diagnostics,
                ) from exc
            if state.phase != ManagedServicePhase.READY:
                failure = state.failure
                raise ServiceUnavailable(
                    failure.code if failure is not None else "service_use_closed",
                    (
                        failure.message
                        if failure is not None
                        else "Managed service closed during setup"
                    ),
                    failure.diagnostics if failure is not None else diagnostics,
                )
            yield service
        finally:
            await service.aclose()

    @asynccontextmanager
    async def use(
        self,
        descriptor: ServiceDescriptor,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ManagedService]:
        """Open a scoped service-use contract and close it on context exit."""

        lease = await self._propose_service_use(
            descriptor,
            timeout_seconds=timeout_seconds,
        )
        self._active_service_use_leases[id(lease)] = lease
        service = ManagedService(self, lease)

        async def close() -> None:
            await service._mark_closed()
            await self._close_active_service_use_lease(
                lease,
                reason="service_use_closed",
            )

        service._close_callback = close  # noqa: SLF001
        try:
            yield service
        finally:
            await service.aclose()

    async def _run_matching_service(
        self,
        service: ManagedService,
        protocol: ServiceProtocol,
        predicate: Callable[[ServiceDescriptor], bool] | None,
        diagnostics: Mapping[str, Any] | None,
    ) -> None:
        winner: ServiceUseLease | None = None
        try:
            while not service._closed:  # noqa: SLF001
                try:
                    winner = await self._race_service_candidates(
                        protocol,
                        predicate=predicate,
                        diagnostics=diagnostics,
                    )
                except ServiceUnavailable as exc:
                    await service._set_unavailable(exc)
                    return
                if service._closed:  # noqa: SLF001
                    await self._close_active_service_use_lease(
                        winner,
                        reason="service_use_closed",
                    )
                    winner = None
                    return
                await service._set_lease(winner)
                failure = await _wait_for_service_authority_loss(winner)
                if not await service._set_disconnected(winner, failure):
                    return
                await self._close_active_service_use_lease(
                    winner,
                    reason="service_use_reconnecting",
                )
                winner = None
        finally:
            if winner is not None:
                with anyio.CancelScope(shield=True):
                    await self._close_active_service_use_lease(
                        winner,
                        reason="service_use_closed",
                    )

    async def _race_service_candidates(
        self,
        protocol: ServiceProtocol,
        *,
        predicate: Callable[[ServiceDescriptor], bool] | None,
        diagnostics: Mapping[str, Any] | None,
    ) -> ServiceUseLease:
        directory = self.directory(protocol)
        send, receive = anyio.create_memory_object_stream[
            tuple[str, ServiceDescriptor | None, ServiceUseLease | None, BaseException | None]
        ](_MAX_MATCHING_SERVICE_CANDIDATES + 1)
        attempted: set[tuple[str, str]] = set()
        successful: dict[tuple[str, str], ServiceUseLease] = {}
        retry_wakeups: dict[tuple[str, str], anyio.Event] = {}
        winner_identity: tuple[str, str] | None = None

        async def attempt(
            identity: tuple[str, str],
            descriptor: ServiceDescriptor,
        ) -> None:
            delay = 0.1
            while True:
                try:
                    lease = await self._propose_service_use(
                        descriptor,
                        timeout_seconds=None,
                    )
                except ServiceUnavailable as exc:
                    if not _retryable_service_negotiation_failure(exc):
                        await send.send(("failed", descriptor, None, exc))
                        return
                    wakeup = anyio.Event()
                    retry_wakeups[identity] = wakeup
                    with anyio.move_on_after(random.uniform(delay * 0.5, delay)):
                        await wakeup.wait()
                    delay = 0.1 if wakeup.is_set() else min(delay * 2, 5.0)
                    continue
                successful[identity] = lease
                await send.send(("ready", descriptor, lease, None))
                return

        async def discover(task_group: anyio.abc.TaskGroup) -> None:
            try:
                async for descriptors in directory.watch_records():
                    for descriptor in descriptors:
                        if predicate is not None and not predicate(descriptor):
                            continue
                        identity = (str(descriptor.endpoint), descriptor.session_id)
                        if identity in attempted:
                            wakeup = retry_wakeups.get(identity)
                            if wakeup is not None:
                                wakeup.set()
                            continue
                        if len(attempted) >= _MAX_MATCHING_SERVICE_CANDIDATES:
                            await send.send(
                                (
                                    "fatal",
                                    None,
                                    None,
                                    ServiceUnavailable(
                                        "service_candidate_limit_exceeded",
                                        "More than 64 matching service candidates were admitted",
                                        diagnostics,
                                    ),
                                )
                            )
                            return
                        attempted.add(identity)
                        task_group.start_soon(attempt, identity, descriptor)
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                await send.send(("fatal", None, None, exc))
            else:
                await send.send(
                    (
                        "fatal",
                        None,
                        None,
                        ServiceUnavailable(
                            "beacon_unavailable",
                            "Service discovery closed while negotiating",
                            diagnostics,
                        ),
                    )
                )

        failure: BaseException | None = None
        async with anyio.create_task_group() as attempts:
            attempts.start_soon(discover, attempts)
            async with receive:
                async for status, descriptor, lease, error in receive:
                    if status == "ready":
                        assert descriptor is not None and lease is not None
                        winner_identity = (
                            str(descriptor.endpoint),
                            descriptor.session_id,
                        )
                        attempts.cancel_scope.cancel()
                        break
                    if status == "fatal":
                        failure = error
                        attempts.cancel_scope.cancel()
                        break

        winner = (
            successful.get(winner_identity)
            if winner_identity is not None
            else None
        )
        for identity, lease in successful.items():
            self._active_service_use_leases[id(lease)] = lease
            if identity != winner_identity:
                await self._close_active_service_use_lease(
                    lease,
                    reason="service_use_race_lost",
                )
        if winner is not None:
            return winner
        if isinstance(failure, ServiceUnavailable):
            raise failure
        raise ServiceUnavailable(
            "beacon_unavailable",
            "Service discovery failed while negotiating",
            {
                **dict(diagnostics or {}),
                "reason": str(failure) if failure is not None else None,
            },
        )

    async def _send(
        self,
        lease: ServiceUseLease,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        event: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send an authorized one-way service message over the services lane."""

        definition = _message_definition(
            lease,
            name,
            exchange_pattern=ServiceExchangePattern.ONE_WAY,
        )
        _assert_consumer_to_service(definition, name)
        await lease.refresh()
        descriptor = lease.descriptor
        return await self._endpoint.send(
            lane=SERVICES_LANE,
            recipient=descriptor.endpoint,
            recipient_session_id=descriptor.session_id,
            subject=entity_subject(
                "service",
                serviceId=descriptor.service_id,
                namespace=descriptor.namespace,
                name=name,
            ),
            message_type=SERVICE_MESSAGE,
            body=ServiceMessageBody(
                serviceNamespace=descriptor.namespace,
                name=name,
                intent=definition.intent,
                exchangePattern=definition.exchange_pattern,
                params=dict(params or {}),
                event=dict(event) if event is not None else None,
            ).to_dict(),
            contract=_contract_pointer(lease),
        )

    async def _request(
        self,
        lease: ServiceUseLease,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody:
        """Send an authorized request-reply service message over the services lane."""

        definition = _message_definition(
            lease,
            name,
            exchange_pattern=ServiceExchangePattern.REQUEST_REPLY,
        )
        _assert_consumer_to_service(definition, name)
        await lease.refresh()
        descriptor = lease.descriptor
        reply = await self._endpoint.request(
            lane=SERVICES_LANE,
            recipient=descriptor.endpoint,
            recipient_session_id=descriptor.session_id,
            subject=entity_subject(
                "service",
                serviceId=descriptor.service_id,
                namespace=descriptor.namespace,
                name=name,
            ),
            message_type=SERVICE_MESSAGE,
            body=ServiceMessageBody(
                serviceNamespace=descriptor.namespace,
                name=name,
                intent=definition.intent,
                exchangePattern=definition.exchange_pattern,
                params=dict(params or {}),
            ).to_dict(),
            timeout=timeout_seconds,
            contract=_contract_pointer(lease),
        )
        body = service_body(reply)
        if _is_valid_response_body(body, descriptor.namespace, name, definition):
            _assert_response_envelope(
                reply,
                sender=descriptor.endpoint,
                sender_session_id=descriptor.session_id,
                recipient=self._endpoint.address,
                recipient_session_id=self._endpoint.session_id,
                contract=lease.contract,
                service_id=descriptor.service_id,
                service_namespace=descriptor.namespace,
                name=name,
            )
            return body
        raise ServiceUnavailable(
            "invalid_service_response",
            "Service returned an invalid response",
            {
                "serviceId": descriptor.service_id,
                "serviceNamespace": descriptor.namespace,
                "name": name,
            },
        )

    async def _create_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        """Create a consumer-written retained service view under the lease fence."""

        await lease.refresh()
        return await self._view_store(view.store_name).create(
            view=view,
            payload=payload,
            context=self._consumer_view_write_context(lease),
            ttl=ttl,
        )

    async def _put_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        """Put a consumer-written retained service view under the lease fence."""

        await lease.refresh()
        return await self._view_store(view.store_name).put(
            view=view,
            payload=payload,
            context=self._consumer_view_write_context(lease),
            revision=revision,
            ttl=ttl,
        )

    async def _update_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        """Update a consumer-written retained service view under the lease fence."""

        await lease.refresh()
        return await self._view_store(view.store_name).update(
            view=view,
            payload=payload,
            context=self._consumer_view_write_context(lease),
            revision=revision,
            ttl=ttl,
        )

    async def _delete_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
        *,
        revision: int | None = None,
    ) -> None:
        """Delete a consumer-written retained service view under the lease fence."""

        await lease.refresh()
        await self._view_store(view.store_name).delete(
            view=view,
            context=self._consumer_view_write_context(lease),
            revision=revision,
        )

    async def _read_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> Mapping[str, Any] | None:
        """Read a fenced service view entry authorized by the supplied lease.

        ``None`` is an ordinary absent-view value under the active lease; it
        does not imply service-use loss.
        """

        try:
            await lease.refresh()
            entry = await self._view_store(view.store_name).get(
                self._consumer_view_read_context(lease),
                view,
            )
        except KvUnavailable as exc:
            raise _service_view_unavailable(view) from exc
        return dict(entry.value) if entry is not None else None

    async def _watch_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> AsyncIterator[Mapping[str, Any] | None]:
        """Yield the current fenced service view payload and subsequent changes.

        A yielded ``None`` reports absent view state under the same service-use
        lease. Consumers should treat service-use lifecycle changes as
        ``ServiceUnavailable`` failures from the lease, command, or view
        infrastructure instead.
        """

        try:
            store = self._view_store(view.store_name)
            await lease.refresh()
            async with store.watch(
                self._consumer_view_read_context(lease),
                view,
            ) as changes:
                async for snapshot in changes:
                    await lease.refresh()
                    yield (
                        dict(snapshot.entry.value)
                        if snapshot.entry is not None
                        else None
                    )
        except KvUnavailable as exc:
            raise _service_view_unavailable(view) from exc

    async def _authorize_inbound_message(
        self,
        lease: ServiceUseLease,
        message: DeckrMessage,
        *,
        name: str | None = None,
    ) -> ServiceMessageBody:
        """Validate service-to-consumer traffic against the active lease."""

        await lease.refresh()
        return _authorize_inbound_message(
            lease,
            message,
            local_endpoint=self._endpoint.address,
            local_session_id=self._endpoint.session_id,
            name=name,
        )

    def _view_access(
        self,
        lease: ServiceUseLease,
        store_name: str,
    ) -> ManagedServiceViewAccess:
        """Return consumer-side managed view access for one store under a lease."""

        return _LeaseManagedServiceViewAccess(
            self._view_store(store_name),
            lease=lease,
            read_context=self._consumer_view_read_context(lease),
            write_context=self._consumer_view_write_context(lease),
        )

    def get_shared_manager(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        """Return a runtime-scoped shared service helper."""

        manager = self._shared_managers.get(key)
        if manager is None:
            manager = factory()
            self._shared_managers[key] = manager
        return manager

    async def aclose(self) -> None:
        for manager in tuple(self._shared_managers.values()):
            aclose = getattr(manager, "aclose", None)
            if callable(aclose):
                await aclose()
        self._shared_managers.clear()
        for lease in tuple(self._active_service_use_leases.values()):
            await self._close_active_service_use_lease(
                lease,
                reason="service_use_closed",
            )
        for directory in tuple(self._directories.values()):
            await directory.aclose()
        self._directories.clear()
        self._view_stores.clear()

    async def _close_active_service_use_lease(
        self,
        lease: ServiceUseLease,
        *,
        reason: str,
    ) -> None:
        if self._active_service_use_leases.pop(id(lease), None) is None:
            return
        try:
            await lease.agreement.cancel(reason)
        except (ConcordConflict, ConcordUnavailable):
            pass
        finally:
            await lease.agreement.aclose()

    async def _propose_service_use(
        self,
        descriptor: ServiceDescriptor,
        *,
        timeout_seconds: float | None,
    ) -> ServiceUseLease:
        agreement = None

        async def wait_for_valid() -> ServiceUseLease:
            nonlocal agreement
            try:
                agreement = self._concord.propose(
                    self._endpoint,
                    ConcordAgreementSpec(
                        participants=(self._endpoint.address, descriptor.endpoint),
                        profile=descriptor.use_profile,
                        terms=None,
                        participant_sessions={
                            str(descriptor.endpoint): descriptor.session_id,
                        },
                    ),
                )
                await agreement.__aenter__()
            except ConcordConflict as exc:
                raise _service_use_conflict(
                    descriptor,
                    exc,
                    agreement=None,
                ) from exc
            except ConcordUnavailable as exc:
                raise ServiceUnavailable(
                    "contract_unavailable",
                    "Concord authority is unavailable during service-use setup",
                    {
                        "reason": str(exc),
                        "profile": descriptor.use_profile,
                        "serviceId": descriptor.service_id,
                        "serviceSessionId": descriptor.session_id,
                    },
                ) from exc
            except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
                raise ServiceUnavailable(
                    "service_use_closed",
                    "Concord authority closed during service-use setup",
                    {
                        "profile": descriptor.use_profile,
                        "serviceId": descriptor.service_id,
                        "serviceSessionId": descriptor.session_id,
                    },
                ) from exc
            try:
                state = await agreement.wait_for_state(
                    lambda current: current.valid
                    or current.phase
                    in {
                        ConcordLifecyclePhase.UNAVAILABLE,
                        ConcordLifecyclePhase.TERMINAL,
                        ConcordLifecyclePhase.CLOSED,
                    }
                    or (
                        current.validity is not None
                        and service_use_negotiation_terminal_status(
                            current.validity.status
                        )
                    )
                )
            except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
                raise ServiceUnavailable(
                    "service_use_closed",
                    "Concord agreement closed during service-use setup",
                    {
                        "profile": descriptor.use_profile,
                        "serviceId": descriptor.service_id,
                        "serviceSessionId": descriptor.session_id,
                    },
                ) from exc
            validity = state.validity
            if state.valid and validity is not None:
                return ServiceUseLease(
                    agreement=agreement,
                    descriptor=descriptor,
                    consumer_endpoint=self._endpoint.address,
                    consumer_session_id=self._endpoint.session_id,
                )
            contract = state.contract
            if validity is None:
                raise ServiceUnavailable(
                    "service_use_conflict",
                    state.reason or "Service-use agreement could not acquire authority",
                    {
                        "contractId": (
                            contract.contract_id if contract is not None else None
                        ),
                        "generation": (
                            contract.generation if contract is not None else None
                        ),
                        "profile": descriptor.use_profile,
                        "serviceId": descriptor.service_id,
                        "serviceSessionId": descriptor.session_id,
                    },
                )
            if (
                state.phase == ConcordLifecyclePhase.UNAVAILABLE
                or validity.status == ContractValidityStatus.UNAVAILABLE
                or not state.current
            ):
                code = "contract_unavailable"
                message = "Service-use authority became unavailable during setup"
            elif validity.status != ContractValidityStatus.VALID:
                code = f"contract_{validity.status.value}"
                message = "Service-use contract became terminal during negotiation"
            else:
                code = "contract_local_authority_lost"
                message = "Service-use agreement lost local authority during setup"
            raise ServiceUnavailable(
                code,
                message,
                {
                    "status": validity.status.value,
                    "reason": validity.reason,
                    "phase": state.phase.value,
                    "current": state.current,
                    "localAuthority": state.local_authority,
                    "contractId": (
                        contract.contract_id if contract is not None else None
                    ),
                    "generation": contract.generation if contract is not None else None,
                    "profile": descriptor.use_profile,
                    "serviceId": descriptor.service_id,
                    "serviceSessionId": descriptor.session_id,
                },
            )

        try:
            if timeout_seconds is None:
                return await wait_for_valid()
            with anyio.fail_after(timeout_seconds):
                return await wait_for_valid()
        except TimeoutError as exc:
            contract_id = None
            generation = None
            if agreement is not None:
                contract = agreement.state.contract
                if contract is not None:
                    contract_id = contract.contract_id
                    generation = contract.generation
                    try:
                        await agreement.cancel("contract_timeout")
                    except ConcordConflict:
                        pass
                await agreement.aclose()
            raise ServiceUnavailable(
                "contract_timeout",
                "Timed out waiting for service-use contract to become valid",
                {
                    "contractId": contract_id,
                    "generation": generation,
                    "profile": descriptor.use_profile,
                    "serviceId": descriptor.service_id,
                    "serviceSessionId": descriptor.session_id,
                },
            ) from exc
        except BaseException:
            if agreement is not None:
                with anyio.CancelScope(shield=True):
                    await agreement.aclose()
            raise

    def _view_store(self, store_name: str) -> ServiceViewStore:
        store = self._view_stores.get(store_name)
        if store is None:
            policy = KvBucketPolicy(
                bucket=store_name,
                ttl_seconds=None,
                description=f"Deckr service view {store_name}",
            )
            store = ServiceViewStore(bucket=self._kv_bucket_for(policy))
            store.start(self._task_group)
            self._view_stores[store_name] = store
        return store

    def _consumer_view_write_context(
        self,
        lease: ServiceUseLease,
    ) -> ServiceViewWriteContext:
        descriptor = lease.descriptor
        return ServiceViewWriteContext(
            writer=ServiceViewWriter.CONSUMER,
            service_id=descriptor.service_id,
            service_namespace=descriptor.namespace,
            service_endpoint=descriptor.endpoint,
            service_session_id=descriptor.session_id,
            consumer_endpoint=self._endpoint.address,
            consumer_session_id=self._endpoint.session_id,
            contract=ContractPointer(
                contractId=lease.contract.contract_id,
                generation=lease.contract.generation,
            ),
            views=descriptor.views,
        )

    def _consumer_view_read_context(
        self,
        lease: ServiceUseLease,
    ) -> ServiceViewReadContext:
        descriptor = lease.descriptor
        return ServiceViewReadContext(
            reader=ServiceViewWriter.CONSUMER,
            service_id=descriptor.service_id,
            service_namespace=descriptor.namespace,
            service_endpoint=descriptor.endpoint,
            service_session_id=descriptor.session_id,
            consumer_endpoint=self._endpoint.address,
            consumer_session_id=self._endpoint.session_id,
            contract=ContractPointer(
                contractId=lease.contract.contract_id,
                generation=lease.contract.generation,
            ),
            views=descriptor.views,
        )


class ManagedServiceContract:
    """Service-side helpers bound to one managed service-use contract."""

    def __init__(
        self,
        *,
        endpoint: EndpointSession,
        participant: ConcordParticipant,
        contract: ContractHandle,
        protocol: ServiceProtocol,
        service_id: str,
    ) -> None:
        self._endpoint = endpoint
        self._participant = participant
        self._contract = contract
        self._protocol = protocol
        self._service_id = service_id

    async def send(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        event: Mapping[str, Any] | None = None,
        subject=None,
    ) -> DeckrMessage:
        context = await self._context()
        definition = _message_definition_from_mapping(
            context.protocol.messages,
            name,
            exchange_pattern=ServiceExchangePattern.ONE_WAY,
            service_id=context.service_id,
        )
        _assert_service_to_consumer(definition, name)
        return await self._endpoint.send(
            lane=SERVICES_LANE,
            recipient=context.consumer_endpoint,
            recipient_session_id=context.consumer_session_id,
            subject=subject or _service_message_subject(context, name),
            message_type=SERVICE_MESSAGE,
            body=ServiceMessageBody(
                serviceNamespace=context.service_namespace,
                name=name,
                intent=definition.intent,
                exchangePattern=definition.exchange_pattern,
                params=dict(params or {}),
                event=dict(event) if event is not None else None,
            ).to_dict(),
            contract=context.contract,
        )

    async def request(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody:
        context = await self._context()
        definition = _message_definition_from_mapping(
            context.protocol.messages,
            name,
            exchange_pattern=ServiceExchangePattern.REQUEST_REPLY,
            service_id=context.service_id,
        )
        _assert_service_to_consumer(definition, name)
        reply = await self._endpoint.request(
            lane=SERVICES_LANE,
            recipient=context.consumer_endpoint,
            recipient_session_id=context.consumer_session_id,
            subject=_service_message_subject(context, name),
            message_type=SERVICE_MESSAGE,
            body=ServiceMessageBody(
                serviceNamespace=context.service_namespace,
                name=name,
                intent=definition.intent,
                exchangePattern=definition.exchange_pattern,
                params=dict(params or {}),
            ).to_dict(),
            timeout=timeout_seconds,
            contract=context.contract,
        )
        body = service_body(reply)
        if _is_valid_response_body(
            body,
            context.service_namespace,
            name,
            definition,
        ):
            _assert_response_envelope(
                reply,
                sender=context.consumer_endpoint,
                sender_session_id=context.consumer_session_id,
                recipient=self._endpoint.address,
                recipient_session_id=self._endpoint.session_id,
                contract=context.contract,
                service_id=context.service_id,
                service_namespace=context.service_namespace,
                name=name,
            )
            return body
        raise ServiceUnavailable(
            "invalid_service_response",
            "Consumer returned an invalid service response",
            {
                "serviceId": context.service_id,
                "serviceNamespace": context.service_namespace,
                "name": name,
            },
        )

    def view_access(self, store: ServiceViewStore) -> ManagedServiceViewAccess:
        return _ServiceManagedServiceViewAccess(
            store,
            contract=self,
        )

    async def _context(self) -> ServiceManagedContractContext:
        if (
            self._participant.participant != self._endpoint.address
            or self._participant.session_id != self._endpoint.session_id
        ):
            raise ServiceUnavailable(
                "contract_session_mismatch",
                "Managed service contract endpoint does not match the participant session",
                _contract_diagnostics(
                    self._contract,
                    service_id=self._service_id,
                    service_session_id=self._endpoint.session_id,
                ),
            )
        managed = await self._managed_contract()
        validity = managed.validity
        if not validity.valid or validity.contract is None:
            raise ServiceUnavailable(
                f"contract_{validity.status.value}",
                "Managed service contract is not valid",
                {
                    **_contract_diagnostics(
                        self._contract,
                        service_id=self._service_id,
                        service_session_id=self._endpoint.session_id,
                    ),
                    "status": validity.status.value,
                    "reason": validity.reason,
                },
            )
        try:
            context = service_managed_contract_context(
                managed,
                protocol=self._protocol,
                service_id=self._service_id,
                record=validity.contract,
                validity=validity,
            )
        except ValueError as exc:
            raise ServiceUnavailable(
                "invalid_service_contract",
                "Managed service contract does not match the service protocol",
                _contract_diagnostics(
                    self._contract,
                    service_id=self._service_id,
                    service_session_id=self._endpoint.session_id,
                ),
            ) from exc
        expected_pointer = ContractPointer(
            contractId=self._contract.contract_id,
            generation=self._contract.generation,
        )
        if (
            context.service_endpoint != self._endpoint.address
            or context.service_session_id != self._endpoint.session_id
            or context.contract != expected_pointer
        ):
            raise ServiceUnavailable(
                "contract_session_mismatch",
                "Managed service contract context is stale",
                _contract_diagnostics(
                    self._contract,
                    service_id=self._service_id,
                    service_session_id=self._endpoint.session_id,
                ),
            )
        return context

    async def _managed_contract(self) -> ConcordManagedContract:
        managed = self._participant.managed_contract(self._contract)
        if managed is None:
            raise ServiceUnavailable(
                "contract_not_managed",
                "Managed service contract is not managed by this participant",
                _contract_diagnostics(
                    self._contract,
                    service_id=self._service_id,
                    service_session_id=self._endpoint.session_id,
                ),
            )
        return managed


class _ServiceManagedServiceViewAccess(ManagedServiceViewAccess):
    def __init__(
        self,
        store: ServiceViewStore,
        *,
        contract: ManagedServiceContract,
    ) -> None:
        super().__init__(store)
        self._contract = contract

    async def read(self, view: ServiceViewRef) -> ServiceViewEntry | None:
        context = await self._contract._context()
        return await self._store.get(context.view_read_context(), view)

    @asynccontextmanager
    async def watch(
        self,
        view: ServiceViewRef,
    ) -> AsyncIterator[
        AsyncIterator[ServiceViewWatchSnapshot | ServiceViewWatchChange]
    ]:
        context = await self._contract._context()
        async with self._store.watch(context.view_read_context(), view) as changes:
            yield changes

    async def create(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        context = await self._contract._context()
        return await self._store.create(
            view=view,
            payload=payload,
            context=context.view_write_context(),
            ttl=ttl,
        )

    async def put(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        context = await self._contract._context()
        return await self._store.put(
            view=view,
            payload=payload,
            context=context.view_write_context(),
            revision=revision,
            ttl=ttl,
        )

    async def update(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        context = await self._contract._context()
        return await self._store.update(
            view=view,
            payload=payload,
            context=context.view_write_context(),
            revision=revision,
            ttl=ttl,
        )

    async def delete(
        self,
        view: ServiceViewRef,
        *,
        revision: int | None = None,
    ) -> None:
        context = await self._contract._context()
        await self._store.delete(
            view=view,
            context=context.view_write_context(),
            revision=revision,
        )


class _LeaseManagedServiceViewAccess(ManagedServiceViewAccess):
    def __init__(
        self,
        store: ServiceViewStore,
        *,
        lease: ServiceUseLease,
        read_context: ServiceViewReadContext | None = None,
        write_context: ServiceViewWriteContext | None = None,
    ) -> None:
        super().__init__(
            store,
            read_context=read_context,
            write_context=write_context,
        )
        self._lease = lease

    async def read(self, view: ServiceViewRef) -> ServiceViewEntry | None:
        await self._lease.refresh()
        return await super().read(view)

    @asynccontextmanager
    async def watch(
        self,
        view: ServiceViewRef,
    ) -> AsyncIterator[
        AsyncIterator[ServiceViewWatchSnapshot | ServiceViewWatchChange]
    ]:
        await self._lease.refresh()
        async with super().watch(view) as changes:
            yield changes

    async def create(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        await self._lease.refresh()
        return await super().create(view, payload, ttl=ttl)

    async def put(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int | None = None,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        await self._lease.refresh()
        return await super().put(view, payload, revision=revision, ttl=ttl)

    async def update(
        self,
        view: ServiceViewRef,
        payload: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> ServiceViewEntry:
        await self._lease.refresh()
        return await super().update(view, payload, revision=revision, ttl=ttl)

    async def delete(
        self,
        view: ServiceViewRef,
        *,
        revision: int | None = None,
    ) -> None:
        await self._lease.refresh()
        await super().delete(view, revision=revision)


def _directory_key(protocol: ServiceProtocol) -> _DirectoryKey:
    return (
        protocol.namespace,
        protocol.feature_id,
        protocol.advertisement_profile,
        protocol.use_profile,
    )


def _deadline(timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return monotonic() + max(0.0, float(timeout_seconds))


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - monotonic())


def _lease_pointer(lease: ServiceUseLease) -> ContractPointer:
    return ContractPointer(
        contractId=lease.contract.contract_id,
        generation=lease.contract.generation,
    )


def _managed_failure(error: ServiceUnavailable) -> ManagedServiceFailure:
    return ManagedServiceFailure(
        code=error.code,
        message=error.message,
        diagnostics=error.diagnostics,
    )


async def _wait_for_service_authority_loss(
    lease: ServiceUseLease,
) -> ServiceUnavailable:
    try:
        await lease.agreement.wait_for_state(lambda state: not state.valid)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
        return ServiceUnavailable(
            "service_use_closed",
            "Managed service agreement closed while authoritative",
            {"serviceId": lease.descriptor.service_id},
        )
    try:
        await lease.refresh()
    except ServiceUnavailable as exc:
        return exc
    return ServiceUnavailable(
        "contract_local_authority_lost",
        "Managed service capability lost local authority",
        {"serviceId": lease.descriptor.service_id},
    )


def _message_definition(
    lease: ServiceUseLease,
    name: str,
    *,
    exchange_pattern: ServiceExchangePattern,
) -> ServiceMessageDefinition:
    return _message_definition_from_mapping(
        lease.descriptor.supported_messages,
        name,
        exchange_pattern=exchange_pattern,
        service_id=lease.descriptor.service_id,
    )


def _message_definition_from_mapping(
    messages: Mapping[str, ServiceMessageDefinition],
    name: str,
    *,
    exchange_pattern: ServiceExchangePattern,
    service_id: str,
) -> ServiceMessageDefinition:
    definition = messages.get(name)
    if definition is None:
        raise ServiceUnavailable(
            "unsupported_service_message",
            "Service descriptor does not support this message",
            {"name": name, "serviceId": service_id},
        )
    if definition.exchange_pattern != exchange_pattern:
        raise ServiceUnavailable(
            "unsupported_service_exchange_pattern",
            "Service message uses a different exchange pattern",
            {
                "name": name,
                "exchangePattern": definition.exchange_pattern.value,
                "expectedExchangePattern": exchange_pattern.value,
            },
        )
    return definition


def _assert_consumer_to_service(
    definition: ServiceMessageDefinition,
    name: str,
) -> None:
    if definition.direction in {
        ServiceMessageDirection.CONSUMER_TO_SERVICE,
        ServiceMessageDirection.BIDIRECTIONAL,
    }:
        return
    raise ServiceUnavailable(
        "unsupported_service_message_direction",
        "Service message direction does not allow consumer-to-service sends",
        {"name": name, "direction": definition.direction.value},
    )


def _assert_service_to_consumer(
    definition: ServiceMessageDefinition,
    name: str,
) -> None:
    if definition.direction in {
        ServiceMessageDirection.SERVICE_TO_CONSUMER,
        ServiceMessageDirection.BIDIRECTIONAL,
    }:
        return
    raise ServiceUnavailable(
        "unsupported_service_message_direction",
        "Service message direction does not allow service-to-consumer sends",
        {"name": name, "direction": definition.direction.value},
    )


def _authorize_inbound_message(
    lease: ServiceUseLease,
    message: DeckrMessage,
    *,
    local_endpoint: Any,
    local_session_id: str,
    name: str | None,
) -> ServiceMessageBody:
    if message.message_type != SERVICE_MESSAGE:
        raise ServiceUnavailable(
            "invalid_service_message",
            "Inbound service traffic must use serviceMessage",
            {"messageType": message.message_type},
        )
    try:
        body = service_body(message)
    except (TypeError, ValueError) as exc:
        raise ServiceUnavailable(
            "invalid_service_message",
            "Inbound service message body is invalid",
        ) from exc
    descriptor = lease.descriptor
    expected_name = body.name if name is None else name
    definition = _message_definition_from_mapping(
        descriptor.supported_messages,
        expected_name,
        exchange_pattern=body.exchange_pattern,
        service_id=descriptor.service_id,
    )
    _assert_service_to_consumer(definition, expected_name)
    if (
        body.service_namespace != descriptor.namespace
        or body.name != expected_name
        or body.intent != definition.intent
    ):
        raise ServiceUnavailable(
            "scope_mismatch",
            "Inbound service message does not match the active lease",
            {
                "serviceId": descriptor.service_id,
                "serviceNamespace": body.service_namespace,
                "expectedNamespace": descriptor.namespace,
                "name": body.name,
                "expectedName": expected_name,
            },
        )
    _assert_response_envelope(
        message,
        sender=descriptor.endpoint,
        sender_session_id=descriptor.session_id,
        recipient=local_endpoint,
        recipient_session_id=local_session_id,
        contract=lease.contract,
        service_id=descriptor.service_id,
        service_namespace=descriptor.namespace,
        name=expected_name,
    )
    return body


def _is_valid_response_body(
    body: ServiceMessageBody,
    service_namespace: str,
    name: str,
    definition: ServiceMessageDefinition,
) -> bool:
    return (
        body.service_namespace == service_namespace
        and body.name == name
        and body.intent == definition.intent
        and body.exchange_pattern == definition.exchange_pattern
        and body.status is not None
    )


def _assert_response_envelope(
    message: DeckrMessage,
    *,
    sender: Any,
    sender_session_id: str,
    recipient: Any,
    recipient_session_id: str,
    contract: Any,
    service_id: str,
    service_namespace: str,
    name: str,
) -> None:
    pointer = ContractPointer(
        contractId=contract.contract_id,
        generation=contract.generation,
    ) if hasattr(contract, "contract_id") else ContractPointer.model_validate(contract)
    if (
        message.sender != sender
        or message.sender_session_id != sender_session_id
        or not isinstance(message.recipient, EndpointTarget)
        or message.recipient.endpoint != recipient
        or message.recipient_session_id != recipient_session_id
        or message.contract != pointer
        or not _service_subject_matches(
            message,
            service_id=service_id,
            namespace=service_namespace,
            name=name,
        )
    ):
        raise ServiceUnavailable(
            "invalid_service_response",
            "Service response envelope does not match the active service-use lease",
            {
                "serviceId": service_id,
                "serviceNamespace": service_namespace,
                "name": name,
            },
        )


def _service_message_subject(
    context: ServiceManagedContractContext,
    name: str,
) -> Any:
    return entity_subject(
        "service",
        serviceId=context.service_id,
        namespace=context.service_namespace,
        name=name,
    )


def _managed_consumer_sessions(
    managed: ConcordManagedContract,
    *,
    service_endpoint: Any,
) -> dict[str, str]:
    consumers = [
        participant
        for participant in managed.record.participants
        if participant != service_endpoint
    ]
    if len(consumers) != 1:
        return {}
    consumer_endpoint = consumers[0]
    consumer_token = managed.validity.tokens.get(str(consumer_endpoint))
    if consumer_token is None:
        return {}
    return {str(consumer_endpoint): consumer_token.session_id}


def _contract_diagnostics(
    contract: ContractHandle,
    *,
    service_id: str,
    service_session_id: str,
) -> dict[str, Any]:
    return {
        "contractId": contract.contract_id,
        "generation": contract.generation,
        "profile": contract.profile,
        "serviceId": service_id,
        "serviceSessionId": service_session_id,
    }


def _service_subject_matches(
    message: DeckrMessage,
    *,
    service_id: str,
    namespace: str,
    name: str,
) -> bool:
    subject = message.subject
    identifiers = subject.identifiers
    return (
        subject.kind == "service"
        and identifiers.get("serviceId") == service_id
        and identifiers.get("namespace") == namespace
        and identifiers.get("name") == name
    )


def _contract_pointer(lease: ServiceUseLease) -> dict[str, int | str]:
    return {
        "contractId": lease.contract.contract_id,
        "generation": lease.contract.generation,
    }


def _service_view_unavailable(view: ServiceViewRef) -> ServiceUnavailable:
    return ServiceUnavailable(
        "service_view_unavailable",
        "Service view is unavailable",
        {"storeName": view.store_name, "key": view.key},
    )


def _service_use_conflict(
    descriptor: ServiceDescriptor,
    exc: ConcordConflict,
    *,
    agreement: Any | None,
) -> ServiceUnavailable:
    diagnostics = {
        "reason": str(exc),
        "contractId": None,
        "generation": None,
        "profile": descriptor.use_profile,
        "serviceId": descriptor.service_id,
        "serviceSessionId": descriptor.session_id,
    }
    if agreement is not None:
        diagnostics["contractId"] = agreement.contract.contract_id
        diagnostics["generation"] = agreement.contract.generation
    terminal_status = terminal_concord_conflict_status(exc)
    return ServiceUnavailable(
        (
            f"contract_{terminal_status.value}"
            if terminal_status is not None
            else "service_use_conflict"
        ),
        "Service-use contract could not be established",
        diagnostics,
    )


def _retryable_service_negotiation_failure(error: ServiceUnavailable) -> bool:
    """Retry only a proposal whose transport/view authority was unavailable.

    Contract rejection, session fencing, token loss, and malformed authority are
    terminal for that exact candidate attempt.  Retrying those outcomes would
    turn a deterministic rejection into an unbounded proposal loop.
    """

    return error.code == "contract_unavailable"
