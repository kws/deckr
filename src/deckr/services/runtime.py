"""Beacon/Concord helpers for Deckr service participants."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import anyio
from pydantic import (
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from deckr.beacon import AdvertisementHandle, BeaconAdvertiser, BeaconService, Candidate
from deckr.concord import (
    ConcordAgreement,
    ConcordAgreementSpec,
    ConcordParticipantManager,
    ConcordService,
    ContractHandle,
    ContractState,
    ContractValidityStatus,
    canonical_json_hash,
)
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    entity_subject,
    service_address,
)
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.lanes import RegisteredEndpointLane
from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_COMMAND_REPLY,
    ServiceCommandBody,
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    ServiceError,
    service_body,
)
from deckr.state import StateConflict, StateStore, StateUnavailable

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_CONTRACT_RECONCILE_SECONDS = 1.0
DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS = 5.0
DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS = 5.0
_CLIENT_CONTRACT_WAIT_INTERVAL_SECONDS = 0.05


class ServiceBackendStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ServiceViewFamily(DeckrModel):
    store_name: str = Field(alias="storeName")
    key_prefix: str = Field(alias="keyPrefix")

    @field_validator("store_name", "key_prefix")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service view family field")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


@dataclass(frozen=True, slots=True)
class ServiceViewRef:
    """Explicit reference to a service-owned current-state view."""

    store_name: str
    key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "store_name",
            _require_text(self.store_name, field_name="service view store_name"),
        )
        object.__setattr__(
            self,
            "key",
            _require_text(self.key, field_name="service view key"),
        )


@dataclass(frozen=True, slots=True)
class ServiceProtocol:
    namespace: str
    feature_id: str
    advertisement_profile: str
    use_profile: str
    operations: tuple[str, ...]
    view_families: Mapping[str, ServiceViewFamily]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "namespace",
            _require_text(self.namespace, field_name="service namespace"),
        )
        object.__setattr__(
            self,
            "feature_id",
            _require_text(self.feature_id, field_name="service feature id"),
        )
        object.__setattr__(
            self,
            "advertisement_profile",
            _require_text(
                self.advertisement_profile,
                field_name="service advertisement profile",
            ),
        )
        object.__setattr__(
            self,
            "use_profile",
            _require_text(self.use_profile, field_name="service use profile"),
        )
        operations = tuple(
            _require_text(item, field_name="service operation")
            for item in self.operations
        )
        if not operations:
            raise ValueError("service protocol operations must not be empty")
        object.__setattr__(self, "operations", operations)
        families: dict[str, ServiceViewFamily] = {}
        for name, family in self.view_families.items():
            key = _require_text(name, field_name="service view family name")
            families[key] = (
                family
                if isinstance(family, ServiceViewFamily)
                else ServiceViewFamily.model_validate(family)
            )
        object.__setattr__(self, "view_families", MappingProxyType(families))

    def advertisement_payload(
        self,
        *,
        service_id: str,
        session_id: str,
        backend_status: ServiceBackendStatus | str,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> ServiceAdvertisementPayload:
        return ServiceAdvertisementPayload(
            profile=self.advertisement_profile,
            serviceId=service_id,
            serviceEndpoint=service_address(service_id),
            serviceNamespace=self.namespace,
            sessionId=session_id,
            serviceUseProfile=self.use_profile,
            backendStatus=backend_status,
            supportedOperations=self.operations,
            views=self.view_families,
            diagnostics=dict(diagnostics or {}),
        )


class ServiceAdvertisementPayload(DeckrModel):
    profile: str
    service_id: str = Field(alias="serviceId")
    service_endpoint: EndpointAddress = Field(alias="serviceEndpoint")
    service_namespace: str = Field(alias="serviceNamespace")
    session_id: str = Field(alias="sessionId")
    service_use_profile: str = Field(alias="serviceUseProfile")
    backend_status: ServiceBackendStatus = Field(alias="backendStatus")
    supported_operations: tuple[str, ...] = Field(alias="supportedOperations")
    views: Mapping[str, ServiceViewFamily]
    diagnostics: JsonObject = Field(default_factory=dict)

    @field_validator(
        "profile",
        "service_id",
        "service_namespace",
        "session_id",
        "service_use_profile",
    )
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service advertisement field")

    @field_validator("supported_operations")
    @classmethod
    def _validate_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("service advertisement requires operations")
        return tuple(
            _require_text(item, field_name="service operation") for item in value
        )

    @field_validator("views", mode="after")
    @classmethod
    def _freeze_views(
        cls,
        value: Mapping[str, ServiceViewFamily],
    ) -> Mapping[str, ServiceViewFamily]:
        return freeze_json(value)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _thaw_diagnostics(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("diagnostics", mode="after")
    @classmethod
    def _freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("views")
    def _serialize_views(
        self,
        value: Mapping[str, ServiceViewFamily],
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }

    @field_serializer("diagnostics")
    def _serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_identity(self) -> ServiceAdvertisementPayload:
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("serviceEndpoint must equal service:<serviceId>")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServiceUseTerms(DeckrModel):
    profile: str
    service_use_id: str = Field(alias="serviceUseId")
    service_id: str = Field(alias="serviceId")
    service_endpoint: EndpointAddress = Field(alias="serviceEndpoint")
    service_namespace: str = Field(alias="serviceNamespace")
    service_advertisement_id: str = Field(alias="serviceAdvertisementId")
    service_session_id: str = Field(alias="serviceSessionId")
    client_endpoint: EndpointAddress = Field(alias="clientEndpoint")
    allowed_operations: tuple[str, ...] = Field(alias="allowedOperations")

    @field_validator(
        "profile",
        "service_use_id",
        "service_id",
        "service_namespace",
        "service_advertisement_id",
        "service_session_id",
    )
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service-use terms field")

    @field_validator("allowed_operations")
    @classmethod
    def _validate_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("service-use terms require operations")
        return tuple(
            _require_text(item, field_name="service operation") for item in value
        )

    @model_validator(mode="after")
    def _validate_identity(self) -> ServiceUseTerms:
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("serviceEndpoint must equal service:<serviceId>")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


@dataclass(frozen=True, slots=True)
class ServiceAdvertisement:
    candidate: Candidate
    service_id: str
    service_namespace: str
    service_endpoint: EndpointAddress
    service_session_id: str
    service_use_profile: str
    supported_operations: tuple[str, ...]


@dataclass(slots=True)
class _ServiceLease:
    agreement: ConcordAgreement
    service: ServiceAdvertisement
    terms: ServiceUseTerms

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract


class ServiceClient:
    """Client facade over Beacon service ads and Concord service-use contracts."""

    def __init__(
        self,
        *,
        endpoint: RegisteredEndpointLane,
        beacon: BeaconService,
        concord: ConcordService,
        state_for: Callable[[str], StateStore],
        task_group: anyio.abc.TaskGroup | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._beacon = beacon
        self._concord = concord
        self._state_for = state_for
        self._task_group = task_group
        self._leases: dict[tuple[str, str, str, str], _ServiceLease] = {}
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        for key, lease in list(self._leases.items()):
            self._leases.pop(key, None)
            with anyio.CancelScope(shield=True):
                await self._cancel_lease(
                    lease,
                    reason="service client closed",
                )

    async def command(
        self,
        service_id: str,
        service_namespace: str,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 2.0,
    ) -> ServiceCommandReplyBody:
        if self._closed:
            return _unavailable_reply(
                service_namespace,
                operation,
                code="client_closed",
                message="The services client is closed",
            )
        try:
            lease = await self._ensure_service_lease(
                service_id,
                service_namespace,
                operation=operation,
                timeout=timeout,
            )
        except _UnsupportedOperation:
            return _rejected_reply(
                service_namespace,
                operation,
                code="unsupported_operation",
                message=(
                    f"Service {service_id!r} does not advertise operation "
                    f"{operation!r}"
                ),
            )
        except _ServiceUnavailable as exc:
            return _unavailable_reply(
                service_namespace,
                operation,
                code=exc.code,
                message=exc.message,
                diagnostics=exc.diagnostics,
            )

        body = ServiceCommandBody(
            serviceNamespace=service_namespace,
            operation=operation,
            params=dict(params or {}),
        )
        try:
            reply = await self._endpoint.request(
                recipient=lease.service.service_endpoint,
                recipient_session_id=lease.service.service_session_id,
                subject=entity_subject(
                    "service",
                    serviceId=service_id,
                    namespace=service_namespace,
                    operation=operation,
                ),
                message_type=SERVICE_COMMAND,
                body=body.to_dict(),
                timeout=timeout,
                accept=lambda msg: _accept_reply(
                    msg,
                    service_namespace=service_namespace,
                    operation=operation,
                ),
            )
        except TimeoutError:
            return _unavailable_reply(
                service_namespace,
                operation,
                code="timeout",
                message=f"Service {service_id!r} did not reply in time",
            )
        except StateUnavailable:
            return _unavailable_reply(
                service_namespace,
                operation,
                code="client_endpoint_unavailable",
                message="The services endpoint is unavailable",
            )
        return ServiceCommandReplyBody.model_validate(thaw_json(reply.body))

    async def read_view(
        self,
        service_id: str,
        service_namespace: str,
        view: ServiceViewRef,
    ) -> Mapping[str, Any] | None:
        try:
            lease = await self._ensure_service_lease(
                service_id,
                service_namespace,
                operation=None,
                timeout=0.25,
            )
        except _ServiceUnavailable:
            return None
        store = self._state_for(view.store_name)
        try:
            entry = await store.get(view.key)
        except StateUnavailable:
            return None
        if entry is None:
            return None
        value = thaw_json(entry.value)
        if value.get("serviceId") != service_id:
            return None
        if value.get("serviceNamespace") != service_namespace:
            return None
        if value.get("sessionId") != lease.service.service_session_id:
            return None
        return value

    async def watch_view(
        self,
        service_id: str,
        service_namespace: str,
        view: ServiceViewRef,
    ) -> AsyncIterator[Mapping[str, Any] | None]:
        store = self._state_for(view.store_name)
        yield await self.read_view(service_id, service_namespace, view)
        while True:
            try:
                async with store.watch(view.key) as changes:
                    async for change in changes:
                        if change.key != view.key:
                            continue
                        yield await self.read_view(service_id, service_namespace, view)
            except StateUnavailable:
                yield None
                await anyio.sleep(1.0)

    async def _ensure_service_lease(
        self,
        service_id: str,
        service_namespace: str,
        *,
        operation: str | None,
        timeout: float,
    ) -> _ServiceLease:
        try:
            service = await self._service_advertisement(service_id, service_namespace)
        except _ServiceUnavailable as exc:
            if exc.code == "service_advertisement_missing":
                await self._cancel_service_leases(service_id, service_namespace)
            raise
        if operation is not None and operation not in service.supported_operations:
            raise _UnsupportedOperation
        key = (
            service.service_id,
            service.service_namespace,
            service.candidate.advertisement.advertisement_id,
            service.service_session_id,
        )
        await self._cancel_stale_leases(key, service_id, service_namespace)
        last_error: _ServiceUnavailable | None = None
        for _attempt in range(2):
            lease = self._leases.get(key)
            if lease is None:
                lease = await self._create_or_reuse_lease(service)
                self._leases[key] = lease
            await self._attach_or_refresh_token(lease)
            try:
                return await self._valid_lease(lease, timeout=timeout)
            except _ServiceUnavailable as exc:
                if not exc.code.startswith("contract_"):
                    raise
                status = exc.diagnostics.get("status")
                if status not in {item.value for item in _STALE_SERVICE_USE_STATUSES}:
                    raise
                self._leases.pop(key, None)
                await self._cancel_lease(
                    lease,
                    reason=f"service_use_{status}",
                )
                last_error = exc
        if last_error is not None:
            raise last_error
        raise _ServiceUnavailable(
            "service_contract_unavailable",
            "Service-use contract is not available",
        )

    async def _service_advertisement(
        self,
        service_id: str,
        service_namespace: str,
    ) -> ServiceAdvertisement:
        endpoint = service_address(service_id)
        try:
            candidates = await self._beacon.find(
                service_namespace,
                selector=lambda ad: ad.endpoint == endpoint,
            )
        except StateUnavailable as exc:
            raise _ServiceUnavailable(
                "beacon_unavailable",
                "Service discovery is unavailable",
            ) from exc
        services: list[ServiceAdvertisement] = []
        for candidate in candidates:
            service = service_advertisement_from_candidate(
                candidate,
                service_namespace,
            )
            if service is not None and service.service_id == service_id:
                services.append(service)
        if not services:
            raise _ServiceUnavailable(
                "service_advertisement_missing",
                f"Service {service_id!r} is not advertised",
                {"serviceId": service_id, "serviceNamespace": service_namespace},
            )
        services.sort(key=_service_sort_key, reverse=True)
        return services[0]

    async def _cancel_stale_leases(
        self,
        current_key: tuple[str, str, str, str],
        service_id: str,
        service_namespace: str,
    ) -> None:
        for key, lease in list(self._leases.items()):
            if key == current_key:
                continue
            if key[0] != service_id or key[1] != service_namespace:
                continue
            self._leases.pop(key, None)
            await self._cancel_lease(
                lease,
                reason="service advertisement changed",
            )

    async def _cancel_service_leases(
        self,
        service_id: str,
        service_namespace: str,
    ) -> None:
        for key, lease in list(self._leases.items()):
            if key[0] == service_id and key[1] == service_namespace:
                self._leases.pop(key, None)
                await self._cancel_lease(
                    lease,
                    reason="service advertisement lost",
                )

    async def _cancel_lease(self, lease: _ServiceLease, *, reason: str) -> None:
        try:
            await lease.agreement.cancel(reason=reason)
        except (StateConflict, StateUnavailable):
            logger.debug("Could not cancel service-use contract")

    async def _create_or_reuse_lease(
        self,
        service: ServiceAdvertisement,
    ) -> _ServiceLease:
        terms = service_use_terms(service, self._endpoint.endpoint)
        agreement = await self._concord.ensure_agreement(
            ConcordAgreementSpec(
                profile=service.service_use_profile,
                participants=(service.service_endpoint, self._endpoint.endpoint),
                local_participant=self._endpoint.endpoint,
                local_session_id=self._endpoint.session_id,
                terms=terms.to_dict(),
                stable_contract_id=terms.service_use_id,
                current_sessions={
                    str(self._endpoint.endpoint): self._endpoint.session_id,
                    str(service.service_endpoint): service.service_session_id,
                },
                refresh_interval=DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS,
                log_label="ServiceClient",
            ),
            start_soon=(
                self._task_group.start_soon if self._task_group is not None else None
            ),
        )
        return _ServiceLease(agreement=agreement, service=service, terms=terms)

    async def _attach_or_refresh_token(self, lease: _ServiceLease) -> None:
        try:
            await lease.agreement.refresh()
        except StateConflict:
            logger.debug("Could not attach service-use client token", exc_info=True)

    async def _valid_lease(
        self,
        lease: _ServiceLease,
        *,
        timeout: float,
    ) -> _ServiceLease:
        deadline = anyio.current_time() + max(timeout, 0)
        while True:
            validity = await lease.agreement.refresh()
            if validity.valid:
                return lease
            if validity.status != ContractValidityStatus.NOT_YET_FULFILLED:
                raise _ServiceUnavailable(
                    f"contract_{validity.status.value}",
                    "Service-use contract is not valid",
                    {"status": validity.status.value, "reason": validity.reason},
                )
            if anyio.current_time() >= deadline:
                raise _ServiceUnavailable(
                    "service_contract_pending",
                    "Service-use contract is pending the service token",
                    {"status": validity.status.value},
                )
            await anyio.sleep(_CLIENT_CONTRACT_WAIT_INTERVAL_SECONDS)


class GenericService:
    """Reusable Beacon/Concord host for a concrete service component."""

    def __init__(
        self,
        *,
        protocol: ServiceProtocol,
        service_id: str,
        endpoint: RegisteredEndpointLane,
        beacon: BeaconService | None,
        concord: ConcordService | None,
        view_state: StateStore | None,
        log_label: str = "service",
        reconcile_interval: float = DEFAULT_SERVICE_CONTRACT_RECONCILE_SECONDS,
        advertisement_refresh_interval: float = (
            DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS
        ),
        refresh_interval: float = DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS,
    ) -> None:
        self.protocol = protocol
        self.service_id = _require_text(service_id, field_name="service id")
        self.endpoint = endpoint
        self._beacon = beacon
        self._concord = concord
        self._view_state = view_state
        self._log_label = log_label
        self._reconcile_interval = reconcile_interval
        self._advertisement_refresh_interval = advertisement_refresh_interval
        self._refresh_interval = refresh_interval
        self._advertisement: AdvertisementHandle | None = None
        self._advertiser: BeaconAdvertiser | None = None
        self._backend_status = ServiceBackendStatus.UNAVAILABLE
        self._backend_diagnostics: Mapping[str, Any] = {}
        self._view_revisions: dict[str, int] = {}
        self._service_contract_manager: ConcordParticipantManager | None = None
        if concord is not None:
            self._service_contract_manager = concord.participant_manager(
                participant=endpoint.endpoint,
                session_id=endpoint.session_id,
                profile=protocol.use_profile,
                refresh_interval=refresh_interval,
                reconcile_interval=reconcile_interval,
                cancel_terminal_statuses=_STALE_SERVICE_USE_STATUSES,
                log_label=log_label,
                accept_contract=self._accept_service_use_contract,
                current_sessions=self._service_use_current_sessions,
            )
        self._contracts_lock = anyio.Lock()
        self._task_group: anyio.abc.TaskGroup | None = None

    @property
    def advertisement(self) -> AdvertisementHandle | None:
        return self._advertisement

    @property
    def backend_status(self) -> ServiceBackendStatus:
        return self._backend_status

    @property
    def backend_diagnostics(self) -> Mapping[str, Any]:
        return self._backend_diagnostics

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        task_group.start_soon(self.contract_watch_loop)
        task_group.start_soon(self.contract_reconcile_loop)
        if self._advertiser is not None:
            self._advertiser.start(task_group)
        if self._service_contract_manager is not None:
            self._service_contract_manager.start(task_group)

    async def publish_status(
        self,
        status: ServiceBackendStatus | str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self._backend_status = ServiceBackendStatus(status)
        self._backend_diagnostics = freeze_json(dict(diagnostics or {}))
        if self._beacon is None:
            return
        payload = self.protocol.advertisement_payload(
            service_id=self.service_id,
            session_id=self.endpoint.session_id,
            backend_status=self._backend_status,
            diagnostics=self._backend_diagnostics,
        )
        try:
            if self._advertiser is None:
                self._advertiser = self._beacon.advertiser(
                    feature_id=self.protocol.feature_id,
                    endpoint=self.endpoint.endpoint,
                    session_id=self.endpoint.session_id,
                    payload=payload.to_dict(),
                    operations=self.protocol.operations,
                    refresh_interval=self._advertisement_refresh_interval,
                    log_label=self._log_label,
                )
                if self._task_group is not None:
                    self._advertiser.start(self._task_group)
            self._advertisement = await self._advertiser.publish(
                payload=payload.to_dict(),
                operations=self.protocol.operations,
            )
        except StateConflict:
            self._advertisement = None
            self._advertiser = None
            try:
                self._advertiser = self._beacon.advertiser(
                    feature_id=self.protocol.feature_id,
                    endpoint=self.endpoint.endpoint,
                    session_id=self.endpoint.session_id,
                    payload=payload.to_dict(),
                    operations=self.protocol.operations,
                    refresh_interval=self._advertisement_refresh_interval,
                    log_label=self._log_label,
                )
                if self._task_group is not None:
                    self._advertiser.start(self._task_group)
                self._advertisement = await self._advertiser.publish(
                    payload=payload.to_dict(),
                    operations=self.protocol.operations,
                )
            except (StateConflict, StateUnavailable):
                logger.warning(
                    "Could not publish %s Beacon advertisement",
                    self._log_label,
                    exc_info=True,
                )
        except StateUnavailable:
            logger.warning(
                "Could not publish %s Beacon advertisement",
                self._log_label,
                exc_info=True,
            )
            self._advertisement = None

    async def put_view(self, key: str, payload: Mapping[str, Any]) -> None:
        if self._view_state is None:
            return
        fenced_payload = {
            **payload,
            "serviceId": self.service_id,
            "serviceNamespace": self.protocol.namespace,
            "sessionId": self.endpoint.session_id,
        }
        entry = await self._view_state.put(key, fenced_payload)
        self._view_revisions[key] = entry.revision

    async def withdraw(self) -> None:
        if self._view_state is not None:
            for key, revision in list(self._view_revisions.items()):
                await _delete_if_current(self._view_state, key, revision)
        self._view_revisions.clear()
        if self._service_contract_manager is not None:
            await self._service_contract_manager.aclose()
        if self._advertisement is not None and self._beacon is not None:
            try:
                if self._advertiser is not None:
                    await self._advertiser.withdraw()
                else:
                    await self._beacon.withdraw(
                        self._advertisement,
                        log_label=self._log_label,
                    )
            except (StateConflict, StateUnavailable):
                logger.debug("Could not withdraw %s Beacon advertisement", self._log_label)
        self._advertisement = None
        self._advertiser = None
        self._task_group = None

    async def advertisement_refresh_loop(self) -> None:
        if self._beacon is None:
            return
        while True:
            await anyio.sleep(self._advertisement_refresh_interval)
            await self.publish_status(
                self._backend_status,
                diagnostics=self._backend_diagnostics,
            )

    async def contract_watch_loop(self) -> None:
        if self._concord is None:
            return
        while True:
            try:
                async with self._concord.watch_contracts(
                    self.protocol.use_profile
                ) as stream:
                    async for _change in stream:
                        await self.reconcile_contracts()
            except StateUnavailable:
                await anyio.sleep(self._reconcile_interval)

    async def contract_reconcile_loop(self) -> None:
        if self._concord is None:
            return
        while True:
            try:
                await self.reconcile_contracts()
            except StateUnavailable:
                logger.warning(
                    "Service contracts unavailable; reconciliation will retry "
                    "service=%s namespace=%s",
                    self.service_id,
                    self.protocol.namespace,
                    exc_info=True,
                )
            await anyio.sleep(self._reconcile_interval)

    async def reconcile_contracts(self) -> None:
        manager = self._service_contract_manager
        if manager is None:
            return
        async with self._contracts_lock:
            await manager.reconcile(reason=f"{self._log_label} service reconcile")

    async def matching_terms(
        self,
        contract: ContractHandle,
    ) -> ServiceUseTerms | None:
        manager = self._service_contract_manager
        if manager is None:
            return None
        managed = manager.managed_contract(contract)
        if managed is None:
            return None
        return self._matching_terms_record(managed.record)

    async def _accept_service_use_contract(
        self,
        contract: ContractHandle,
        record: Any,
    ) -> bool:
        return (
            self.endpoint.endpoint in contract.participants
            and self._matching_terms_record(record) is not None
        )

    def _service_use_current_sessions(
        self,
        _contract: ContractHandle,
    ) -> Mapping[str, str]:
        return {str(self.endpoint.endpoint): self.endpoint.session_id}

    def _matching_terms_record(self, record: Any) -> ServiceUseTerms | None:
        if self._advertisement is None:
            return None
        if record.state != ContractState.OPEN:
            return None
        try:
            terms = ServiceUseTerms.model_validate(thaw_json(record.terms or {}))
        except ValueError:
            return None
        if terms.profile != self.protocol.use_profile:
            return None
        if terms.service_namespace != self.protocol.namespace:
            return None
        if terms.service_id != self.service_id:
            return None
        if terms.service_endpoint != self.endpoint.endpoint:
            return None
        if terms.service_session_id != self.endpoint.session_id:
            return None
        if terms.service_advertisement_id != self._advertisement.advertisement_id:
            return None
        return terms

    async def command_authorized(
        self,
        message: DeckrMessage,
        body: ServiceCommandBody,
    ) -> bool:
        if body.service_namespace != self.protocol.namespace:
            return True
        manager = self._service_contract_manager
        if manager is None:
            return False
        await self.reconcile_contracts()
        for managed in manager.managed_contracts:
            terms = self._matching_terms_record(managed.record)
            if terms is None:
                continue
            if terms.client_endpoint != message.sender:
                continue
            if body.operation not in terms.allowed_operations:
                continue
            validity = await manager.validate(
                managed.contract,
                current_sessions={
                    str(terms.service_endpoint): self.endpoint.session_id,
                    str(terms.client_endpoint): message.sender_session_id,
                },
            )
            if validity.status == ContractValidityStatus.VALID:
                return True
        return False


class _ServiceUnavailable(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = dict(diagnostics or {})


class _UnsupportedOperation(Exception):
    pass


def service_view_key(service_id: str, family: str, *tokens: str) -> str:
    parts = ["views", encode_key_token(service_id), encode_key_token(family)]
    parts.extend(encode_key_token(token) for token in tokens)
    return ".".join(parts)


def service_view_prefix(service_id: str, family: str) -> str:
    return service_view_key(service_id, family) + "."


def service_advertisement_from_candidate(
    candidate: Candidate,
    service_namespace: str,
) -> ServiceAdvertisement | None:
    advertisement = candidate.advertisement
    if advertisement.feature_id != service_namespace or advertisement.payload is None:
        return None
    try:
        payload = ServiceAdvertisementPayload.model_validate(
            thaw_json(advertisement.payload)
        )
    except (TypeError, ValueError):
        return None
    if payload.service_namespace != service_namespace:
        return None
    if payload.service_endpoint != advertisement.endpoint:
        return None
    if payload.session_id != advertisement.session_id:
        return None
    return ServiceAdvertisement(
        candidate=candidate,
        service_id=payload.service_id,
        service_namespace=payload.service_namespace,
        service_endpoint=payload.service_endpoint,
        service_session_id=payload.session_id,
        service_use_profile=payload.service_use_profile,
        supported_operations=payload.supported_operations,
    )


def service_use_terms(
    service: ServiceAdvertisement,
    client_endpoint: EndpointAddress,
) -> ServiceUseTerms:
    operations = sorted(service.supported_operations)
    identity = {
        "profile": service.service_use_profile,
        "serviceId": service.service_id,
        "serviceEndpoint": str(service.service_endpoint),
        "serviceNamespace": service.service_namespace,
        "serviceAdvertisementId": service.candidate.advertisement.advertisement_id,
        "serviceSessionId": service.service_session_id,
        "clientEndpoint": str(client_endpoint),
        "allowedOperations": operations,
    }
    digest = canonical_json_hash(identity).removeprefix("sha256:")[:32]
    return ServiceUseTerms.model_validate(
        {
            **identity,
            "serviceUseId": f"service-use:{digest}",
        }
    )


async def _delete_if_current(state: StateStore, key: str, revision: int) -> None:
    try:
        await state.delete(key, revision=revision)
    except StateConflict:
        logger.debug("Service view %s changed before cleanup", key)
    except StateUnavailable:
        logger.debug("Service view %s unavailable before cleanup", key, exc_info=True)


_STALE_SERVICE_USE_STATUSES = frozenset(
    {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }
)


def _stale_service_use_contract(status: ContractValidityStatus) -> bool:
    return status in _STALE_SERVICE_USE_STATUSES


def _service_sort_key(service: ServiceAdvertisement) -> tuple[datetime, int, str]:
    advertisement = service.candidate.advertisement
    timestamp = (
        advertisement.updated_at
        or advertisement.created_at
        or datetime.min.replace(tzinfo=UTC)
    )
    return (timestamp, advertisement.refresh_seq, service.candidate.key)


def _accept_reply(
    msg: DeckrMessage,
    *,
    service_namespace: str,
    operation: str,
) -> bool:
    if msg.message_type != SERVICE_COMMAND_REPLY:
        return False
    try:
        body = service_body(msg)
    except (TypeError, ValueError, ValidationError):
        return False
    return (
        isinstance(body, ServiceCommandReplyBody)
        and body.service_namespace == service_namespace
        and body.operation == operation
    )


def _rejected_reply(
    service_namespace: str,
    operation: str,
    *,
    code: str,
    message: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> ServiceCommandReplyBody:
    return ServiceCommandReplyBody(
        serviceNamespace=service_namespace,
        operation=operation,
        status=ServiceCommandStatus.REJECTED,
        error=ServiceError(
            code=code,
            message=message,
            diagnostics=dict(diagnostics or {}),
        ),
    )


def _unavailable_reply(
    service_namespace: str,
    operation: str,
    *,
    code: str,
    message: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> ServiceCommandReplyBody:
    return ServiceCommandReplyBody(
        serviceNamespace=service_namespace,
        operation=operation,
        status=ServiceCommandStatus.UNAVAILABLE,
        error=ServiceError(
            code=code,
            message=message,
            diagnostics=dict(diagnostics or {}),
        ),
    )


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
