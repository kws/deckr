"""Beacon/Concord helpers for Deckr service participants."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping
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

from deckr.beacon import (
    AdvertisementHandle,
    BeaconAdvertisement,
    BeaconAdvertisementSpec,
    BeaconService,
    Candidate,
)
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    ConcordAgreement,
    ConcordAgreementSpec,
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
    EndpointTarget,
    endpoint_target,
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
from deckr.state import (
    DEFAULT_STATE_RECONCILE_SECONDS,
    StateConflict,
    StateStore,
    StateUnavailable,
)

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_CONTRACT_RECONCILE_SECONDS = DEFAULT_STATE_RECONCILE_SECONDS
DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS = 5.0
DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
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
    service_session_id: str = Field(alias="serviceSessionId")
    client_endpoint: EndpointAddress = Field(alias="clientEndpoint")
    allowed_operations: tuple[str, ...] = Field(default=(), alias="allowedOperations")
    allowed_views: Mapping[str, tuple[str, ...]] = Field(
        default_factory=dict,
        alias="allowedViews",
    )

    @field_validator(
        "profile",
        "service_use_id",
        "service_id",
        "service_namespace",
        "service_session_id",
    )
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service-use terms field")

    @field_validator("allowed_operations")
    @classmethod
    def _validate_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _require_text(item, field_name="service operation") for item in value
        )

    @field_validator("allowed_views", mode="after")
    @classmethod
    def _freeze_allowed_views(
        cls,
        value: Mapping[str, tuple[str, ...]],
    ) -> Mapping[str, tuple[str, ...]]:
        views = {
            _require_text(family, field_name="service view family"): tuple(
                _require_text(prefix, field_name="service view prefix")
                for prefix in prefixes
            )
            for family, prefixes in value.items()
        }
        return MappingProxyType(views)

    @field_serializer("allowed_views")
    def _serialize_allowed_views(
        self,
        value: Mapping[str, tuple[str, ...]],
    ) -> dict[str, list[str]]:
        return {family: list(prefixes) for family, prefixes in value.items()}

    @model_validator(mode="after")
    def _validate_identity(self) -> ServiceUseTerms:
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("serviceEndpoint must equal service:<serviceId>")
        if not self.allowed_operations and not any(self.allowed_views.values()):
            raise ValueError("service-use terms require operations or views")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    """Profile-validated service fact parsed from a Beacon candidate."""

    candidate: Candidate
    service_id: str
    namespace: str
    endpoint: EndpointAddress
    session_id: str
    advertisement_profile: str
    use_profile: str
    supported_operations: frozenset[str]
    views: Mapping[str, ServiceViewFamily]
    backend_status: ServiceBackendStatus
    diagnostics: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported_operations",
            frozenset(
                _require_text(item, field_name="service operation")
                for item in self.supported_operations
            ),
        )
        object.__setattr__(self, "views", MappingProxyType(dict(self.views)))
        object.__setattr__(self, "diagnostics", freeze_json(dict(self.diagnostics)))


@dataclass(frozen=True, slots=True)
class ServiceUseRequest:
    descriptor: ServiceDescriptor
    client_endpoint: EndpointAddress
    operations: frozenset[str]
    views: Mapping[str, tuple[str, ...]]


@dataclass(slots=True)
class ServiceUseLease:
    agreement: ConcordAgreement
    descriptor: ServiceDescriptor
    terms: ServiceUseTerms

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract

    async def refresh(self) -> None:
        try:
            validity = await self.agreement.refresh()
        except StateConflict as exc:
            raise _service_use_conflict_unavailable(exc) from exc
        if validity.valid:
            return
        raise ServiceUnavailable(
            f"contract_{validity.status.value}",
            "Service-use contract is not valid",
            {"status": validity.status.value, "reason": validity.reason},
        )


class AuthorizationDecision(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    NOT_APPLICABLE = "not_applicable"


class ServiceUnavailable(Exception):
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


class UnsupportedServiceScope(ValueError):
    pass


class ServiceAdvertiser:
    """Service-side Beacon helper for one service profile."""

    def __init__(
        self,
        *,
        protocol: ServiceProtocol,
        service_id: str,
        endpoint: RegisteredEndpointLane,
        beacon: BeaconService,
        log_label: str = "service",
        refresh_interval: float = DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS,
    ) -> None:
        self.protocol = protocol
        self.service_id = _require_text(service_id, field_name="service id")
        self.endpoint = endpoint
        self._beacon = beacon
        self._log_label = log_label
        self._refresh_interval = refresh_interval
        self._advertisement: AdvertisementHandle | None = None
        self._advertiser: BeaconAdvertisement | None = None
        self._backend_status = ServiceBackendStatus.UNAVAILABLE
        self._backend_diagnostics: Mapping[str, Any] = {}
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
        if self._advertiser is not None:
            self._advertiser.start(task_group)

    async def publish(
        self,
        status: ServiceBackendStatus | str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self._backend_status = ServiceBackendStatus(status)
        self._backend_diagnostics = freeze_json(dict(diagnostics or {}))
        payload = self.protocol.advertisement_payload(
            service_id=self.service_id,
            session_id=self.endpoint.session_id,
            backend_status=self._backend_status,
            diagnostics=self._backend_diagnostics,
        )
        payload_dict = payload.to_dict()
        try:
            if self._advertiser is None or self._advertiser.closed:
                self._advertiser = await self._beacon.ensure_advertisement(
                    BeaconAdvertisementSpec(
                        feature_id=self.protocol.feature_id,
                        endpoint=self.endpoint.endpoint,
                        session_id=self.endpoint.session_id,
                        payload=payload_dict,
                        operations=self.protocol.operations,
                        refresh_interval=self._refresh_interval,
                        log_label=self._log_label,
                    ),
                    start_soon=(
                        self._task_group.start_soon
                        if self._task_group is not None
                        else None
                    ),
                )
            self._advertisement = await self._advertiser.publish(payload=payload_dict)
        except StateUnavailable:
            logger.warning(
                "Could not publish %s Beacon advertisement",
                self._log_label,
                exc_info=True,
            )
            self._advertisement = None

    async def withdraw(self) -> None:
        if self._advertiser is not None:
            try:
                await self._advertiser.aclose()
            except (StateConflict, StateUnavailable):
                logger.debug("Could not withdraw %s Beacon advertisement", self._log_label)
            self._advertiser = None
        self._advertisement = None
        self._task_group = None


class ServiceUseLeaseManager:
    """Client-side Concord helper for an explicitly selected service descriptor."""

    def __init__(
        self,
        *,
        endpoint: RegisteredEndpointLane,
        concord: ConcordService,
        task_group: anyio.abc.TaskGroup | None = None,
        log_label: str = "ServiceUseLeaseManager",
        refresh_interval: float = DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS,
    ) -> None:
        self._endpoint = endpoint
        self._concord = concord
        self._task_group = task_group
        self._log_label = log_label
        self._refresh_interval = refresh_interval
        self._leases: dict[tuple[str, ...], ServiceUseLease] = {}
        self._lease_lock = anyio.Lock()
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        async with self._lease_lock:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            await self._cancel_lease(lease, reason="service lease manager closed")

    async def ensure(
        self,
        descriptor: ServiceDescriptor,
        *,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout: float = 2.0,
    ) -> ServiceUseLease:
        async with self._lease_lock:
            return await self._ensure_locked(
                descriptor,
                operations=operations,
                views=views,
                timeout=timeout,
            )

    async def _ensure_locked(
        self,
        descriptor: ServiceDescriptor,
        *,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout: float = 2.0,
    ) -> ServiceUseLease:
        if self._closed:
            raise ServiceUnavailable(
                "client_closed",
                "The service lease manager is closed",
            )
        terms = service_use_terms(
            descriptor,
            self._endpoint.endpoint,
            operations=operations,
            views=views,
        )
        key = self._lease_key(descriptor, terms)
        lease = self._leases.get(key)
        if lease is None or lease.terms != terms:
            if lease is not None:
                await self._cancel_lease(lease, reason="service terms changed")
            lease = await self._create_or_reuse_lease(descriptor, terms)
            self._leases[key] = lease
        try:
            return await self._valid_lease(lease, timeout=timeout)
        except ServiceUnavailable as exc:
            if _pending_service_use_error(exc):
                await self._drop_lease(lease, reason="service_use_acceptance_timeout")
                raise
            if not _stale_service_use_error(exc):
                raise
            await self._drop_lease(
                lease,
                reason=f"service_use_{exc.diagnostics.get('status')}",
            )
            replacement = await self._create_or_reuse_lease(descriptor, terms)
            self._leases[key] = replacement
            return await self._valid_lease(replacement, timeout=timeout)

    async def cached(
        self,
        *,
        service_id: str,
        namespace: str,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout: float = 0.0,
    ) -> ServiceUseLease | None:
        """Return a valid cached lease that already covers the requested scope."""

        async with self._lease_lock:
            return await self._cached_locked(
                service_id=service_id,
                namespace=namespace,
                operations=operations,
                views=views,
                timeout=timeout,
            )

    async def _cached_locked(
        self,
        *,
        service_id: str,
        namespace: str,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout: float = 0.0,
    ) -> ServiceUseLease | None:
        if self._closed:
            raise ServiceUnavailable(
                "client_closed",
                "The service lease manager is closed",
            )
        service_id = _require_text(service_id, field_name="service id")
        namespace = _require_text(namespace, field_name="service namespace")
        leases = sorted(
            (
                lease
                for lease in self._leases.values()
                if lease.descriptor.service_id == service_id
                and lease.descriptor.namespace == namespace
                and _lease_covers_scope(lease, operations=operations, views=views)
            ),
            key=lambda lease: service_descriptor_sort_key(lease.descriptor),
            reverse=True,
        )
        for lease in leases:
            try:
                return await self._valid_lease(lease, timeout=timeout)
            except ServiceUnavailable as exc:
                if _pending_service_use_error(exc) or _stale_service_use_error(exc):
                    await self._drop_lease(
                        lease,
                        reason=f"cached_service_use_{exc.code}",
                    )
                    continue
                raise
        return None

    def _lease_key(
        self,
        descriptor: ServiceDescriptor,
        terms: ServiceUseTerms,
    ) -> tuple[str, ...]:
        return (
            str(self._endpoint.endpoint),
            self._endpoint.session_id,
            descriptor.service_id,
            descriptor.namespace,
            str(descriptor.endpoint),
            descriptor.session_id,
            descriptor.use_profile,
            terms.service_use_id,
        )

    async def _create_or_reuse_lease(
        self,
        descriptor: ServiceDescriptor,
        terms: ServiceUseTerms,
    ) -> ServiceUseLease:
        agreement = await self._concord.ensure_agreement(
            ConcordAgreementSpec(
                profile=descriptor.use_profile,
                participants=(descriptor.endpoint, self._endpoint.endpoint),
                local_participant=self._endpoint.endpoint,
                local_session_id=self._endpoint.session_id,
                terms=terms.to_dict(),
                stable_contract_id=terms.service_use_id,
                current_sessions={
                    str(self._endpoint.endpoint): self._endpoint.session_id,
                    str(descriptor.endpoint): descriptor.session_id,
                },
                refresh_interval=self._refresh_interval,
                log_label=self._log_label,
            ),
            start_soon=(
                self._task_group.start_soon if self._task_group is not None else None
            ),
        )
        return ServiceUseLease(agreement=agreement, descriptor=descriptor, terms=terms)

    async def _valid_lease(
        self,
        lease: ServiceUseLease,
        *,
        timeout: float,
    ) -> ServiceUseLease:
        deadline = anyio.current_time() + max(timeout, 0)
        while True:
            try:
                validity = await lease.agreement.refresh()
            except StateConflict as exc:
                raise _service_use_conflict_unavailable(exc) from exc
            if validity.valid:
                return lease
            if validity.status != ContractValidityStatus.NOT_YET_FULFILLED:
                raise ServiceUnavailable(
                    f"contract_{validity.status.value}",
                    "Service-use contract is not valid",
                    {"status": validity.status.value, "reason": validity.reason},
                )
            if anyio.current_time() >= deadline:
                raise ServiceUnavailable(
                    "service_contract_pending",
                    "Service-use contract is pending the service token",
                    {"status": validity.status.value},
                )
            await anyio.sleep(_CLIENT_CONTRACT_WAIT_INTERVAL_SECONDS)

    async def _cancel_lease(self, lease: ServiceUseLease, *, reason: str) -> None:
        try:
            await lease.agreement.cancel(reason=reason)
        except (StateConflict, StateUnavailable):
            logger.debug("Could not cancel service-use contract")

    async def _drop_lease(self, lease: ServiceUseLease, *, reason: str) -> None:
        for key, current in list(self._leases.items()):
            if current is lease:
                self._leases.pop(key, None)
        await self._cancel_lease(lease, reason=reason)


class ServiceCommandChannel:
    """Thin services-lane request/reply helper."""

    def __init__(self, *, endpoint: RegisteredEndpointLane) -> None:
        self._endpoint = endpoint

    async def command(
        self,
        lease: ServiceUseLease,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 2.0,
    ) -> ServiceCommandReplyBody:
        operation = _require_text(operation, field_name="service operation")
        descriptor = lease.descriptor
        if operation not in lease.terms.allowed_operations:
            return _rejected_reply(
                descriptor.namespace,
                operation,
                code="operation_not_authorized",
                message=(
                    f"Service-use lease does not authorize operation {operation!r}"
                ),
            )
        try:
            await lease.refresh()
        except ServiceUnavailable as exc:
            return _unavailable_reply(
                descriptor.namespace,
                operation,
                code=exc.code,
                message=exc.message,
                diagnostics=exc.diagnostics,
            )
        body = ServiceCommandBody(
            serviceNamespace=descriptor.namespace,
            operation=operation,
            params=dict(params or {}),
        )
        try:
            reply = await self._endpoint.request(
                recipient=descriptor.endpoint,
                recipient_session_id=descriptor.session_id,
                subject=entity_subject(
                    "service",
                    serviceId=descriptor.service_id,
                    namespace=descriptor.namespace,
                    operation=operation,
                ),
                message_type=SERVICE_COMMAND,
                body=body.to_dict(),
                timeout=timeout,
                accept=lambda msg: _accept_reply(
                    msg,
                    service_namespace=descriptor.namespace,
                    operation=operation,
                ),
            )
        except TimeoutError:
            return _unavailable_reply(
                descriptor.namespace,
                operation,
                code="timeout",
                message=f"Service {descriptor.service_id!r} did not reply in time",
            )
        except StateUnavailable:
            return _unavailable_reply(
                descriptor.namespace,
                operation,
                code="client_endpoint_unavailable",
                message="The services endpoint is unavailable",
            )
        return ServiceCommandReplyBody.model_validate(thaw_json(reply.body))


class ServiceViewReader:
    """Fenced current-state reader for service views scoped by a lease."""

    def __init__(self, *, state_for: Callable[[str], StateStore]) -> None:
        self._state_for = state_for

    async def read(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> Mapping[str, Any] | None:
        if not _view_ref_authorized(lease, view):
            raise UnsupportedServiceScope(
                f"Service-use lease does not authorize view {view.key!r}"
            )
        try:
            await lease.refresh()
        except ServiceUnavailable:
            return None
        store = self._state_for(view.store_name)
        try:
            entry = await store.get(view.key)
        except StateUnavailable:
            return None
        if entry is None:
            return None
        value = thaw_json(entry.value)
        descriptor = lease.descriptor
        if value.get("serviceId") != descriptor.service_id:
            return None
        if value.get("serviceNamespace") != descriptor.namespace:
            return None
        if value.get("sessionId") != descriptor.session_id:
            return None
        return value

    async def watch(
        self,
        lease: ServiceUseLease,
        view: ServiceViewRef,
    ) -> AsyncIterator[Mapping[str, Any] | None]:
        if not _view_ref_authorized(lease, view):
            raise UnsupportedServiceScope(
                f"Service-use lease does not authorize view {view.key!r}"
            )
        store = self._state_for(view.store_name)
        yield await self.read(lease, view)
        while True:
            try:
                async with store.watch(view.key) as changes:
                    async for change in changes:
                        if change.key != view.key:
                            continue
                        yield await self.read(lease, view)
            except StateUnavailable:
                yield None
                await anyio.sleep(1.0)


class ServiceViewWriter:
    """Service-side fenced current-state writer."""

    def __init__(
        self,
        *,
        protocol: ServiceProtocol,
        service_id: str,
        endpoint: RegisteredEndpointLane,
        state: StateStore,
    ) -> None:
        self.protocol = protocol
        self.service_id = _require_text(service_id, field_name="service id")
        self.endpoint = endpoint
        self._state = state
        self._revisions: dict[str, int] = {}

    async def put(self, key: str, payload: Mapping[str, Any]) -> None:
        fenced_payload = {
            **payload,
            "serviceId": self.service_id,
            "serviceNamespace": self.protocol.namespace,
            "sessionId": self.endpoint.session_id,
        }
        entry = await self._state.put(key, fenced_payload)
        self._revisions[key] = entry.revision

    async def withdraw(self) -> None:
        for key, revision in list(self._revisions.items()):
            await _delete_if_current(self._state, key, revision)
        self._revisions.clear()


class ServiceUseAuthorizer:
    """Service-side Concord authorization helper for service commands."""

    def __init__(
        self,
        *,
        protocol: ServiceProtocol,
        service_id: str,
        endpoint: RegisteredEndpointLane,
        concord: ConcordService,
        log_label: str = "service",
        reconcile_interval: float = DEFAULT_SERVICE_CONTRACT_RECONCILE_SECONDS,
        refresh_interval: float = DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS,
    ) -> None:
        self.protocol = protocol
        self.service_id = _require_text(service_id, field_name="service id")
        self.endpoint = endpoint
        self._log_label = log_label
        self._reconcile_interval = reconcile_interval
        self._manager = concord.participant_manager(
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

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._manager.start(task_group)

    async def aclose(self) -> None:
        await self._manager.aclose()

    async def reconcile_contracts(self) -> None:
        async with self._contracts_lock:
            await self._manager.reconcile(reason=f"{self._log_label} service reconcile")

    async def matching_terms(
        self,
        contract: ContractHandle,
    ) -> ServiceUseTerms | None:
        managed = self._manager.managed_contract(contract)
        if managed is None:
            return None
        return self._matching_terms_record(managed.record)

    async def authorize_command(
        self,
        message: DeckrMessage,
        body: ServiceCommandBody,
    ) -> AuthorizationDecision:
        if not _command_applies_to_service(
            message,
            body,
            service_id=self.service_id,
            namespace=self.protocol.namespace,
            endpoint=self.endpoint.endpoint,
        ):
            return AuthorizationDecision.NOT_APPLICABLE
        await self.reconcile_contracts()
        for managed in self._manager.managed_contracts:
            terms = self._matching_terms_record(managed.record)
            if terms is None:
                continue
            if terms.client_endpoint != message.sender:
                continue
            if body.operation not in terms.allowed_operations:
                continue
            validity = await self._manager.validate(
                managed.contract,
                current_sessions={
                    str(terms.service_endpoint): self.endpoint.session_id,
                    str(terms.client_endpoint): message.sender_session_id,
                },
            )
            if validity.status == ContractValidityStatus.VALID:
                return AuthorizationDecision.AUTHORIZED
        return AuthorizationDecision.DENIED

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
        return terms


def service_view_key(service_id: str, family: str, *tokens: str) -> str:
    parts = ["views", encode_key_token(service_id), encode_key_token(family)]
    parts.extend(encode_key_token(token) for token in tokens)
    return ".".join(parts)


def service_view_prefix(service_id: str, family: str) -> str:
    return service_view_key(service_id, family) + "."


def parse_service_descriptor(
    candidate: Candidate,
    protocol: ServiceProtocol,
) -> ServiceDescriptor | None:
    advertisement = candidate.advertisement
    if advertisement.feature_id != protocol.feature_id or advertisement.payload is None:
        return None
    try:
        payload = ServiceAdvertisementPayload.model_validate(
            thaw_json(advertisement.payload)
        )
    except (TypeError, ValueError):
        return None
    if payload.profile != protocol.advertisement_profile:
        return None
    if payload.service_namespace != protocol.namespace:
        return None
    if payload.service_use_profile != protocol.use_profile:
        return None
    if payload.service_endpoint != advertisement.endpoint:
        return None
    if payload.session_id != advertisement.session_id:
        return None
    if not set(payload.supported_operations).issubset(set(protocol.operations)):
        return None
    if {
        key: family.to_dict() for key, family in payload.views.items()
    } != {key: family.to_dict() for key, family in protocol.view_families.items()}:
        return None
    return ServiceDescriptor(
        candidate=candidate,
        service_id=payload.service_id,
        namespace=payload.service_namespace,
        endpoint=payload.service_endpoint,
        session_id=payload.session_id,
        advertisement_profile=payload.profile,
        use_profile=payload.service_use_profile,
        supported_operations=frozenset(payload.supported_operations),
        views=payload.views,
        backend_status=payload.backend_status,
        diagnostics=payload.diagnostics,
    )


def service_use_terms(
    descriptor: ServiceDescriptor,
    client_endpoint: EndpointAddress,
    *,
    operations: Collection[str] = (),
    views: Collection[str] | Mapping[str, Collection[str]] = (),
) -> ServiceUseTerms:
    allowed_operations = sorted(_normalize_operations(descriptor, operations))
    allowed_views = _normalize_view_scope(descriptor, views)
    identity = {
        "profile": descriptor.use_profile,
        "serviceId": descriptor.service_id,
        "serviceEndpoint": str(descriptor.endpoint),
        "serviceNamespace": descriptor.namespace,
        "serviceSessionId": descriptor.session_id,
        "clientEndpoint": str(client_endpoint),
        "allowedOperations": allowed_operations,
        "allowedViews": {
            family: list(prefixes) for family, prefixes in allowed_views.items()
        },
    }
    digest = canonical_json_hash(identity).removeprefix("sha256:")[:32]
    return ServiceUseTerms.model_validate(
        {
            **identity,
            "serviceUseId": f"service-use:{digest}",
        }
    )


def service_descriptor_sort_key(
    descriptor: ServiceDescriptor,
) -> tuple[datetime, int, str]:
    advertisement = descriptor.candidate.advertisement
    timestamp = (
        advertisement.updated_at
        or advertisement.created_at
        or datetime.min.replace(tzinfo=UTC)
    )
    return (timestamp, advertisement.refresh_seq, descriptor.candidate.key)


def newest_service_descriptor(
    descriptors: Collection[ServiceDescriptor],
) -> ServiceDescriptor | None:
    if not descriptors:
        return None
    return max(descriptors, key=service_descriptor_sort_key)


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


def _stale_service_use_error(exc: ServiceUnavailable) -> bool:
    if not exc.code.startswith("contract_"):
        return False
    return exc.diagnostics.get("status") in {
        item.value for item in _STALE_SERVICE_USE_STATUSES
    }


def _service_use_conflict_unavailable(exc: StateConflict) -> ServiceUnavailable:
    return ServiceUnavailable(
        f"contract_{ContractValidityStatus.INVALID_TOKEN.value}",
        "Service-use contract could not be refreshed",
        {
            "status": ContractValidityStatus.INVALID_TOKEN.value,
            "reason": str(exc),
        },
    )


def _pending_service_use_error(exc: ServiceUnavailable) -> bool:
    return exc.code == "service_contract_pending"


def _normalize_operations(
    descriptor: ServiceDescriptor,
    operations: Collection[str],
) -> frozenset[str]:
    normalized = frozenset(
        _require_text(operation, field_name="service operation")
        for operation in operations
    )
    unsupported = normalized.difference(descriptor.supported_operations)
    if unsupported:
        raise UnsupportedServiceScope(
            f"Service {descriptor.service_id!r} does not advertise operations "
            f"{sorted(unsupported)!r}"
        )
    return normalized


def _normalize_view_scope(
    descriptor: ServiceDescriptor,
    views: Collection[str] | Mapping[str, Collection[str]],
) -> Mapping[str, tuple[str, ...]]:
    if isinstance(views, Mapping):
        requested: dict[str, tuple[str, ...] | None] = {}
        for family, prefixes in views.items():
            family_name = _require_text(family, field_name="service view family")
            if isinstance(prefixes, str):
                requested[family_name] = (
                    _require_text(prefixes, field_name="service view prefix"),
                )
            else:
                requested[family_name] = tuple(
                    _require_text(prefix, field_name="service view prefix")
                    for prefix in prefixes
                )
    elif isinstance(views, str):
        family = _require_text(views, field_name="service view family")
        requested = {family: None}
    else:
        requested = {
            _require_text(family, field_name="service view family"): None
            for family in views
        }
    result: dict[str, tuple[str, ...]] = {}
    for family, prefixes in requested.items():
        service_family = descriptor.views.get(family)
        if service_family is None:
            raise UnsupportedServiceScope(
                f"Service {descriptor.service_id!r} does not advertise view family "
                f"{family!r}"
            )
        if prefixes is None:
            prefixes = (service_family.key_prefix,)
        normalized_prefixes = tuple(sorted(set(prefixes)))
        for prefix in normalized_prefixes:
            if not prefix.startswith(service_family.key_prefix):
                raise UnsupportedServiceScope(
                    f"View prefix {prefix!r} is outside service view family {family!r}"
                )
        result[family] = normalized_prefixes
    return MappingProxyType(result)


def _view_ref_authorized(lease: ServiceUseLease, view: ServiceViewRef) -> bool:
    for family, prefixes in lease.terms.allowed_views.items():
        view_family = lease.descriptor.views.get(family)
        if view_family is None:
            continue
        if view.store_name != view_family.store_name:
            continue
        if any(view.key.startswith(prefix) for prefix in prefixes):
            return True
    return False


def _lease_covers_scope(
    lease: ServiceUseLease,
    *,
    operations: Collection[str],
    views: Collection[str] | Mapping[str, Collection[str]],
) -> bool:
    try:
        requested_operations = _normalize_operations(lease.descriptor, operations)
        requested_views = _normalize_view_scope(lease.descriptor, views)
    except UnsupportedServiceScope:
        return False
    if not requested_operations.issubset(set(lease.terms.allowed_operations)):
        return False
    for family, prefixes in requested_views.items():
        allowed = lease.terms.allowed_views.get(family, ())
        if not all(
            any(prefix.startswith(allowed_prefix) for allowed_prefix in allowed)
            for prefix in prefixes
        ):
            return False
    return True


def _command_applies_to_service(
    message: DeckrMessage,
    body: ServiceCommandBody,
    *,
    service_id: str,
    namespace: str,
    endpoint: EndpointAddress,
) -> bool:
    if body.service_namespace != namespace:
        return False
    if message.subject.kind != "service":
        return False
    identifiers = message.subject.identifiers
    if identifiers.get("serviceId") != service_id:
        return False
    if identifiers.get("namespace") != namespace:
        return False
    if isinstance(message.recipient, EndpointTarget):
        return message.recipient.endpoint == endpoint
    return endpoint_target(endpoint) == message.recipient


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
