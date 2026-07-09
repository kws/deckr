"""Shared Action Runtime service protocol contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ConfigDict, Field, field_serializer, field_validator

from deckr.actions.endpoints import require_provider_instance_id
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    ACTION_LIFECYCLE_REJECTED,
    BINDING_ATTACHED,
    BINDING_DETACHED,
    BINDING_OUTPUT,
    BINDING_OVERLAY,
    BINDING_OVERLAY_CLEAR,
    CAPABILITY_INPUT,
    CLOSE_PAGE,
    OPEN_PAGE,
    PAGE_SESSION_CLOSED,
    PAGE_SESSION_OPENED,
    REPLACE_PAGE,
    ActionAvailabilityEntry,
    ActionInstanceLifecycleBody,
    ActionLifecycleRejectedBody,
    ActionMessageBody,
    BindingAttachedBody,
    BindingDetachedBody,
    BindingOutputBody,
    BindingOverlayBody,
    BindingOverlayClearBody,
    CapabilityInputBody,
    EmptyActionBody,
    OpenPageBody,
    PageSessionLifecycleBody,
    ReplacePageBody,
)
from deckr.contracts.messages import EndpointAddress, service_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.services import (
    ServiceExchangePattern,
    ServiceMessageBody,
    ServiceMessageDefinition,
    ServiceMessageDirection,
    ServiceMessageIntent,
    ServiceOperationDefinition,
    ServiceProtocol,
    ServiceViewFamilyDefinition,
    ServiceViewRef,
    ServiceViewWriter,
    service_view_key,
)

ACTION_RUNTIME_SERVICE_NAMESPACE = "dev.deckr.action_runtime.provider"
ACTION_RUNTIME_SERVICE_FEATURE_ID = ACTION_RUNTIME_SERVICE_NAMESPACE
ACTION_RUNTIME_SERVICE_ADVERTISEMENT_PROFILE_ID = (
    "dev.deckr.action_runtime.provider.advertisement.v1"
)
ACTION_RUNTIME_SERVICE_USE_PROFILE_ID = (
    "dev.deckr.action_runtime.provider.service_use.v1"
)
ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME = "deckr_action_runtime_service_view_v1"
ACTION_RUNTIME_AVAILABILITY_VIEW_SCHEMA_ID = (
    "dev.deckr.action_runtime.availability-view.v1"
)

ACTION_AVAILABILITY_VIEW_FAMILY = "action_availability"
CURRENT_VIEW_TOKEN = "current"

ACTION_INSTANCE_CREATED_MESSAGE = "action_instance_created"
ACTION_INSTANCE_DESTROYED_MESSAGE = "action_instance_destroyed"
BINDING_ATTACHED_MESSAGE = "binding_attached"
BINDING_DETACHED_MESSAGE = "binding_detached"
PAGE_SESSION_OPENED_MESSAGE = "page_session_opened"
PAGE_SESSION_CLOSED_MESSAGE = "page_session_closed"
CAPABILITY_INPUT_MESSAGE = "capability_input"
ACTION_LIFECYCLE_REJECTED_MESSAGE = "action_lifecycle_rejected"
BINDING_OUTPUT_MESSAGE = "binding_output"
BINDING_OVERLAY_MESSAGE = "binding_overlay"
BINDING_OVERLAY_CLEAR_MESSAGE = "binding_overlay_clear"
OPEN_PAGE_MESSAGE = "open_page"
REPLACE_PAGE_MESSAGE = "replace_page"
CLOSE_PAGE_MESSAGE = "close_page"

CONTROLLER_TO_RUNTIME_MESSAGES = frozenset(
    {
        ACTION_INSTANCE_CREATED_MESSAGE,
        ACTION_INSTANCE_DESTROYED_MESSAGE,
        BINDING_ATTACHED_MESSAGE,
        BINDING_DETACHED_MESSAGE,
        PAGE_SESSION_OPENED_MESSAGE,
        PAGE_SESSION_CLOSED_MESSAGE,
        CAPABILITY_INPUT_MESSAGE,
    }
)
RUNTIME_TO_CONTROLLER_MESSAGES = frozenset(
    {
        ACTION_LIFECYCLE_REJECTED_MESSAGE,
        BINDING_OUTPUT_MESSAGE,
        BINDING_OVERLAY_MESSAGE,
        BINDING_OVERLAY_CLEAR_MESSAGE,
        OPEN_PAGE_MESSAGE,
        REPLACE_PAGE_MESSAGE,
        CLOSE_PAGE_MESSAGE,
    }
)
EVENT_MESSAGES = frozenset(
    {
        ACTION_INSTANCE_CREATED_MESSAGE,
        ACTION_INSTANCE_DESTROYED_MESSAGE,
        BINDING_ATTACHED_MESSAGE,
        BINDING_DETACHED_MESSAGE,
        PAGE_SESSION_OPENED_MESSAGE,
        PAGE_SESSION_CLOSED_MESSAGE,
        CAPABILITY_INPUT_MESSAGE,
        ACTION_LIFECYCLE_REJECTED_MESSAGE,
    }
)
COMMAND_MESSAGES = (
    CONTROLLER_TO_RUNTIME_MESSAGES | RUNTIME_TO_CONTROLLER_MESSAGES
) - EVENT_MESSAGES

_LEGACY_TO_RUNTIME_MESSAGE: Mapping[str, str] = {
    ACTION_INSTANCE_CREATED: ACTION_INSTANCE_CREATED_MESSAGE,
    ACTION_INSTANCE_DESTROYED: ACTION_INSTANCE_DESTROYED_MESSAGE,
    BINDING_ATTACHED: BINDING_ATTACHED_MESSAGE,
    BINDING_DETACHED: BINDING_DETACHED_MESSAGE,
    PAGE_SESSION_OPENED: PAGE_SESSION_OPENED_MESSAGE,
    PAGE_SESSION_CLOSED: PAGE_SESSION_CLOSED_MESSAGE,
    CAPABILITY_INPUT: CAPABILITY_INPUT_MESSAGE,
    ACTION_LIFECYCLE_REJECTED: ACTION_LIFECYCLE_REJECTED_MESSAGE,
    BINDING_OUTPUT: BINDING_OUTPUT_MESSAGE,
    BINDING_OVERLAY: BINDING_OVERLAY_MESSAGE,
    BINDING_OVERLAY_CLEAR: BINDING_OVERLAY_CLEAR_MESSAGE,
    OPEN_PAGE: OPEN_PAGE_MESSAGE,
    REPLACE_PAGE: REPLACE_PAGE_MESSAGE,
    CLOSE_PAGE: CLOSE_PAGE_MESSAGE,
}
_RUNTIME_TO_LEGACY_MESSAGE: Mapping[str, str] = {
    runtime: legacy for legacy, runtime in _LEGACY_TO_RUNTIME_MESSAGE.items()
}
_BODY_BY_RUNTIME_MESSAGE: Mapping[str, type[ActionMessageBody]] = {
    ACTION_INSTANCE_CREATED_MESSAGE: ActionInstanceLifecycleBody,
    ACTION_INSTANCE_DESTROYED_MESSAGE: ActionInstanceLifecycleBody,
    BINDING_ATTACHED_MESSAGE: BindingAttachedBody,
    BINDING_DETACHED_MESSAGE: BindingDetachedBody,
    PAGE_SESSION_OPENED_MESSAGE: PageSessionLifecycleBody,
    PAGE_SESSION_CLOSED_MESSAGE: PageSessionLifecycleBody,
    CAPABILITY_INPUT_MESSAGE: CapabilityInputBody,
    ACTION_LIFECYCLE_REJECTED_MESSAGE: ActionLifecycleRejectedBody,
    BINDING_OUTPUT_MESSAGE: BindingOutputBody,
    BINDING_OVERLAY_MESSAGE: BindingOverlayBody,
    BINDING_OVERLAY_CLEAR_MESSAGE: BindingOverlayClearBody,
    OPEN_PAGE_MESSAGE: OpenPageBody,
    REPLACE_PAGE_MESSAGE: ReplacePageBody,
    CLOSE_PAGE_MESSAGE: EmptyActionBody,
}


def action_runtime_service_id(provider_instance_id: str) -> str:
    provider_instance_id = require_provider_instance_id(
        provider_instance_id,
        field_name="providerInstanceId",
    )
    return f"action-runtime.{provider_instance_id}"


def action_runtime_provider_instance_id(service_id: str) -> str | None:
    prefix = "action-runtime."
    if not service_id.startswith(prefix):
        return None
    try:
        return require_provider_instance_id(
            service_id[len(prefix) :],
            field_name="providerInstanceId",
        )
    except ValueError:
        return None


def action_runtime_service_protocol() -> ServiceProtocol:
    messages: dict[str, ServiceMessageDefinition] = {}
    operations: dict[str, ServiceOperationDefinition] = {}
    for name in sorted(CONTROLLER_TO_RUNTIME_MESSAGES | RUNTIME_TO_CONTROLLER_MESSAGES):
        operations[name] = ServiceOperationDefinition()
        messages[name] = ServiceMessageDefinition(
            operation=name,
            intent=(
                ServiceMessageIntent.EVENT
                if name in EVENT_MESSAGES
                else ServiceMessageIntent.COMMAND
            ),
            exchangePattern=ServiceExchangePattern.ONE_WAY,
            direction=(
                ServiceMessageDirection.CONSUMER_TO_SERVICE
                if name in CONTROLLER_TO_RUNTIME_MESSAGES
                else ServiceMessageDirection.SERVICE_TO_CONSUMER
            ),
        )
    return ServiceProtocol(
        namespace=ACTION_RUNTIME_SERVICE_NAMESPACE,
        feature_id=ACTION_RUNTIME_SERVICE_FEATURE_ID,
        advertisement_profile=ACTION_RUNTIME_SERVICE_ADVERTISEMENT_PROFILE_ID,
        use_profile=ACTION_RUNTIME_SERVICE_USE_PROFILE_ID,
        operations=operations,
        messages=messages,
        view_families={
            ACTION_AVAILABILITY_VIEW_FAMILY: ServiceViewFamilyDefinition(
                storeName=ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME,
                writer=ServiceViewWriter.SERVICE,
            ),
        },
    )


ACTION_RUNTIME_SERVICE_PROTOCOL = action_runtime_service_protocol()


def action_availability_view_key(service_id: str) -> str:
    return service_view_key(
        service_id,
        ACTION_AVAILABILITY_VIEW_FAMILY,
        CURRENT_VIEW_TOKEN,
    )


def action_availability_view_ref(service_id: str) -> ServiceViewRef:
    return ServiceViewRef(
        store_name=ACTION_RUNTIME_SERVICE_VIEW_STORE_NAME,
        key=action_availability_view_key(service_id),
    )


class ActionRuntimeAvailabilityViewPayload(DeckrModel):
    """Provider-scoped current action availability for one runtime service lease."""

    model_config = ConfigDict(extra="ignore")

    provider_instance_id: str = Field(alias="providerInstanceId")
    service_id: str = Field(alias="serviceId")
    service_endpoint: EndpointAddress = Field(alias="serviceEndpoint")
    provider_id: str = Field(alias="providerId")
    service_session_id: str = Field(alias="serviceSessionId")
    labels: Mapping[str, str] = Field(default_factory=dict)
    annotations: JsonObject = Field(default_factory=dict)
    entries: tuple[ActionAvailabilityEntry, ...] = Field(default_factory=tuple)

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("service_id", "provider_id", "service_session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("action runtime availability field must be a string")
        if value.strip() != value:
            raise ValueError(
                "action runtime availability field must not contain leading or "
                "trailing whitespace"
            )
        if not value:
            raise ValueError("action runtime availability field must not be empty")
        return value

    @field_validator("labels", mode="after")
    @classmethod
    def _freeze_labels(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json({str(key): str(item) for key, item in value.items()})

    @field_serializer("labels")
    def _serialize_labels(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)

    @field_validator("annotations", mode="before")
    @classmethod
    def _thaw_annotations(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("annotations", mode="after")
    @classmethod
    def _freeze_annotations(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("annotations")
    def _serialize_annotations(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @field_validator("entries", mode="before")
    @classmethod
    def _validate_entries(
        cls,
        value: Iterable[ActionAvailabilityEntry | Mapping[str, Any]],
    ) -> tuple[ActionAvailabilityEntry, ...]:
        return tuple(ActionAvailabilityEntry.model_validate(item) for item in value)

    def model_post_init(self, __context: Any) -> None:
        if self.service_id != action_runtime_service_id(self.provider_instance_id):
            raise ValueError(
                "serviceId must equal action-runtime.<providerInstanceId>"
            )
        if self.service_endpoint != service_address(self.service_id):
            raise ValueError("serviceEndpoint must equal service:<serviceId>")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def action_runtime_message_name(legacy_message_type: str) -> str:
    name = _LEGACY_TO_RUNTIME_MESSAGE.get(legacy_message_type)
    if name is None:
        raise ValueError(f"Unsupported action runtime message {legacy_message_type!r}")
    return name


def legacy_action_message_type(runtime_message_name: str) -> str:
    name = _RUNTIME_TO_LEGACY_MESSAGE.get(runtime_message_name)
    if name is None:
        raise ValueError(
            f"Unsupported action runtime service message {runtime_message_name!r}"
        )
    return name


def action_runtime_payload(
    name: str,
    body: ActionMessageBody | Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = _action_runtime_body_for_name(name, body or {}).to_dict()
    if name in EVENT_MESSAGES:
        return {}, payload
    return payload, None


def action_runtime_body_from_service_message(
    body: ServiceMessageBody,
) -> ActionMessageBody:
    payload = body.event if body.name in EVENT_MESSAGES else body.params
    return _action_runtime_body_for_name(body.name, payload or {})


def _action_runtime_body_for_name(
    name: str,
    payload: ActionMessageBody | Mapping[str, Any],
) -> ActionMessageBody:
    body_type = _BODY_BY_RUNTIME_MESSAGE.get(name)
    if body_type is None:
        raise ValueError(f"Unsupported action runtime service message {name!r}")
    if isinstance(payload, ActionMessageBody):
        if not isinstance(payload, body_type):
            raise TypeError(
                f"{name!r} requires body type {body_type.__name__}, "
                f"got {type(payload).__name__}"
            )
        return payload
    return body_type.model_validate(thaw_json(payload))
