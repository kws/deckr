"""Beacon/Concord service models for Deckr service participants."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import (
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from deckr.beacon import Candidate
from deckr.concord import (
    ConcordAgreementLease,
    ConcordConflict,
    ConcordManagedContract,
    ConcordParticipant,
    ContractHandle,
    ContractRecord,
    ContractValidityStatus,
    canonical_json_hash,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import DeckrMessage, EndpointAddress, service_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.services.messages import ServiceCommandReplyBody

_TERMINAL_DURING_NEGOTIATION = frozenset(
    {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }
)

_SERVICE_USE_LOSS_STATUSES = frozenset(
    {
        ContractValidityStatus.UNAVAILABLE.value,
        *{status.value for status in _TERMINAL_DURING_NEGOTIATION},
    }
)

_SERVICE_USE_LOSS_CODES = frozenset(
    {
        "contract_not_managed",
        "service_use_conflict",
        *{f"contract_{status}" for status in _SERVICE_USE_LOSS_STATUSES},
    }
)

_SERVICE_USE_REPLY_ERROR_CODES = frozenset(
    {
        "service_use_contract_invalid",
    }
)


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


class ServiceViewFamilyDefinition(DeckrModel):
    store_name: str = Field(alias="storeName")

    @field_validator("store_name")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service view family definition field")

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
    view_families: Mapping[str, ServiceViewFamilyDefinition]

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
        families: dict[str, ServiceViewFamilyDefinition] = {}
        for name, family in self.view_families.items():
            key = _require_text(name, field_name="service view family name")
            families[key] = (
                family
                if isinstance(family, ServiceViewFamilyDefinition)
                else ServiceViewFamilyDefinition.model_validate(family)
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
            views=_service_protocol_views(self, service_id),
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
    """Profile-validated service fact from Beacon discovery or Concord terms."""

    candidate: Candidate | None
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


@dataclass(slots=True)
class ServiceUseLease:
    agreement: ConcordAgreementLease
    descriptor: ServiceDescriptor
    terms: ServiceUseTerms

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract

    async def refresh(self) -> None:
        try:
            validity = await self.agreement.refresh()
        except ConcordConflict as exc:
            status = (
                terminal_concord_conflict_status(exc)
                or ContractValidityStatus.INVALID_TOKEN
            )
            raise ServiceUnavailable(
                f"contract_{status.value}",
                "Service-use contract could not be refreshed",
                {
                    "status": status.value,
                    "reason": str(exc),
                    "contractId": self.contract.contract_id,
                    "generation": self.contract.generation,
                    "profile": self.contract.profile,
                    "serviceId": self.descriptor.service_id,
                    "serviceSessionId": self.descriptor.session_id,
                },
            ) from exc
        if validity.valid:
            return
        raise ServiceUnavailable(
            f"contract_{validity.status.value}",
            "Service-use contract is not valid",
            {
                "status": validity.status.value,
                "reason": validity.reason,
                "contractId": self.contract.contract_id,
                "generation": self.contract.generation,
                "profile": self.contract.profile,
                "serviceId": self.descriptor.service_id,
                "serviceSessionId": self.descriptor.session_id,
            },
        )


@dataclass(frozen=True, slots=True)
class AuthorizedServiceCommand:
    contract: ContractHandle
    record: ContractRecord
    terms: ServiceUseTerms


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


class ServiceUseAuthorizationError(ValueError):
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


def terminal_concord_conflict_status(
    exc: ConcordConflict,
) -> ContractValidityStatus | None:
    """Classify terminal Concord conflicts for service infrastructure."""

    message = str(exc)
    if (
        "Concord contract is cancelled" in message
        or message.startswith("Concord contract ")
        and " is cancelled" in message
    ):
        return ContractValidityStatus.CANCELLED
    if (
        "Concord contract is missing" in message
        or message.startswith("Concord contract ")
        and " is missing" in message
    ):
        return ContractValidityStatus.MISSING_CONTRACT
    if "Concord contract" in message and "changed identity" in message:
        return ContractValidityStatus.INVALID_CONTRACT
    if "Concord participant token is missing" in message:
        return ContractValidityStatus.MISSING_TOKEN
    if (
        "Concord participant token is invalid" in message
        or "Concord participant token changed owner" in message
    ):
        return ContractValidityStatus.INVALID_TOKEN
    return None


def service_unavailable_ends_service_use(exc: ServiceUnavailable) -> bool:
    """Return whether a service-domain exception means the current lease is lost."""

    if exc.code in _SERVICE_USE_LOSS_CODES:
        return True
    return _service_use_diagnostics_end_service_use(dict(exc.diagnostics))


def service_command_reply_ends_service_use(reply: ServiceCommandReplyBody) -> bool:
    """Return whether a service command reply reports ended service-use authority."""

    error = getattr(reply, "error", None)
    if error is None:
        return False
    if error.code in _SERVICE_USE_LOSS_CODES:
        return True
    if error.code not in _SERVICE_USE_REPLY_ERROR_CODES:
        return False
    return _service_use_diagnostics_end_service_use(
        dict(getattr(error, "diagnostics", None) or {})
    )


async def authorize_service_command(
    participant: ConcordParticipant,
    message: DeckrMessage,
    *,
    service_id: str,
    protocol: ServiceProtocol,
    operation: str,
) -> AuthorizedServiceCommand:
    """Validate that a service command is authorized by its exact Concord pointer."""

    pointer = message.contract
    if pointer is None:
        raise ServiceUseAuthorizationError(
            "missing_contract",
            "Service command requires a Concord contract pointer",
        )
    managed = _managed_contract_for_pointer(
        participant.managed_contracts,
        pointer,
    )
    if managed is None:
        await participant.reconcile(reason="service command authorization")
        managed = _managed_contract_for_pointer(
            participant.managed_contracts,
            pointer,
        )
    if managed is None:
        raise ServiceUseAuthorizationError(
            "contract_not_managed",
            "Service command contract is not managed by this participant",
            {"contractId": pointer.contract_id, "generation": pointer.generation},
        )

    validity = await participant.validate(
        managed.contract,
        current_sessions={
            str(message.sender): message.sender_session_id,
        },
    )
    if not validity.valid or validity.contract is None:
        raise ServiceUseAuthorizationError(
            f"contract_{validity.status.value}",
            "Service command contract is not valid",
            {
                "contractId": pointer.contract_id,
                "generation": pointer.generation,
                "status": validity.status.value,
                "reason": validity.reason,
            },
        )

    try:
        terms = ServiceUseTerms.model_validate(validity.contract.terms or {})
    except ValueError as exc:
        raise ServiceUseAuthorizationError(
            "invalid_terms",
            "Service command contract has invalid service-use terms",
            {"reason": str(exc)},
        ) from exc

    if not _service_command_terms_match(
        validity.contract,
        terms,
        participant=participant,
        sender=message.sender,
        service_id=service_id,
        protocol=protocol,
        operation=operation,
    ):
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service command contract does not authorize this command",
            {
                "contractId": pointer.contract_id,
                "generation": pointer.generation,
                "serviceId": service_id,
                "operation": operation,
            },
        )
    return AuthorizedServiceCommand(
        contract=managed.contract,
        record=validity.contract,
        terms=terms,
    )


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
    expected_views = _service_protocol_views(protocol, payload.service_id)
    if {key: family.to_dict() for key, family in payload.views.items()} != {
        key: family.to_dict() for key, family in expected_views.items()
    }:
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
        views=expected_views,
        backend_status=payload.backend_status,
        diagnostics=payload.diagnostics,
    )


def service_descriptor_from_terms(
    protocol: ServiceProtocol,
    terms: ServiceUseTerms,
    *,
    backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
    diagnostics: Mapping[str, Any] | None = None,
) -> ServiceDescriptor:
    """Build service facts from already-negotiated Concord service-use terms."""

    if terms.profile != protocol.use_profile:
        raise UnsupportedServiceScope(
            f"Service-use terms profile {terms.profile!r} does not match "
            f"protocol {protocol.use_profile!r}"
        )
    if terms.service_namespace != protocol.namespace:
        raise UnsupportedServiceScope(
            f"Service-use terms namespace {terms.service_namespace!r} does not "
            f"match protocol {protocol.namespace!r}"
        )
    unsupported = set(terms.allowed_operations).difference(set(protocol.operations))
    if unsupported:
        raise UnsupportedServiceScope(
            f"Service-use terms allow operations outside protocol: "
            f"{sorted(unsupported)!r}"
        )
    _validate_view_scope(protocol, terms)
    return ServiceDescriptor(
        candidate=None,
        service_id=terms.service_id,
        namespace=terms.service_namespace,
        endpoint=terms.service_endpoint,
        session_id=terms.service_session_id,
        advertisement_profile=protocol.advertisement_profile,
        use_profile=terms.profile,
        supported_operations=frozenset(protocol.operations),
        views=_service_protocol_views(protocol, terms.service_id),
        backend_status=backend_status,
        diagnostics=dict(diagnostics or {}),
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
    if descriptor.candidate is None:
        return (datetime.min.replace(tzinfo=UTC), 0, "")
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


def service_use_negotiation_terminal_status(
    status: ContractValidityStatus,
) -> bool:
    return status in _TERMINAL_DURING_NEGOTIATION


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


def _validate_view_scope(protocol: ServiceProtocol, terms: ServiceUseTerms) -> None:
    protocol_views = _service_protocol_views(protocol, terms.service_id)
    for family, prefixes in terms.allowed_views.items():
        service_family = protocol_views.get(family)
        if service_family is None:
            raise UnsupportedServiceScope(
                f"Service-use terms allow unknown view family {family!r}"
            )
        for prefix in prefixes:
            if not prefix.startswith(service_family.key_prefix):
                raise UnsupportedServiceScope(
                    f"Service-use terms view prefix {prefix!r} is outside "
                    f"service view family {family!r}"
                )


def _service_protocol_views(
    protocol: ServiceProtocol,
    service_id: str,
) -> Mapping[str, ServiceViewFamily]:
    service_id = _require_text(service_id, field_name="service id")
    return MappingProxyType(
        {
            family: ServiceViewFamily(
                storeName=definition.store_name,
                keyPrefix=service_view_prefix(service_id, family),
            )
            for family, definition in protocol.view_families.items()
        }
    )


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


def _managed_contract_for_pointer(
    managed_contracts: Collection[ConcordManagedContract],
    pointer: ContractPointer,
) -> ConcordManagedContract | None:
    for managed in managed_contracts:
        contract = managed.contract
        if (
            contract.contract_id == pointer.contract_id
            and contract.generation == pointer.generation
        ):
            return managed
    return None


def _service_command_terms_match(
    record: ContractRecord,
    terms: ServiceUseTerms,
    *,
    participant: ConcordParticipant,
    sender: EndpointAddress,
    service_id: str,
    protocol: ServiceProtocol,
    operation: str,
) -> bool:
    expected_participants = {str(terms.service_endpoint), str(terms.client_endpoint)}
    return (
        {str(item) for item in record.participants} == expected_participants
        and terms.profile == protocol.use_profile
        and terms.service_id == service_id
        and terms.service_endpoint == participant.participant
        and terms.service_namespace == protocol.namespace
        and terms.service_session_id == participant.session_id
        and terms.client_endpoint == sender
        and operation in terms.allowed_operations
        and operation in protocol.operations
    )


def _service_use_diagnostics_end_service_use(diagnostics: Mapping[str, Any]) -> bool:
    status = diagnostics.get("status")
    reason = diagnostics.get("reason")
    return status in _SERVICE_USE_LOSS_STATUSES or reason in (
        _SERVICE_USE_LOSS_CODES | _SERVICE_USE_LOSS_STATUSES
    )


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
