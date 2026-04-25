from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from deckr.hardware import events as hw_events
from deckr.plugin.messages import HostMessage
from deckr.transports.bus import (
    TRANSPORT_ID_HEADER,
    TRANSPORT_KIND_HEADER,
    TRANSPORT_LANE_HEADER,
    TRANSPORT_REMOTE_ID_HEADER,
    EventBus,
    TransportEnvelope,
)

SendRemote = Callable[[dict[str, Any], str | None], Awaitable[None]]


class LaneHandler(Protocol):
    async def handle_local_event(
        self,
        envelope: TransportEnvelope,
        *,
        send_remote: SendRemote,
    ) -> None: ...

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_transport_id: str | None,
    ) -> None: ...

    async def handle_transport_disconnect(
        self,
        *,
        remote_transport_id: str | None,
    ) -> None: ...


class PluginMessagesLaneHandler:
    def __init__(self, *, transport_kind: str, transport_id: str, bus: EventBus) -> None:
        self._transport_kind = transport_kind
        self._transport_id = transport_id
        self._bus = bus

    async def handle_local_event(
        self,
        envelope: TransportEnvelope,
        *,
        send_remote: SendRemote,
    ) -> None:
        message = envelope.message
        if not isinstance(message, HostMessage):
            return
        if envelope.headers.get(TRANSPORT_ID_HEADER) == self._transport_id:
            return
        await send_remote(message.to_dict(), None)

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_transport_id: str | None,
    ) -> None:
        del send_remote
        message = HostMessage.from_dict(payload)
        await self._bus.send(
            message,
            headers=self._headers(remote_transport_id=remote_transport_id),
        )

    async def handle_transport_disconnect(
        self,
        *,
        remote_transport_id: str | None,
    ) -> None:
        del remote_transport_id
        return

    def _headers(self, *, remote_transport_id: str | None) -> dict[str, str]:
        headers = {
            TRANSPORT_KIND_HEADER: self._transport_kind,
            TRANSPORT_ID_HEADER: self._transport_id,
            TRANSPORT_LANE_HEADER: "plugin_messages",
        }
        if remote_transport_id is not None:
            headers[TRANSPORT_REMOTE_ID_HEADER] = remote_transport_id
        return headers


class HardwareEventsLaneHandler:
    def __init__(self, *, transport_kind: str, transport_id: str, bus: EventBus) -> None:
        self._transport_kind = transport_kind
        self._transport_id = transport_id
        self._bus = bus
        self._remote_device_targets: dict[str, str | None] = {}

    async def handle_local_event(
        self,
        envelope: TransportEnvelope,
        *,
        send_remote: SendRemote,
    ) -> None:
        message = envelope.message
        if not isinstance(message, self._message_types()):
            return
        if TRANSPORT_ID_HEADER in envelope.headers:
            return

        if isinstance(message, hw_events.HARDWARE_INPUT_MESSAGE_TYPES):
            message = self._namespace_local_input(message)
            target_transport_id = None
        else:
            target_transport_id = self._target_transport_id(message)
        await send_remote(
            hw_events.hardware_message_to_wire(message),
            target_transport_id,
        )

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_transport_id: str | None,
    ) -> None:
        del send_remote
        message = hw_events.hardware_message_from_wire(payload)
        if isinstance(message, hw_events.HARDWARE_INPUT_MESSAGE_TYPES):
            await self._publish_remote_input(message, remote_transport_id=remote_transport_id)
            return

        await self._publish_remote_command(message)

    async def _publish_remote_input(
        self,
        message: hw_events.HardwareTransportMessage,
        *,
        remote_transport_id: str | None,
    ) -> None:
        if isinstance(message, hw_events.DeviceConnectedMessage):
            self._remote_device_targets[message.device_id] = self._target_transport_id(
                message
            ) or remote_transport_id
        elif isinstance(message, hw_events.DeviceDisconnectedMessage):
            self._remote_device_targets.pop(message.device_id, None)
        await self._bus.send(
            message,
            headers=self._headers(remote_transport_id=remote_transport_id),
        )

    async def _publish_remote_command(
        self,
        message: hw_events.HardwareTransportMessage,
    ) -> None:
        parsed = hw_events.parse_remote_device_id(message.device_id)
        manager_id = parsed.get("manager_id")
        local_device_id = parsed.get("device_id")
        if manager_id is not None:
            if manager_id != self._transport_id or not local_device_id:
                return
            message = self._with_device_id(message, local_device_id)
        await self._bus.send(
            message,
            headers=self._headers(remote_transport_id=None),
        )

    async def handle_transport_disconnect(
        self,
        *,
        remote_transport_id: str | None,
    ) -> None:
        if remote_transport_id is None:
            return

        disconnected_device_ids = [
            device_id
            for device_id, target_transport_id in self._remote_device_targets.items()
            if target_transport_id == remote_transport_id
        ]
        for device_id in disconnected_device_ids:
            self._remote_device_targets.pop(device_id, None)
            message = hw_events.DeviceDisconnectedMessage(device_id=device_id)
            await self._bus.send(
                message,
                headers=self._headers(remote_transport_id=remote_transport_id),
            )

    @staticmethod
    def _message_types() -> tuple[type, ...]:
        return (
            *hw_events.HARDWARE_INPUT_MESSAGE_TYPES,
            *hw_events.HARDWARE_COMMAND_MESSAGE_TYPES,
        )

    def _namespace_local_input(
        self,
        message: hw_events.HardwareTransportMessage,
    ) -> hw_events.HardwareTransportMessage:
        parsed = hw_events.parse_remote_device_id(message.device_id)
        if parsed.get("manager_id") is not None:
            return message
        return self._with_device_id(
            message,
            hw_events.build_remote_device_id(self._transport_id, message.device_id),
        )

    @staticmethod
    def _with_device_id(
        message: hw_events.HardwareTransportMessage,
        device_id: str,
    ) -> hw_events.HardwareTransportMessage:
        if message.device_id == device_id:
            return message
        return message.model_copy(update={"device_id": device_id})

    @staticmethod
    def _target_transport_id(
        message: hw_events.HardwareTransportMessage,
    ) -> str | None:
        parsed = hw_events.parse_remote_device_id(message.device_id)
        return parsed.get("manager_id")

    def _headers(self, *, remote_transport_id: str | None) -> dict[str, str]:
        headers = {
            TRANSPORT_KIND_HEADER: self._transport_kind,
            TRANSPORT_ID_HEADER: self._transport_id,
            TRANSPORT_LANE_HEADER: "hardware_events",
        }
        if remote_transport_id is not None:
            headers[TRANSPORT_REMOTE_ID_HEADER] = remote_transport_id
        return headers


def build_lane_handler(
    *,
    lane: str,
    transport_kind: str,
    transport_id: str,
    bus: EventBus,
) -> LaneHandler:
    if lane == "plugin_messages":
        return PluginMessagesLaneHandler(
            transport_kind=transport_kind,
            transport_id=transport_id,
            bus=bus,
        )
    if lane == "hardware_events":
        return HardwareEventsLaneHandler(
            transport_kind=transport_kind,
            transport_id=transport_id,
            bus=bus,
        )
    raise ValueError(f"Unsupported transport lane {lane!r}")
