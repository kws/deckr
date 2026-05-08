"""Current-state contracts for Deckr service endpoints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_serializer, field_validator

from deckr.contracts.messages import EndpointAddress, service_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.state import (
    decode_key_token,
    encode_key_token,
)


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def service_catalog_key(service_id: str) -> str:
    return ".".join(("catalog", "services", encode_key_token(service_id)))


def service_status_key(service_id: str) -> str:
    return ".".join(("status", "services", encode_key_token(service_id)))


def service_view_key(
    service_id: str,
    service_namespace: str,
    *tokens: str,
) -> str:
    return ".".join(
        (
            "view",
            "services",
            encode_key_token(service_id),
            encode_key_token(service_namespace),
            *(encode_key_token(token) for token in tokens),
        )
    )


def parse_service_catalog_key(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[:2] != ["catalog", "services"]:
        return None
    return decode_key_token(parts[2])


def parse_service_status_key(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[:2] != ["status", "services"]:
        return None
    return decode_key_token(parts[2])


def parse_service_view_key(key: str) -> tuple[str, str, tuple[str, ...]] | None:
    parts = key.split(".")
    if len(parts) < 4 or parts[:2] != ["view", "services"]:
        return None
    return (
        decode_key_token(parts[2]),
        decode_key_token(parts[3]),
        tuple(decode_key_token(part) for part in parts[4:]),
    )


class ServiceStatusValue(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ServiceCatalog(DeckrModel):
    service_id: str = Field(alias="serviceId")
    service_endpoint: EndpointAddress = Field(alias="serviceEndpoint")
    service_namespace: str = Field(alias="serviceNamespace")
    session_id: str = Field(alias="sessionId")
    supported_operations: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="supportedOperations",
    )
    view_prefixes: tuple[str, ...] = Field(default_factory=tuple, alias="viewPrefixes")
    timestamp: datetime
    labels: Mapping[str, str] = Field(default_factory=dict)
    annotations: JsonObject = Field(default_factory=dict)
    diagnostics: JsonObject = Field(default_factory=dict)

    @field_validator("service_id", "service_namespace", "session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service catalog field")

    @field_validator("supported_operations", "view_prefixes")
    @classmethod
    def _validate_tuple(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            _require_text(item, field_name="service catalog list item")
            for item in value
        )

    @field_validator("labels", mode="after")
    @classmethod
    def _freeze_labels(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(
            {
                _require_text(key, field_name="service catalog label key"): (
                    _require_text(item, field_name="service catalog label value")
                )
                for key, item in value.items()
            }
        )

    @field_validator("annotations", "diagnostics", mode="after")
    @classmethod
    def _freeze_json_object(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_serializer("labels")
    def _serialize_labels(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)

    @field_serializer("annotations", "diagnostics")
    def _serialize_json_object(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def model_post_init(self, __context: Any) -> None:
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("service catalog endpoint must match serviceId")


class ServiceStatus(DeckrModel):
    service_id: str = Field(alias="serviceId")
    service_endpoint: EndpointAddress = Field(alias="serviceEndpoint")
    service_namespace: str = Field(alias="serviceNamespace")
    session_id: str = Field(alias="sessionId")
    status: ServiceStatusValue
    timestamp: datetime
    diagnostics: JsonObject = Field(default_factory=dict)

    @field_validator("service_id", "service_namespace", "session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service status field")

    @field_validator("diagnostics", mode="after")
    @classmethod
    def _freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_serializer("diagnostics")
    def _serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    def model_post_init(self, __context: Any) -> None:
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("service status endpoint must match serviceId")
