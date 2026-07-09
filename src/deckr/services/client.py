"""Managed service-use client for Deckr service consumers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection, Hashable, Mapping
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

import anyio

from deckr.beacon import Beacon, BeaconDirectory
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordManagedContract,
    ConcordParticipant,
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
    ServiceViewChange,
    ServiceViewEntry,
    ServiceViewStore,
)
from deckr.substrates.nats_kv import KvBucketPolicy, KvUnavailable, NatsJsonKvBucket

logger = logging.getLogger(__name__)

_DirectoryKey = tuple[str, str, str, str]


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
        service_use_token_refresh_seconds: float = (
            DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
        ),
    ) -> None:
        if service_use_token_refresh_seconds <= 0:
            raise ValueError(
                "service_use_token_refresh_seconds must be greater than zero"
            )
        self._endpoint = endpoint
        self._beacon = beacon
        self._concord = concord
        self._task_group = task_group
        self._kv_bucket_for = kv_bucket_for
        self._service_use_token_refresh_seconds = service_use_token_refresh_seconds
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
        except KvUnavailable as exc:
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
        except KvUnavailable as exc:
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
        select: Callable[[Collection[ServiceDescriptor]], ServiceDescriptor | None]
        | None = None,
        timeout_seconds: float | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[ServiceUseLease]:
        """Find a matching service and hold a scoped service-use lease."""

        deadline = _deadline(timeout_seconds)
        descriptor = await self.wait_for_descriptor(
            protocol,
            predicate,
            select=select,
            timeout_seconds=_remaining_timeout(deadline),
            diagnostics=diagnostics,
        )
        async with self.use(
            descriptor,
            timeout_seconds=_remaining_timeout(deadline),
        ) as lease:
            yield lease

    @asynccontextmanager
    async def use(
        self,
        descriptor: ServiceDescriptor,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ServiceUseLease]:
        """Open a scoped service-use contract and close it on context exit."""

        lease = await self._propose_service_use(
            descriptor,
            timeout_seconds=timeout_seconds,
        )
        self._active_service_use_leases[id(lease)] = lease
        try:
            yield lease
        finally:
            await self._close_active_service_use_lease(
                lease,
                reason="service_use_closed",
            )

    async def send(
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

    async def request(
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

    async def create_view(
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

    async def put_view(
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

    async def update_view(
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

    async def delete_view(
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

    async def read_view(
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

    async def watch_view(
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

        yield await self.read_view(lease, view)
        try:
            store = self._view_store(view.store_name)
            await lease.refresh()
            async with store.watch(
                self._consumer_view_read_context(lease),
                view,
            ) as changes:
                async for change in changes:
                    await lease.refresh()
                    yield (
                        dict(change.entry.value)
                        if change.entry is not None
                        else None
                    )
        except KvUnavailable as exc:
            raise _service_view_unavailable(view) from exc

    async def authorize_inbound_message(
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

    async def validate_inbound_message(
        self,
        lease: ServiceUseLease,
        message: DeckrMessage,
        *,
        name: str | None = None,
    ) -> ServiceMessageBody:
        """Validate service-to-consumer traffic against the active lease."""

        return await self.authorize_inbound_message(lease, message, name=name)

    def view_access(
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
        except ConcordConflict:
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
                agreement = await self._concord.propose(
                    ConcordAgreementSpec(
                        participants=(self._endpoint.address, descriptor.endpoint),
                        local_participant=self._endpoint.address,
                        local_session_id=self._endpoint.session_id,
                        profile=descriptor.use_profile,
                        terms=None,
                        refresh_interval=self._service_use_token_refresh_seconds,
                        log_label=f"DeckrServices[{descriptor.namespace}]",
                    ),
                    start_soon=self._task_group.start_soon,
                )
            except ConcordConflict as exc:
                raise _service_use_conflict(
                    descriptor,
                    exc,
                    agreement=None,
                ) from exc
            while True:
                try:
                    validity = await agreement.refresh()
                except ConcordConflict as exc:
                    status = terminal_concord_conflict_status(exc)
                    if status is None:
                        await agreement.aclose()
                        raise _service_use_conflict(
                            descriptor,
                            exc,
                            agreement=agreement,
                        ) from exc
                    await agreement.aclose()
                    raise ServiceUnavailable(
                        f"contract_{status.value}",
                        "Service-use contract became terminal during negotiation",
                        {
                            "status": status.value,
                            "reason": str(exc),
                            "contractId": agreement.contract.contract_id,
                            "generation": agreement.contract.generation,
                            "profile": descriptor.use_profile,
                            "serviceId": descriptor.service_id,
                            "serviceSessionId": descriptor.session_id,
                        },
                    ) from exc
                if validity.valid:
                    return ServiceUseLease(
                        agreement=agreement,
                        descriptor=descriptor,
                        consumer_endpoint=self._endpoint.address,
                        consumer_session_id=self._endpoint.session_id,
                    )
                if service_use_negotiation_terminal_status(validity.status):
                    await agreement.aclose()
                    raise ServiceUnavailable(
                        f"contract_{validity.status.value}",
                        "Service-use contract became terminal during negotiation",
                        {
                            "status": validity.status.value,
                            "reason": validity.reason,
                            "contractId": agreement.contract.contract_id,
                            "generation": agreement.contract.generation,
                            "profile": descriptor.use_profile,
                            "serviceId": descriptor.service_id,
                            "serviceSessionId": descriptor.session_id,
                        },
                    )
                await anyio.sleep(
                    0.5
                    if validity.status == ContractValidityStatus.UNAVAILABLE
                    else 0.25
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
                contract_id = agreement.contract.contract_id
                generation = agreement.contract.generation
                try:
                    await agreement.cancel("contract_timeout")
                except ConcordConflict:
                    pass
                finally:
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
        current_sessions = _managed_consumer_sessions(
            managed,
            service_endpoint=self._endpoint.address,
        )
        try:
            validity = await self._participant.validate(
                managed.contract,
                current_sessions=current_sessions,
            )
        except ConcordConflict as exc:
            status = (
                terminal_concord_conflict_status(exc)
                or ContractValidityStatus.INVALID_TOKEN
            )
            raise ServiceUnavailable(
                f"contract_{status.value}",
                "Managed service contract could not be validated",
                {
                    **_contract_diagnostics(
                        self._contract,
                        service_id=self._service_id,
                        service_session_id=self._endpoint.session_id,
                    ),
                    "status": status.value,
                    "reason": str(exc),
                },
            ) from exc
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
            await self._participant.reconcile(reason="managed service contract refresh")
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
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ServiceViewChange]]:
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
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[ServiceViewChange]]:
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
    return ServiceUnavailable(
        "service_use_conflict",
        "Service-use contract could not be established",
        diagnostics,
    )
