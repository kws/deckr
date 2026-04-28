from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import anyio
from pydantic import Field, field_serializer, field_validator

from deckr.contracts.messages import EndpointAddress
from deckr.contracts.models import DeckrModel, freeze_json, thaw_json

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


def state_expires_at(
    value: Mapping[str, Any],
    *,
    ttl: float | None = None,
    now: datetime | None = None,
) -> datetime | None:
    timestamp = value.get("timestamp")
    ttl_seconds = value.get("ttlSeconds", value.get("ttl_seconds"))
    payload_deadline: datetime | None = None
    if timestamp is not None and ttl_seconds is not None:
        parsed = _parse_timestamp(timestamp)
        try:
            seconds = float(ttl_seconds)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            payload_deadline = parsed + timedelta(seconds=seconds)

    ttl_deadline = None
    if ttl is not None and ttl > 0:
        ttl_deadline = (now or datetime.now(UTC)) + timedelta(seconds=ttl)

    deadlines = [deadline for deadline in (payload_deadline, ttl_deadline) if deadline]
    if not deadlines:
        return None
    return min(deadlines)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be an ISO-8601 string or datetime")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


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
    device_id: str = Field(alias="deviceId")
    hardware_type: str = Field(alias="hardwareType")
    fingerprint: str


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


class DeviceClaim(DeckrModel):
    claimed_by_endpoint: EndpointAddress = Field(alias="claimedByEndpoint")
    claimed_by_session_id: str = Field(alias="claimedBySessionId")
    timestamp: datetime
    ttl_seconds: int = Field(alias="ttlSeconds")

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
