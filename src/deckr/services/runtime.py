"""Beacon/Concord service models for Deckr service participants."""

from __future__ import annotations

import math
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
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import DeckrMessage, EndpointAddress, service_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.services.messages import (
    SERVICE_MESSAGE,
    ServiceExchangePattern,
    ServiceMessageBody,
    ServiceMessageDirection,
    ServiceMessageIntent,
    service_body,
)

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
_JSON_SCHEMA_CONTRACT_KEYS = frozenset(
    {
        "$ref",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "items",
        "oneOf",
        "properties",
        "type",
    }
)
_OPERATION_INTENTS = frozenset(
    {
        ServiceMessageIntent.COMMAND,
        ServiceMessageIntent.QUERY,
    }
)


class ServiceBackendStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ServiceViewWriter(StrEnum):
    SERVICE = "service"
    CONSUMER = "consumer"


class ServiceOperationDefinition(DeckrModel):
    description: str | None = None

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="service operation description")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServicePayloadSchema(DeckrModel):
    """A JSON Schema contract for one service-message payload position."""

    schema_id: str = Field(alias="schemaId")
    json_schema: JsonObject = Field(alias="schema")

    @field_validator("schema_id")
    @classmethod
    def _validate_schema_id(cls, value: str) -> str:
        return _require_text(value, field_name="service payload schema id")

    @field_validator("json_schema", mode="before")
    @classmethod
    def _thaw_schema(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("json_schema", mode="after")
    @classmethod
    def _freeze_schema(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not value:
            raise ValueError("service payload schema must not be empty")
        if not any(key in value for key in _JSON_SCHEMA_CONTRACT_KEYS):
            raise ValueError(
                "service payload schema must include a JSON Schema contract keyword"
            )
        _require_json_wire_safe(value, field_name="service payload schema")
        return freeze_json(value)

    @field_serializer("json_schema")
    def _serialize_schema(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServiceMessageDefinition(DeckrModel):
    operation: str | None = None
    intent: ServiceMessageIntent
    exchange_pattern: ServiceExchangePattern = Field(alias="exchangePattern")
    direction: ServiceMessageDirection
    params_schema: ServicePayloadSchema | None = Field(
        default=None,
        alias="paramsSchema",
    )
    result_schema: ServicePayloadSchema | None = Field(
        default=None,
        alias="resultSchema",
    )
    event_schema: ServicePayloadSchema | None = Field(
        default=None,
        alias="eventSchema",
    )
    error_schema: ServicePayloadSchema | None = Field(
        default=None,
        alias="errorSchema",
    )

    @field_validator("operation")
    @classmethod
    def _validate_operation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value, field_name="service message operation")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServiceViewFamily(DeckrModel):
    store_name: str = Field(alias="storeName")
    key_prefix: str = Field(alias="keyPrefix")
    writer: ServiceViewWriter

    @field_validator("store_name", "key_prefix")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service view family field")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServiceViewFamilyDefinition(DeckrModel):
    store_name: str = Field(alias="storeName")
    writer: ServiceViewWriter

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
    operations: Mapping[str, ServiceOperationDefinition]
    messages: Mapping[str, ServiceMessageDefinition]
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
        operations: dict[str, ServiceOperationDefinition] = {}
        for name, definition in self.operations.items():
            key = _require_text(name, field_name="service operation")
            operations[key] = (
                definition
                if isinstance(definition, ServiceOperationDefinition)
                else ServiceOperationDefinition.model_validate(definition)
            )
        object.__setattr__(self, "operations", MappingProxyType(operations))
        messages: dict[str, ServiceMessageDefinition] = {}
        for name, definition in self.messages.items():
            key = _require_text(name, field_name="service message")
            messages[key] = (
                definition
                if isinstance(definition, ServiceMessageDefinition)
                else ServiceMessageDefinition.model_validate(definition)
            )
        _validate_service_message_operations(operations, messages)
        object.__setattr__(self, "messages", MappingProxyType(messages))
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
            supportedOperations=tuple(self.operations),
            supportedMessages=self.messages,
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
    supported_messages: Mapping[str, ServiceMessageDefinition] = Field(
        alias="supportedMessages"
    )
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
        return tuple(
            _require_text(item, field_name="service operation") for item in value
        )

    @field_validator("views", mode="after")
    @classmethod
    def _freeze_views_and_messages(
        cls,
        value: Mapping[str, ServiceViewFamily] | Mapping[str, ServiceMessageDefinition],
    ) -> Mapping[str, ServiceViewFamily] | Mapping[str, ServiceMessageDefinition]:
        return freeze_json(value)

    @field_validator("supported_messages", mode="after")
    @classmethod
    def _freeze_messages(
        cls,
        value: Mapping[str, ServiceMessageDefinition],
    ) -> Mapping[str, ServiceMessageDefinition]:
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

    @field_serializer("supported_messages")
    def _serialize_supported_messages(
        self,
        value: Mapping[str, ServiceMessageDefinition],
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


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    """Profile-validated service fact from Beacon discovery."""

    candidate: Candidate | None
    service_id: str
    namespace: str
    endpoint: EndpointAddress
    session_id: str
    advertisement_profile: str
    use_profile: str
    supported_operations: frozenset[str]
    supported_messages: Mapping[str, ServiceMessageDefinition]
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
        object.__setattr__(
            self,
            "supported_messages",
            MappingProxyType(dict(self.supported_messages)),
        )
        object.__setattr__(self, "views", MappingProxyType(dict(self.views)))
        object.__setattr__(self, "diagnostics", freeze_json(dict(self.diagnostics)))


@dataclass(slots=True)
class ServiceUseLease:
    agreement: ConcordAgreementLease
    descriptor: ServiceDescriptor
    consumer_endpoint: EndpointAddress
    consumer_session_id: str
    _had_valid_authority: bool = False

    def __post_init__(self) -> None:
        self.consumer_session_id = _require_text(
            self.consumer_session_id,
            field_name="consumer session id",
        )
        self._had_valid_authority = _agreement_had_valid_authority(self.agreement)

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract

    async def refresh(self) -> None:
        had_valid_authority = (
            self._had_valid_authority
            or _agreement_had_valid_authority(self.agreement)
        )
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
            self._had_valid_authority = True
            return
        if (
            validity.status == ContractValidityStatus.UNAVAILABLE
            and had_valid_authority
        ):
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


def _agreement_had_valid_authority(agreement: ConcordAgreementLease) -> bool:
    valid = getattr(agreement, "valid", None)
    if valid is not None:
        return bool(valid)
    validity = getattr(agreement, "validity", None)
    return bool(getattr(validity, "valid", False))


@dataclass(frozen=True, slots=True)
class ServiceViewWriteContext:
    writer: ServiceViewWriter
    service_id: str
    service_namespace: str
    service_endpoint: EndpointAddress
    service_session_id: str
    consumer_endpoint: EndpointAddress
    consumer_session_id: str
    contract: ContractPointer
    views: Mapping[str, ServiceViewFamily]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service_id",
            _require_text(self.service_id, field_name="service id"),
        )
        object.__setattr__(
            self,
            "service_namespace",
            _require_text(
                self.service_namespace,
                field_name="service namespace",
            ),
        )
        object.__setattr__(
            self,
            "service_session_id",
            _require_text(
                self.service_session_id,
                field_name="service session id",
            ),
        )
        object.__setattr__(
            self,
            "consumer_session_id",
            _require_text(
                self.consumer_session_id,
                field_name="consumer session id",
            ),
        )
        object.__setattr__(self, "views", MappingProxyType(dict(self.views)))


@dataclass(frozen=True, slots=True)
class ServiceViewReadContext:
    reader: ServiceViewWriter
    service_id: str
    service_namespace: str
    service_endpoint: EndpointAddress
    service_session_id: str
    consumer_endpoint: EndpointAddress
    consumer_session_id: str
    contract: ContractPointer
    views: Mapping[str, ServiceViewFamily]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service_id",
            _require_text(self.service_id, field_name="service id"),
        )
        object.__setattr__(
            self,
            "service_namespace",
            _require_text(
                self.service_namespace,
                field_name="service namespace",
            ),
        )
        object.__setattr__(
            self,
            "service_session_id",
            _require_text(
                self.service_session_id,
                field_name="service session id",
            ),
        )
        object.__setattr__(
            self,
            "consumer_session_id",
            _require_text(
                self.consumer_session_id,
                field_name="consumer session id",
            ),
        )
        object.__setattr__(self, "views", MappingProxyType(dict(self.views)))


@dataclass(frozen=True, slots=True)
class AuthorizedServiceMessage:
    contract: ContractHandle
    record: ContractRecord
    body: ServiceMessageBody
    view_read_context: ServiceViewReadContext
    view_write_context: ServiceViewWriteContext


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


def service_message_ends_service_use(message: ServiceMessageBody) -> bool:
    """Return whether a service response reports ended service-use authority."""

    error = getattr(message, "error", None)
    if error is None:
        return False
    if error.code in _SERVICE_USE_LOSS_CODES:
        return True
    if error.code not in _SERVICE_USE_REPLY_ERROR_CODES:
        return False
    return _service_use_diagnostics_end_service_use(
        dict(getattr(error, "diagnostics", None) or {})
    )


async def authorize_service_message(
    participant: ConcordParticipant,
    message: DeckrMessage,
    *,
    service_id: str,
    protocol: ServiceProtocol,
    name: str | None = None,
) -> AuthorizedServiceMessage:
    """Validate that a service message is authorized by its exact Concord pointer."""

    try:
        body = service_body(message)
    except (TypeError, ValueError) as exc:
        raise ServiceUseAuthorizationError(
            "invalid_service_message",
            "Service message body is invalid",
        ) from exc
    if message.message_type != SERVICE_MESSAGE:
        raise ServiceUseAuthorizationError(
            "invalid_service_message",
            "Service authorization requires a serviceMessage",
            {"messageType": message.message_type},
        )
    expected_name = body.name if name is None else name
    if not _service_subject_matches(
        message,
        service_id=service_id,
        namespace=protocol.namespace,
        name=expected_name,
    ):
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service message subject does not match the authorized service message",
            {
                "serviceId": service_id,
                "namespace": protocol.namespace,
                "name": expected_name,
            },
        )
    if body.service_namespace != protocol.namespace or body.name != expected_name:
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service message does not target the authorized protocol message",
            {
                "serviceNamespace": body.service_namespace,
                "expectedNamespace": protocol.namespace,
                "name": body.name,
                "expectedName": expected_name,
            },
        )
    definition = protocol.messages.get(expected_name)
    if definition is None:
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service protocol does not declare this message",
            {"name": expected_name},
        )
    if (
        body.intent != definition.intent
        or body.exchange_pattern != definition.exchange_pattern
    ):
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service message metadata does not match the protocol definition",
            {
                "name": expected_name,
                "intent": body.intent.value,
                "expectedIntent": definition.intent.value,
                "exchangePattern": body.exchange_pattern.value,
                "expectedExchangePattern": definition.exchange_pattern.value,
            },
        )
    if definition.direction not in {
        ServiceMessageDirection.CONSUMER_TO_SERVICE,
        ServiceMessageDirection.BIDIRECTIONAL,
    }:
        raise ServiceUseAuthorizationError(
            "direction_mismatch",
            "Service message direction is not authorized from consumer to service",
            {"name": expected_name, "direction": definition.direction.value},
        )

    pointer = message.contract
    if pointer is None:
        raise ServiceUseAuthorizationError(
            "missing_contract",
            "Service message requires a Concord contract pointer",
        )
    managed = _managed_contract_for_pointer(
        participant.managed_contracts,
        pointer,
    )
    if managed is None:
        await participant.reconcile(reason="service message authorization")
        managed = _managed_contract_for_pointer(
            participant.managed_contracts,
            pointer,
        )
    if managed is None:
        raise ServiceUseAuthorizationError(
            "contract_not_managed",
            "Service message contract is not managed by this participant",
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
            "Service message contract is not valid",
            {
                "contractId": pointer.contract_id,
                "generation": pointer.generation,
                "status": validity.status.value,
                "reason": validity.reason,
            },
        )

    if not _service_message_contract_match(
        validity.contract,
        participant=participant,
        sender=message.sender,
        service_id=service_id,
        protocol=protocol,
        name=expected_name,
    ):
        raise ServiceUseAuthorizationError(
            "scope_mismatch",
            "Service message contract does not authorize this message",
            {
                "contractId": pointer.contract_id,
                "generation": pointer.generation,
                "serviceId": service_id,
                "name": expected_name,
            },
        )
    context = _service_view_context_from_contract(
        managed,
        protocol=protocol,
        service_id=service_id,
        participant_side=ServiceViewWriter.SERVICE,
        record=validity.contract,
        validity=validity,
    )
    return AuthorizedServiceMessage(
        contract=managed.contract,
        record=validity.contract,
        body=body,
        view_read_context=context.view_read_context(),
        view_write_context=context.view_write_context(),
    )


@dataclass(frozen=True, slots=True)
class ServiceManagedContractContext:
    protocol: ServiceProtocol
    service_id: str
    service_namespace: str
    service_endpoint: EndpointAddress
    service_session_id: str
    consumer_endpoint: EndpointAddress
    consumer_session_id: str
    contract: ContractPointer
    views: Mapping[str, ServiceViewFamily]
    participant_side: ServiceViewWriter

    def view_read_context(self) -> ServiceViewReadContext:
        return ServiceViewReadContext(
            reader=self.participant_side,
            service_id=self.service_id,
            service_namespace=self.service_namespace,
            service_endpoint=self.service_endpoint,
            service_session_id=self.service_session_id,
            consumer_endpoint=self.consumer_endpoint,
            consumer_session_id=self.consumer_session_id,
            contract=self.contract,
            views=self.views,
        )

    def view_write_context(self) -> ServiceViewWriteContext:
        return ServiceViewWriteContext(
            writer=self.participant_side,
            service_id=self.service_id,
            service_namespace=self.service_namespace,
            service_endpoint=self.service_endpoint,
            service_session_id=self.service_session_id,
            consumer_endpoint=self.consumer_endpoint,
            consumer_session_id=self.consumer_session_id,
            contract=self.contract,
            views=self.views,
        )


def service_managed_contract_context(
    managed: ConcordManagedContract,
    *,
    protocol: ServiceProtocol,
    service_id: str,
    record: ContractRecord | None = None,
    validity: Any | None = None,
) -> ServiceManagedContractContext:
    """Derive service-side message and view context from a managed contract."""

    return _service_view_context_from_contract(
        managed,
        protocol=protocol,
        service_id=service_id,
        participant_side=ServiceViewWriter.SERVICE,
        record=record,
        validity=validity,
    )


def service_view_read_context_from_managed_contract(
    managed: ConcordManagedContract,
    *,
    protocol: ServiceProtocol,
    service_id: str,
) -> ServiceViewReadContext:
    """Return the service-side read context for a managed service-use contract."""

    return service_managed_contract_context(
        managed,
        protocol=protocol,
        service_id=service_id,
    ).view_read_context()


def service_view_write_context_from_managed_contract(
    managed: ConcordManagedContract,
    *,
    protocol: ServiceProtocol,
    service_id: str,
) -> ServiceViewWriteContext:
    """Return the service-side write context for a managed service-use contract."""

    return service_managed_contract_context(
        managed,
        protocol=protocol,
        service_id=service_id,
    ).view_write_context()


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
    if not set(payload.supported_messages).issubset(set(protocol.messages)):
        return None
    for name, message in payload.supported_messages.items():
        expected_message = protocol.messages[name]
        if message.to_dict() != expected_message.to_dict():
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
        supported_messages=payload.supported_messages,
        views=expected_views,
        backend_status=payload.backend_status,
        diagnostics=payload.diagnostics,
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
                writer=definition.writer,
            )
            for family, definition in protocol.view_families.items()
        }
    )


def _validate_service_message_operations(
    operations: Mapping[str, ServiceOperationDefinition],
    messages: Mapping[str, ServiceMessageDefinition],
) -> None:
    for name, definition in messages.items():
        if definition.operation is not None:
            if definition.operation not in operations:
                raise ValueError(
                    "service message references unknown operation "
                    f"{definition.operation!r}"
                )
            continue
        if definition.direction in {
            ServiceMessageDirection.CONSUMER_TO_SERVICE,
            ServiceMessageDirection.BIDIRECTIONAL,
        }:
            raise ValueError(
                "consumer-to-service and bidirectional service messages require "
                f"a declared operation: {name}"
            )
        if definition.intent in _OPERATION_INTENTS:
            raise ValueError(
                "service command and query messages require a declared operation: "
                f"{name}"
            )


def _service_view_context_from_contract(
    managed: ConcordManagedContract,
    *,
    protocol: ServiceProtocol,
    service_id: str,
    participant_side: ServiceViewWriter,
    record: ContractRecord | None = None,
    validity: Any | None = None,
) -> ServiceManagedContractContext:
    if participant_side != ServiceViewWriter.SERVICE:
        raise ValueError("managed service contexts are service-side only")
    service_id = _require_text(service_id, field_name="service id")
    service_endpoint = service_address(service_id)
    validity = validity or managed.validity
    token = getattr(validity, "tokens", {}).get(str(service_endpoint)) or managed.token
    if token is None:
        raise ValueError("managed service contract requires a local participant token")
    if token.participant != service_endpoint:
        raise ValueError("managed contract local participant is not the service endpoint")
    record = record or managed.record
    consumers = [
        participant
        for participant in record.participants
        if participant != service_endpoint
    ]
    if len(consumers) != 1:
        raise ValueError("managed service contract must have exactly one consumer")
    consumer_endpoint = consumers[0]
    consumer_token = getattr(validity, "tokens", {}).get(str(consumer_endpoint))
    if consumer_token is None:
        raise ValueError("managed service contract requires a consumer participant token")
    return ServiceManagedContractContext(
        protocol=protocol,
        service_id=service_id,
        service_namespace=protocol.namespace,
        service_endpoint=service_endpoint,
        service_session_id=token.session_id,
        consumer_endpoint=consumer_endpoint,
        consumer_session_id=consumer_token.session_id,
        contract=ContractPointer(
            contractId=managed.contract.contract_id,
            generation=managed.contract.generation,
        ),
        views=_service_protocol_views(protocol, service_id),
        participant_side=participant_side,
    )


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


def _service_message_contract_match(
    record: ContractRecord,
    *,
    participant: ConcordParticipant,
    sender: EndpointAddress,
    service_id: str,
    protocol: ServiceProtocol,
    name: str,
) -> bool:
    expected_participants = {str(participant.participant), str(sender)}
    return (
        {str(item) for item in record.participants} == expected_participants
        and record.profile == protocol.use_profile
        and participant.participant == service_address(service_id)
        and name in protocol.messages
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


def _require_json_wire_safe(value: Any, *, field_name: str) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _require_json_wire_safe(item, field_name=field_name)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _require_json_wire_safe(item, field_name=field_name)
        return
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain NaN or Infinity")
        return
    raise ValueError(
        f"{field_name} contains unsupported JSON value type: {type(value).__name__}"
    )
