from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.actions.endpoints import (
    action_provider_address,
    require_provider_instance_id,
)
from deckr.actions.messages import ActionDescriptor, MatchedCapability
from deckr.beacon import AdvertisementRecord
from deckr.concord import canonical_json_hash
from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.hardware.descriptors import ControlRef, DeviceRef
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HARDWARE_PROFILE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    ProfileCapacity,
    hardware_claim_conflicts,
    hardware_payload_from_advertisement,
)

ACTIONS_PROFILE_ID = "dev.deckr.profile.actions.v1"
ACTION_BINDING_PROFILE_ID = "dev.deckr.profile.action_binding.v1"

ACTIONS_FEATURE_ID = "dev.deckr.actions"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _endpoint_id(endpoint: EndpointAddress, *, family: str, field_name: str) -> str:
    if endpoint.family != family:
        raise ValueError(f"{field_name} must use {family}:<id>")
    return endpoint.endpoint_id


class ActionBeaconDescriptor(ActionDescriptor):
    capacity: ProfileCapacity | None = None
    hints: JsonObject = Field(default_factory=dict)

    @field_validator("hints", mode="before")
    @classmethod
    def _thaw_hints(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("hints", mode="after")
    @classmethod
    def _freeze_hints(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("hints")
    def _serialize_hints(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class ActionsBeaconPayload(DeckrModel):
    profile: Literal[ACTIONS_PROFILE_ID] = ACTIONS_PROFILE_ID
    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_endpoint: EndpointAddress = Field(alias="providerEndpoint")
    provider_id: str = Field(alias="providerId")
    session_id: str = Field(alias="sessionId")
    labels: Mapping[str, str] = Field(default_factory=dict)
    annotations: JsonObject = Field(default_factory=dict)
    actions: Mapping[str, ActionBeaconDescriptor] = Field(default_factory=dict)

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @field_validator("provider_id", "session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="actions profile field")

    @field_validator("labels", mode="after")
    @classmethod
    def _freeze_labels(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(
            {
                _require_text(key, field_name="actions profile label key"): _require_text(
                    item,
                    field_name="actions profile label value",
                )
                for key, item in value.items()
            }
        )

    @field_validator("annotations", mode="before")
    @classmethod
    def _thaw_annotations(cls, value: Any) -> Any:
        return thaw_json(value)

    @field_validator("annotations", mode="after")
    @classmethod
    def _freeze_annotations(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_validator("actions", mode="after")
    @classmethod
    def _freeze_actions(
        cls,
        value: Mapping[str, ActionBeaconDescriptor],
    ) -> Mapping[str, ActionBeaconDescriptor]:
        return freeze_json(value)

    @field_serializer("labels")
    def _serialize_labels(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)

    @field_serializer("annotations")
    def _serialize_annotations(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)

    @field_serializer("actions")
    def _serialize_actions(
        self,
        value: Mapping[str, ActionBeaconDescriptor],
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def _validate_identity(self) -> ActionsBeaconPayload:
        if self.provider_endpoint != action_provider_address(self.provider_instance_id):
            raise ValueError("providerEndpoint must equal action_provider:<providerInstanceId>")
        for key, descriptor in self.actions.items():
            action_id = _require_text(key, field_name="action profile key")
            if descriptor.action_id != action_id:
                raise ValueError("action map keys must match descriptor actionId")
            if descriptor.provider_id not in {None, self.provider_id}:
                raise ValueError("action descriptor providerId must match providerId")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ActionBindingTerms(DeckrModel):
    profile: Literal[ACTION_BINDING_PROFILE_ID] = ACTION_BINDING_PROFILE_ID
    binding_id: str = Field(alias="bindingId")
    controller_endpoint: EndpointAddress = Field(alias="controllerEndpoint")
    provider_endpoint: EndpointAddress = Field(alias="providerEndpoint")
    provider_instance_id: str = Field(alias="providerInstanceId")
    provider_id: str = Field(alias="providerId")
    action_id: str = Field(alias="actionId")
    action_instance_id: str = Field(alias="actionInstanceId")
    config_id: str = Field(alias="configId")
    context_id: str = Field(alias="contextId")
    hardware_claim_id: str = Field(alias="hardwareClaimId")
    device_ref: DeviceRef = Field(alias="deviceRef")
    control_ref: ControlRef = Field(alias="controlRef")
    matched_capabilities: tuple[MatchedCapability, ...] = Field(
        default_factory=tuple,
        alias="matchedCapabilities",
    )

    @field_validator(
        "binding_id",
        "provider_id",
        "action_id",
        "action_instance_id",
        "config_id",
        "context_id",
        "hardware_claim_id",
    )
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="action binding field")

    @field_validator("provider_instance_id")
    @classmethod
    def _validate_provider_instance_id(cls, value: str) -> str:
        return require_provider_instance_id(value, field_name="providerInstanceId")

    @model_validator(mode="after")
    def _validate_identity(self) -> ActionBindingTerms:
        _endpoint_id(
            self.controller_endpoint,
            family="controller",
            field_name="controllerEndpoint",
        )
        provider_instance_id = _endpoint_id(
            self.provider_endpoint,
            family="action_provider",
            field_name="providerEndpoint",
        )
        if provider_instance_id != self.provider_instance_id:
            raise ValueError("providerEndpoint must equal action_provider:<providerInstanceId>")
        if self.device_ref != self.control_ref.device_ref:
            raise ValueError("controlRef.deviceRef must match deviceRef")
        for matched in self.matched_capabilities:
            capability = matched.capability
            if capability.device_ref != self.device_ref:
                raise ValueError("matchedCapabilities must refer to the binding device")
            if capability.control_id not in {None, self.control_ref.control_id}:
                raise ValueError("matchedCapabilities must refer to the binding control")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def profile_terms_hash(terms: HardwareClaimTerms | ActionBindingTerms) -> str:
    return canonical_json_hash(terms)


def actions_payload_from_advertisement(
    advertisement: AdvertisementRecord,
) -> ActionsBeaconPayload:
    if advertisement.feature_id != ACTIONS_FEATURE_ID:
        raise ValueError("advertisement featureId is not dev.deckr.actions")
    if advertisement.payload is None:
        raise ValueError("actions advertisement requires payload")
    payload = ActionsBeaconPayload.model_validate(advertisement.payload)
    if payload.session_id != advertisement.session_id:
        raise ValueError("actions payload sessionId must match advertisement sessionId")
    if payload.provider_endpoint != advertisement.endpoint:
        raise ValueError("actions payload providerEndpoint must match advertisement endpoint")
    return payload


__all__ = [
    "ACTION_BINDING_PROFILE_ID",
    "ACTIONS_FEATURE_ID",
    "ACTIONS_PROFILE_ID",
    "HARDWARE_CLAIM_PROFILE_ID",
    "HARDWARE_FEATURE_ID",
    "HARDWARE_PROFILE_ID",
    "ActionBeaconDescriptor",
    "ActionBindingTerms",
    "ActionsBeaconPayload",
    "HardwareAdvertisementDevice",
    "HardwareBeaconPayload",
    "HardwareClaimDevice",
    "HardwareClaimTerms",
    "ProfileCapacity",
    "actions_payload_from_advertisement",
    "hardware_claim_conflicts",
    "hardware_payload_from_advertisement",
    "profile_terms_hash",
]
