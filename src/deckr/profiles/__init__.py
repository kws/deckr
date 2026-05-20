from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.actions.endpoints import (
    action_provider_address,
    require_provider_instance_id,
)
from deckr.actions.messages import ActionDescriptor, MatchedCapability
from deckr.beacon import AdvertisementRecord
from deckr.concord import canonical_json_hash
from deckr.contracts.messages import EndpointAddress, hardware_manager_address
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json
from deckr.hardware.descriptors import ControlRef, DeviceDescriptor, DeviceRef

HARDWARE_PROFILE_ID = "dev.deckr.profile.hardware.v1"
ACTIONS_PROFILE_ID = "dev.deckr.profile.actions.v1"
HARDWARE_CLAIM_PROFILE_ID = "dev.deckr.profile.hardware_claim.v1"
ACTION_BINDING_PROFILE_ID = "dev.deckr.profile.action_binding.v1"

HARDWARE_FEATURE_ID = "dev.deckr.hardware"
ACTIONS_FEATURE_ID = "dev.deckr.actions"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _require_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name=field_name)


def _endpoint_id(endpoint: EndpointAddress, *, family: str, field_name: str) -> str:
    if endpoint.family != family:
        raise ValueError(f"{field_name} must use {family}:<id>")
    return endpoint.endpoint_id


class ProfileCapacity(DeckrModel):
    total_instances: int | None = Field(default=None, alias="totalInstances")
    claimed_instances: int = Field(default=0, alias="claimedInstances")
    available_instances: int | None = Field(default=None, alias="availableInstances")

    @field_validator("total_instances", "available_instances")
    @classmethod
    def _validate_optional_count(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("capacity counts must be non-negative")
        return value

    @field_validator("claimed_instances")
    @classmethod
    def _validate_claimed_instances(cls, value: int) -> int:
        if value < 0:
            raise ValueError("capacity counts must be non-negative")
        return value

    @model_validator(mode="after")
    def _validate_available_instances(self) -> ProfileCapacity:
        if (
            self.total_instances is not None
            and self.available_instances is not None
            and self.available_instances
            != max(self.total_instances - self.claimed_instances, 0)
        ):
            raise ValueError("availableInstances must match totalInstances - claimedInstances")
        return self


class HardwareAdvertisementDevice(DeckrModel):
    capacity: ProfileCapacity = Field(default_factory=ProfileCapacity)
    device_ref: DeviceRef = Field(alias="deviceRef")
    descriptor: DeviceDescriptor

    @field_serializer("descriptor")
    def _serialize_descriptor(self, value: DeviceDescriptor) -> dict[str, Any]:
        return thaw_json(value.model_dump(by_alias=True, exclude_none=True, mode="json"))


class HardwareBeaconPayload(DeckrModel):
    profile: Literal[HARDWARE_PROFILE_ID] = HARDWARE_PROFILE_ID
    manager_id: str = Field(alias="managerId")
    manager_endpoint: EndpointAddress = Field(alias="managerEndpoint")
    session_id: str = Field(alias="sessionId")
    labels: Mapping[str, str] = Field(default_factory=dict)
    devices: Mapping[str, HardwareAdvertisementDevice] = Field(default_factory=dict)

    @field_validator("manager_id", "session_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="hardware profile field")

    @field_validator("labels", mode="after")
    @classmethod
    def _freeze_labels(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(
            {
                _require_text(key, field_name="hardware profile label key"): _require_text(
                    item,
                    field_name="hardware profile label value",
                )
                for key, item in value.items()
            }
        )

    @field_validator("devices", mode="after")
    @classmethod
    def _freeze_devices(
        cls,
        value: Mapping[str, HardwareAdvertisementDevice],
    ) -> Mapping[str, HardwareAdvertisementDevice]:
        return freeze_json(value)

    @field_serializer("labels")
    def _serialize_labels(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)

    @field_serializer("devices")
    def _serialize_devices(
        self,
        value: Mapping[str, HardwareAdvertisementDevice],
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def _validate_identity(self) -> HardwareBeaconPayload:
        if self.manager_endpoint != hardware_manager_address(self.manager_id):
            raise ValueError("managerEndpoint must equal hardware_manager:<managerId>")
        for key, item in self.devices.items():
            if item.device_ref.manager_id != self.manager_id:
                raise ValueError("deviceRef.managerId must match managerId")
            if item.device_ref.device_id != key:
                raise ValueError("device map keys must match deviceRef.deviceId")
            if item.descriptor.device_id != item.device_ref.device_id:
                raise ValueError("descriptor.deviceId must match deviceRef.deviceId")
            if item.device_ref.fingerprint not in {None, item.descriptor.fingerprint}:
                raise ValueError("deviceRef fingerprint must match descriptor fingerprint")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


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


class HardwareClaimDevice(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    instance_count: int = Field(alias="instanceCount")

    @field_validator("instance_count")
    @classmethod
    def _validate_instance_count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("instanceCount must be greater than zero")
        return value


class HardwareClaimTerms(DeckrModel):
    profile: Literal[HARDWARE_CLAIM_PROFILE_ID] = HARDWARE_CLAIM_PROFILE_ID
    claim_id: str = Field(alias="claimId")
    controller_endpoint: EndpointAddress = Field(alias="controllerEndpoint")
    manager_endpoint: EndpointAddress = Field(alias="managerEndpoint")
    manager_advertisement_id: str = Field(alias="managerAdvertisementId")
    devices: tuple[HardwareClaimDevice, ...]

    @field_validator("claim_id", "manager_advertisement_id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="hardware claim field")

    @field_validator("devices", mode="after")
    @classmethod
    def _validate_devices(
        cls,
        value: tuple[HardwareClaimDevice, ...],
    ) -> tuple[HardwareClaimDevice, ...]:
        if not value:
            raise ValueError("hardware claims require at least one device")
        device_ids = [item.device_ref.device_id for item in value]
        duplicates = {item for item in device_ids if device_ids.count(item) > 1}
        if duplicates:
            raise ValueError("device ids in one claim must be unique")
        return value

    @model_validator(mode="after")
    def _validate_identity(self) -> HardwareClaimTerms:
        _endpoint_id(
            self.controller_endpoint,
            family="controller",
            field_name="controllerEndpoint",
        )
        manager_id = _endpoint_id(
            self.manager_endpoint,
            family="hardware_manager",
            field_name="managerEndpoint",
        )
        for device in self.devices:
            if device.device_ref.manager_id != manager_id:
                raise ValueError("deviceRef.managerId must match managerEndpoint")
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


def hardware_claim_conflicts(
    existing_claims: Iterable[HardwareClaimTerms],
    proposed_claim: HardwareClaimTerms,
) -> tuple[HardwareClaimTerms, ...]:
    """Return existing hardware claims that overlap the proposed device ownership."""

    proposed = _hardware_claim_device_keys(proposed_claim)
    conflicts: list[HardwareClaimTerms] = []
    for claim in existing_claims:
        if claim.claim_id == proposed_claim.claim_id:
            continue
        if _hardware_claim_device_keys(claim) & proposed:
            conflicts.append(claim)
    return tuple(conflicts)


def _hardware_claim_device_keys(claim: HardwareClaimTerms) -> set[tuple[str, str]]:
    return {
        (device.device_ref.manager_id, device.device_ref.device_id)
        for device in claim.devices
    }


def hardware_payload_from_advertisement(
    advertisement: AdvertisementRecord,
) -> HardwareBeaconPayload:
    if advertisement.feature_id != HARDWARE_FEATURE_ID:
        raise ValueError("advertisement featureId is not dev.deckr.hardware")
    if advertisement.payload is None:
        raise ValueError("hardware advertisement requires payload")
    payload = HardwareBeaconPayload.model_validate(advertisement.payload)
    if payload.session_id != advertisement.session_id:
        raise ValueError("hardware payload sessionId must match advertisement sessionId")
    if payload.manager_endpoint != advertisement.endpoint:
        raise ValueError("hardware payload managerEndpoint must match advertisement endpoint")
    return payload


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
