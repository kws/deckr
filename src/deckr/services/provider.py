from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import anyio

from deckr.beacon import Beacon, BeaconAdvertisementLease, BeaconAdvertisementSpec
from deckr.concord import (
    Concord,
    ConcordParticipant,
    ConcordParticipantCandidate,
    ConcordParticipantPolicy,
    ConcordParticipantSelection,
    ConcordParticipantState,
    ContractHandle,
    ContractPointer,
)
from deckr.contracts.messages import SERVICES_LANE, DeckrMessage
from deckr.lanes import EndpointSession
from deckr.services.client import ManagedServiceContract
from deckr.services.runtime import (
    AuthorizedServiceMessage,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceProtocol,
    ServiceUseAuthorizationError,
    ServiceViewWriteContext,
    authorize_service_message,
    service_view_write_context_from_managed_contract,
)


@dataclass(frozen=True, slots=True)
class ServiceProviderRequest:
    message: DeckrMessage
    authorization: AuthorizedServiceMessage | None = None
    failure: ServiceUseAuthorizationError | None = None

    def __post_init__(self) -> None:
        if (self.authorization is None) == (self.failure is None):
            raise ValueError(
                "provider request requires exactly one authorization outcome"
            )


class ServiceProvider:
    """Provider-side service lifecycle with fenced requests and discovery."""

    def __init__(
        self,
        *,
        endpoint: EndpointSession,
        beacon: Beacon,
        concord: Concord,
        protocol: ServiceProtocol,
        service_id: str,
        backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
        diagnostics: Mapping[str, object] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.protocol = protocol
        self.service_id = service_id
        self._backend_status = ServiceBackendStatus(backend_status)
        self._diagnostics = dict(diagnostics or {})
        self._labels = dict(labels or {})
        self.descriptor = self._build_descriptor()
        if endpoint.address != self.descriptor.endpoint:
            raise ValueError("service id must match the local service endpoint")
        self._beacon = beacon
        self._concord = concord
        self._participant: ConcordParticipant = concord.participant(
            endpoint,
            ConcordParticipantPolicy(
                profile=protocol.use_profile,
                select=self._select_contracts,
            ),
        )
        self._advertisement: BeaconAdvertisementLease | None = None
        self._subscription_cm: AbstractAsyncContextManager[
            anyio.abc.ObjectReceiveStream[DeckrMessage]
        ] | None = None
        self._requests: anyio.abc.ObjectReceiveStream[DeckrMessage] | None = None
        self._entered = False
        self._closed = False
        self._close_started = False
        self._close_lock = anyio.Lock()
        self._close_done = anyio.Event()
        self._enter_cancel_scope: anyio.CancelScope | None = None
        self._enter_task_id: int | None = None
        self._enter_done = anyio.Event()

    @property
    def state(self) -> ConcordParticipantState:
        return self._participant.state

    def watch_state(
        self,
    ) -> AbstractAsyncContextManager[AsyncIterator[ConcordParticipantState]]:
        return self._participant.watch_state()

    async def wait_for_state(
        self,
        predicate: Callable[[ConcordParticipantState], bool],
        *,
        timeout_seconds: float | None = None,
    ) -> ConcordParticipantState:
        return await self._participant.wait_for_state(
            predicate,
            timeout_seconds=timeout_seconds,
        )

    async def __aenter__(self) -> ServiceProvider:
        if self._entered:
            raise RuntimeError("service provider contexts cannot be re-entered")
        self._entered = True
        with anyio.CancelScope() as scope:
            self._enter_cancel_scope = scope
            self._enter_task_id = anyio.get_current_task().id
            try:
                return await self._enter()
            except BaseException:
                if not self._close_started:
                    await self.aclose()
                raise
            finally:
                self._enter_cancel_scope = None
                self._enter_done.set()
        raise anyio.ClosedResourceError

    async def _enter(self) -> ServiceProvider:
        await self._concord._bootstrap_owner(self.endpoint)  # noqa: SLF001
        if self._closed:
            raise anyio.ClosedResourceError
        await self._participant.__aenter__()
        if self._closed:
            raise anyio.ClosedResourceError
        self._subscription_cm = self.endpoint.subscribe(SERVICES_LANE)
        self._requests = await self._subscription_cm.__aenter__()
        if self._closed:
            raise anyio.ClosedResourceError
        payload = self._advertisement_payload()
        self._advertisement = self._beacon.advertise(
            self.endpoint,
            BeaconAdvertisementSpec(
                feature_id=self.protocol.feature_id,
                payload=payload.to_dict(),
                labels=self._labels,
                operations=tuple(self.protocol.operations),
            ),
        )
        await self._advertisement.__aenter__()
        if self._closed:
            raise anyio.ClosedResourceError
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def requests(self) -> AsyncIterator[ServiceProviderRequest]:
        stream = self._requests
        if self._closed or stream is None:
            raise anyio.ClosedResourceError
        async for message in stream:
            try:
                authorization = await authorize_service_message(
                    self._participant,
                    message,
                    service_id=self.descriptor.service_id,
                    protocol=self.protocol,
                )
            except ServiceUseAuthorizationError as exc:
                yield ServiceProviderRequest(message, failure=exc)
            else:
                yield ServiceProviderRequest(message, authorization=authorization)

    async def authorize(
        self,
        message: DeckrMessage,
        *,
        name: str | None = None,
    ) -> AuthorizedServiceMessage:
        if self._closed:
            raise anyio.ClosedResourceError
        return await authorize_service_message(
            self._participant,
            message,
            service_id=self.descriptor.service_id,
            protocol=self.protocol,
            name=name,
        )

    def contract(
        self,
        pointer: ContractHandle | ContractPointer,
    ) -> ManagedServiceContract | None:
        """Return a message/view capability for one currently valid contract."""

        if self._closed:
            return None
        managed = self._participant.managed_contract(pointer)
        if managed is None or not managed.local_authority or not managed.validity.valid:
            return None
        return ManagedServiceContract(
            endpoint=self.endpoint,
            participant=self._participant,
            contract=managed.contract,
            protocol=self.protocol,
            service_id=self.service_id,
        )

    def view_write_context(
        self,
        pointer: ContractHandle | ContractPointer,
    ) -> ServiceViewWriteContext | None:
        """Return a fenced view capability for one currently valid contract."""

        if self._closed:
            return None
        managed = self._participant.managed_contract(pointer)
        if managed is None or not managed.local_authority or not managed.validity.valid:
            return None
        try:
            return service_view_write_context_from_managed_contract(
                managed,
                protocol=self.protocol,
                service_id=self.service_id,
            )
        except ValueError:
            return None

    async def update(
        self,
        *,
        backend_status: ServiceBackendStatus | None = None,
        diagnostics: Mapping[str, object] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Update the provider's discovery facts without changing authority."""

        if self._closed or self._advertisement is None:
            raise anyio.ClosedResourceError
        if backend_status is not None:
            self._backend_status = ServiceBackendStatus(backend_status)
        if diagnostics is not None:
            self._diagnostics = dict(diagnostics)
        if labels is not None:
            self._labels = dict(labels)
        self.descriptor = self._build_descriptor()
        await self._advertisement.update(
            payload=self._advertisement_payload().to_dict(),
            labels=self._labels,
            operations=tuple(self.protocol.operations),
        )

    async def cancel_authorized(
        self,
        authorization: AuthorizedServiceMessage,
        *,
        reason: str,
    ) -> bool:
        """Cancel the exact contract represented by a prior authorization."""

        if self._closed:
            raise anyio.ClosedResourceError
        return await self._participant.cancel(
            authorization.contract,
            reason=reason,
        )

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                if self._close_started:
                    first = False
                else:
                    first = True
                    self._close_started = True
                    self._closed = True
                    enter_scope = self._enter_cancel_scope
                    if enter_scope is not None:
                        enter_scope.cancel()
                    wait_for_enter = (
                        enter_scope is not None
                        and self._enter_task_id != anyio.get_current_task().id
                    )
            if not first:
                await self._close_done.wait()
                return
            try:
                with anyio.move_on_after(5):
                    if wait_for_enter:
                        await self._enter_done.wait()
                    advertisement = self._advertisement
                    subscription = self._subscription_cm
                    self._advertisement = None
                    self._subscription_cm = None
                    self._requests = None
                    async with anyio.create_task_group() as cleanup_group:
                        cleanup_group.start_soon(self._participant.aclose)
                        if advertisement is not None:
                            cleanup_group.start_soon(advertisement.aclose)
                        if subscription is not None:
                            cleanup_group.start_soon(
                                self._close_subscription,
                                subscription,
                            )
            finally:
                self._close_done.set()

    @staticmethod
    async def _close_subscription(
        subscription: AbstractAsyncContextManager[
            anyio.abc.ObjectReceiveStream[DeckrMessage]
        ],
    ) -> None:
        await subscription.__aexit__(None, None, None)

    def _select_contracts(
        self,
        candidates: tuple[ConcordParticipantCandidate, ...],
    ) -> ConcordParticipantSelection:
        return ConcordParticipantSelection(
            candidate.contract.pointer
            for candidate in candidates
            if candidate.record.terms is None
            and self.endpoint.address in candidate.record.participants
            and candidate.record.participant_sessions.get(str(self.endpoint.address))
            == self.endpoint.session_id
        )

    def _advertisement_payload(self):
        return self.protocol.advertisement_payload(
            service_id=self.service_id,
            session_id=self.endpoint.session_id,
            backend_status=self._backend_status,
            diagnostics=self._diagnostics,
        )

    def _build_descriptor(self) -> ServiceDescriptor:
        payload = self._advertisement_payload()
        return ServiceDescriptor(
            candidate=None,
            service_id=payload.service_id,
            namespace=payload.service_namespace,
            endpoint=payload.service_endpoint,
            session_id=payload.session_id,
            advertisement_profile=payload.profile,
            use_profile=payload.service_use_profile,
            supported_operations=frozenset(payload.supported_operations),
            supported_messages=payload.supported_messages,
            views=payload.views,
            backend_status=payload.backend_status,
            diagnostics=payload.diagnostics,
        )


__all__ = ["ServiceProvider", "ServiceProviderRequest"]
