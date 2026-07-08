"""Internal service-view contracts for provider action availability."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from deckr.actions.endpoints import (
    action_provider_address,
    require_provider_instance_id,
)
from deckr.actions.messages import ActionAvailabilityEntry
from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel
from deckr.services import (
    ServiceProtocol,
    ServiceViewFamilyDefinition,
    ServiceViewRef,
    service_view_key,
)

ACTION_AVAILABILITY_SERVICE_NAMESPACE = "dev.deckr.action_availability.service"
ACTION_AVAILABILITY_SERVICE_FEATURE_ID = ACTION_AVAILABILITY_SERVICE_NAMESPACE
ACTION_AVAILABILITY_SERVICE_ADVERTISEMENT_PROFILE_ID = (
    "dev.deckr.action_availability.service.advertisement.v1"
)
ACTION_AVAILABILITY_SERVICE_USE_PROFILE_ID = (
    "dev.deckr.action_availability.service_use.v1"
)
ACTION_AVAILABILITY_SERVICE_VIEW_STORE_NAME = (
    "deckr_action_availability_service_view_v1"
)
ACTION_AVAILABILITY_SERVICE_VIEW_FAMILY = "actions"
ACTION_AVAILABILITY_CURRENT_VIEW_TOKEN = "current"
ACTION_AVAILABILITY_READ_OPERATION = "readActionsCurrent"


def action_availability_service_id(provider_instance_id: str) -> str:
    """Return the internal availability service id for one provider instance."""

    provider_instance_id = require_provider_instance_id(
        provider_instance_id,
        field_name="providerInstanceId",
    )
    return f"action-availability.{provider_instance_id}"


def action_availability_provider_instance_id(service_id: str) -> str | None:
    """Return the provider instance id encoded in an availability service id."""

    prefix = "action-availability."
    if not service_id.startswith(prefix):
        return None
    provider_instance_id = service_id[len(prefix) :]
    try:
        return require_provider_instance_id(
            provider_instance_id,
            field_name="providerInstanceId",
        )
    except ValueError:
        return None


def action_availability_service_protocol() -> ServiceProtocol:
    """Return the shared service protocol for action availability views."""

    return ServiceProtocol(
        namespace=ACTION_AVAILABILITY_SERVICE_NAMESPACE,
        feature_id=ACTION_AVAILABILITY_SERVICE_FEATURE_ID,
        advertisement_profile=ACTION_AVAILABILITY_SERVICE_ADVERTISEMENT_PROFILE_ID,
        use_profile=ACTION_AVAILABILITY_SERVICE_USE_PROFILE_ID,
        operations=(ACTION_AVAILABILITY_READ_OPERATION,),
        view_families={
            ACTION_AVAILABILITY_SERVICE_VIEW_FAMILY: ServiceViewFamilyDefinition(
                storeName=ACTION_AVAILABILITY_SERVICE_VIEW_STORE_NAME,
            )
        },
    )


ACTION_AVAILABILITY_SERVICE_PROTOCOL = action_availability_service_protocol()


def action_availability_view_key(service_id: str) -> str:
    """Return the logical ``actions/current`` service view key."""

    return service_view_key(
        service_id,
        ACTION_AVAILABILITY_SERVICE_VIEW_FAMILY,
        ACTION_AVAILABILITY_CURRENT_VIEW_TOKEN,
    )


def action_availability_view_ref(service_id: str) -> ServiceViewRef:
    """Return the current action availability view reference for a service."""

    return ServiceViewRef(
        store_name=ACTION_AVAILABILITY_SERVICE_VIEW_STORE_NAME,
        key=action_availability_view_key(service_id),
    )


class ActionAvailabilityViewPayload(DeckrModel):
    """Provider-scoped current action availability view payload."""

    model_config = ConfigDict(extra="ignore")

    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_endpoint: EndpointAddress = Field(alias="providerEndpoint")
    provider_id: str = Field(alias="providerId")
    provider_session_id: str = Field(alias="providerSessionId")
    entries: tuple[ActionAvailabilityEntry, ...] = Field(default_factory=tuple)

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("provider_id", "provider_session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("action availability payload field must be a string")
        if value.strip() != value:
            raise ValueError(
                "action availability payload field must not contain leading or "
                "trailing whitespace"
            )
        if not value:
            raise ValueError("action availability payload field must not be empty")
        return value

    @field_validator("entries", mode="before")
    @classmethod
    def _validate_entries(
        cls,
        value: Iterable[ActionAvailabilityEntry | dict[str, Any]],
    ) -> tuple[ActionAvailabilityEntry, ...]:
        return tuple(ActionAvailabilityEntry.model_validate(item) for item in value)

    def model_post_init(self, __context: Any) -> None:
        if self.provider_endpoint != action_provider_address(self.provider_instance_id):
            raise ValueError(
                "providerEndpoint must equal action_provider:<providerInstanceId>"
            )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
