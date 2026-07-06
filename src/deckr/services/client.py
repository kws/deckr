"""Managed service-use client for Deckr service consumers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
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
    ContractValidityStatus,
)
from deckr.contracts.messages import SERVICES_LANE, entity_subject
from deckr.lanes import EndpointSession
from deckr.services.messages import (
    SERVICE_COMMAND,
    ServiceCommandBody,
    ServiceCommandReplyBody,
    service_body,
)
from deckr.services.runtime import (
    ServiceDescriptor,
    ServiceProtocol,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceViewRef,
    parse_service_descriptor,
    service_use_negotiation_terminal_status,
    service_use_terms,
    terminal_concord_conflict_status,
)
from deckr.services.views import ServiceViewStore
from deckr.substrates.nats_kv import KvBucketPolicy, KvUnavailable, NatsJsonKvBucket

logger = logging.getLogger(__name__)

_DirectoryKey = tuple[str, str, str, str]


class DeckrServices:
    """Managed surface for service discovery, authority, commands, and views."""

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
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
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
            operations=operations,
            views=views,
            timeout_seconds=_remaining_timeout(deadline),
        ) as lease:
            yield lease

    @asynccontextmanager
    async def use(
        self,
        descriptor: ServiceDescriptor,
        *,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[ServiceUseLease]:
        """Open a scoped service-use contract and close it on context exit."""

        lease = await self._propose_service_use(
            descriptor,
            operations=operations,
            views=views,
            timeout_seconds=timeout_seconds,
        )
        try:
            yield lease
        finally:
            try:
                await lease.agreement.cancel("service_use_closed")
            except ConcordConflict:
                pass
            finally:
                await lease.agreement.aclose()

    async def command(
        self,
        lease: ServiceUseLease,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ServiceCommandReplyBody:
        """Send an authorized service command over the services lane."""

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
                operation=operation,
            ),
            message_type=SERVICE_COMMAND,
            body=ServiceCommandBody(
                serviceNamespace=descriptor.namespace,
                operation=operation,
                params=dict(params or {}),
            ).to_dict(),
            timeout=timeout_seconds,
            contract={
                "contractId": lease.contract.contract_id,
                "generation": lease.contract.generation,
            },
        )
        body = service_body(reply)
        if isinstance(body, ServiceCommandReplyBody):
            return body
        raise ServiceUnavailable(
            "invalid_service_reply",
            "Service returned an invalid command reply",
            {
                "serviceId": descriptor.service_id,
                "serviceNamespace": descriptor.namespace,
                "operation": operation,
            },
        )

    async def read_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> Mapping[str, Any] | None:
        """Read a fenced service view entry authorized by the supplied lease."""

        try:
            entry = await self._view_store(view.store_name).get(lease, view)
        except KvUnavailable as exc:
            raise _service_view_unavailable(view) from exc
        return dict(entry.value) if entry is not None else None

    async def watch_view(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> AsyncIterator[Mapping[str, Any] | None]:
        """Yield the current fenced service view payload and subsequent changes."""

        yield await self.read_view(lease, view)
        try:
            store = self._view_store(view.store_name)
            async with store.watch(lease, view) as changes:
                async for change in changes:
                    await lease.refresh()
                    yield (
                        dict(change.entry.value)
                        if change.entry is not None
                        else None
                    )
        except KvUnavailable as exc:
            raise _service_view_unavailable(view) from exc

    async def aclose(self) -> None:
        for directory in tuple(self._directories.values()):
            await directory.aclose()
        self._directories.clear()
        self._view_stores.clear()

    async def _propose_service_use(
        self,
        descriptor: ServiceDescriptor,
        *,
        operations: Collection[str],
        views: Collection[str] | Mapping[str, Collection[str]],
        timeout_seconds: float | None,
    ) -> ServiceUseLease:
        terms = service_use_terms(
            descriptor,
            client_endpoint=self._endpoint.address,
            operations=operations,
            views=views,
        )
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
                        terms=terms,
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
                        terms=terms,
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
