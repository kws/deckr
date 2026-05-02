from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import anyio
from pydantic import Field, field_serializer, field_validator, model_validator

from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

DEFAULT_STATE_STORE_NAME = "deckr_state_v1"
DEFAULT_STATE_LEASE_TTL_SECONDS = 15
DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS = 5.0

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def encode_key_token(raw: str) -> str:
    if _SAFE_TOKEN_RE.fullmatch(raw) and not raw.startswith("b64_"):
        return raw
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return "b64_" + encoded.rstrip("=")


def decode_key_token(token: str) -> str:
    if not token.startswith("b64_"):
        return token
    encoded = token[4:]
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


class StateConflict(RuntimeError):
    """Raised when a first-writer or revision-checked state write fails."""


class StateUnavailable(RuntimeError):
    """Raised when the current-state substrate cannot answer safely."""


@dataclass(frozen=True, slots=True)
class StateEntry:
    key: str
    value: Mapping[str, Any]
    revision: int


@dataclass(frozen=True, slots=True)
class StateChange:
    operation: Literal["put", "delete", "expire"]
    key: str
    entry: StateEntry | None = None


class StateStore(Protocol):
    async def get(self, key: str) -> StateEntry | None: ...

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]: ...

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def delete(self, key: str, *, revision: int | None = None) -> None: ...

    def watch(
        self,
        prefix: str = "",
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[StateChange]]: ...


def state_value(value: Mapping[str, Any] | DeckrModel) -> Mapping[str, Any]:
    if isinstance(value, DeckrModel):
        return freeze_json(value.model_dump(by_alias=True, exclude_none=True, mode="json"))
    return freeze_json(dict(value))


class EndpointPresence(DeckrModel):
    endpoint: EndpointAddress
    lane: str
    session_id: str = Field(alias="sessionId")
    timestamp: datetime
    ttl_seconds: int = Field(alias="ttlSeconds")
    metadata: Mapping[str, str] = Field(default_factory=dict)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_validator("metadata", mode="after")
    @classmethod
    def _freeze_metadata(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return freeze_json(value)

    @field_serializer("metadata")
    def _serialize_metadata(self, value: Mapping[str, str]) -> dict[str, str]:
        return thaw_json(value)


class HardwareInventoryDevice(DeckrModel):
    device_ref: DeviceRef = Field(alias="deviceRef")
    descriptor: DeviceDescriptor

    @field_serializer("descriptor")
    def _serialize_descriptor(self, value: DeviceDescriptor) -> dict[str, Any]:
        return thaw_json(value.model_dump(by_alias=True, exclude_none=True, mode="json"))


class HardwareInventory(DeckrModel):
    manager_id: str = Field(alias="managerId")
    manager_endpoint: EndpointAddress = Field(alias="managerEndpoint")
    session_id: str = Field(alias="sessionId")
    timestamp: datetime
    ttl_seconds: int = Field(alias="ttlSeconds")
    devices: Mapping[str, HardwareInventoryDevice] = Field(default_factory=dict)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @field_validator("devices", mode="after")
    @classmethod
    def _freeze_devices(
        cls, value: Mapping[str, HardwareInventoryDevice]
    ) -> Mapping[str, HardwareInventoryDevice]:
        return freeze_json(value)

    @field_serializer("devices")
    def _serialize_devices(
        self, value: Mapping[str, HardwareInventoryDevice]
    ) -> dict[str, dict[str, Any]]:
        return {
            key: item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for key, item in value.items()
        }

    @model_validator(mode="after")
    def _validate_devices(self) -> HardwareInventory:
        for key, item in self.devices.items():
            if item.device_ref.manager_id != self.manager_id:
                raise ValueError(
                    "hardware inventory device references must match managerId"
                )
            if item.device_ref.device_id != key:
                raise ValueError(
                    "hardware inventory device map keys must match deviceRef.deviceId"
                )
            if item.descriptor.device_id != item.device_ref.device_id:
                raise ValueError(
                    "hardware inventory descriptor deviceId must match deviceRef"
                )
            if item.device_ref.fingerprint not in {None, item.descriptor.fingerprint}:
                raise ValueError(
                    "hardware inventory deviceRef fingerprint must match descriptor"
                )
        return self


class DeviceClaim(DeckrModel):
    claimed_by_endpoint: EndpointAddress = Field(alias="claimedByEndpoint")
    claimed_by_session_id: str = Field(alias="claimedBySessionId")
    timestamp: datetime
    ttl_seconds: int = Field(alias="ttlSeconds")

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def presence_endpoint_key(
    *,
    lane: str,
    endpoint: str | EndpointAddress,
) -> str:
    parsed = (
        endpoint
        if isinstance(endpoint, EndpointAddress)
        else EndpointAddress.model_validate(endpoint)
    )
    return ".".join(
        (
            "presence",
            "endpoint",
            encode_key_token(lane),
            encode_key_token(parsed.family),
            encode_key_token(parsed.endpoint_id),
        )
    )


def hardware_inventory_key(manager_id: str) -> str:
    return ".".join(("inventory", "hardware", encode_key_token(manager_id)))


def device_claim_key(*, manager_id: str, device_id: str) -> str:
    return ".".join(
        (
            "claim",
            "device",
            encode_key_token(manager_id),
            encode_key_token(device_id),
        )
    )


def plugin_action_catalog_key(host_id: str) -> str:
    return ".".join(("catalog", "plugin", encode_key_token(host_id)))


def parse_presence_endpoint_key(key: str) -> tuple[str, EndpointAddress] | None:
    parts = key.split(".")
    if len(parts) != 5 or parts[:2] != ["presence", "endpoint"]:
        return None
    lane = decode_key_token(parts[2])
    family = decode_key_token(parts[3])
    endpoint_id = decode_key_token(parts[4])
    return lane, EndpointAddress.model_validate(f"{family}:{endpoint_id}")


def parse_hardware_inventory_key(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[:2] != ["inventory", "hardware"]:
        return None
    return decode_key_token(parts[2])


def parse_device_claim_key(key: str) -> tuple[str, str] | None:
    parts = key.split(".")
    if len(parts) != 4 or parts[:2] != ["claim", "device"]:
        return None
    return decode_key_token(parts[2]), decode_key_token(parts[3])


def parse_plugin_action_catalog_key(key: str) -> str | None:
    parts = key.split(".")
    if len(parts) != 3 or parts[:2] != ["catalog", "plugin"]:
        return None
    return decode_key_token(parts[2])
