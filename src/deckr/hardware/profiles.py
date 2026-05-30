from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.beacon import AdvertisementRecord
from deckr.contracts.messages import EndpointAddress, hardware_manager_address
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

HARDWARE_PROFILE_ID = "dev.deckr.profile.hardware.v1"
HARDWARE_CLAIM_PROFILE_ID = "dev.deckr.profile.hardware_claim.v1"
HARDWARE_FEATURE_ID = "dev.deckr.hardware"


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
    devices: tuple[HardwareClaimDevice, ...]

    @field_validator("claim_id")
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


def hardware_claim_conflicts(
    existing_claims: Iterable[HardwareClaimTerms],
    proposed_claim: HardwareClaimTerms,
) -> tuple[HardwareClaimTerms, ...]:
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


__all__ = [
    "HARDWARE_CLAIM_PROFILE_ID",
    "HARDWARE_FEATURE_ID",
    "HARDWARE_PROFILE_ID",
    "HardwareAdvertisementDevice",
    "HardwareBeaconPayload",
    "HardwareClaimDevice",
    "HardwareClaimTerms",
    "ProfileCapacity",
    "hardware_claim_conflicts",
    "hardware_payload_from_advertisement",
]
