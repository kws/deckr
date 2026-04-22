from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from deckr.core._bridge import (
    event_has_bridge_metadata,
    event_originated_from_bridge,
    mark_event_from_bridge,
)
from deckr.core.messaging import EventBus
from deckr.hardware import events as hw_events
from deckr.plugin.messages import HostMessage

logger = logging.getLogger(__name__)

SendRemote = Callable[[dict[str, Any], str | None], Awaitable[None]]


class LaneHandler(Protocol):
    async def handle_local_event(self, event: Any, *, send_remote: SendRemote) -> None: ...

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_bridge_id: str | None,
    ) -> None: ...

    async def handle_transport_disconnect(
        self,
        *,
        remote_bridge_id: str | None,
    ) -> None: ...


class PluginMessagesLaneHandler:
    def __init__(self, *, bridge_id: str, bus: EventBus) -> None:
        self._bridge_id = bridge_id
        self._bus = bus

    async def handle_local_event(self, event: Any, *, send_remote: SendRemote) -> None:
        if not isinstance(event, HostMessage):
            return
        if event_originated_from_bridge(event, self._bridge_id):
            return
        await send_remote(event.to_dict(), None)

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_bridge_id: str | None,
    ) -> None:
        del send_remote, remote_bridge_id
        message = HostMessage.from_dict(payload)
        await self._bus.send(mark_event_from_bridge(message, self._bridge_id))

    async def handle_transport_disconnect(
        self,
        *,
        remote_bridge_id: str | None,
    ) -> None:
        del remote_bridge_id
        return


@dataclass(slots=True)
class _BridgedHWDevice:
    remote_device_id: str
    target_bridge_id: str | None
    hid: str
    slots: list[hw_events.HWSlot]
    name: str | None
    _send_remote: SendRemote

    @property
    def id(self) -> str:
        return self.remote_device_id

    async def _send_command(self, command: hw_events.HardwareOutputCommand) -> None:
        message = hw_events.command_to_transport_message(command)
        await self._send_remote(
            hw_events.hardware_message_to_wire(message),
            self.target_bridge_id,
        )

    async def set_image(self, slot_id: str, image: bytes) -> None:
        await self._send_command(
            hw_events.SetImageCommand(
                device_id=self.remote_device_id,
                slot_id=slot_id,
                image=image,
            )
        )

    async def clear_slot(self, slot_id: str) -> None:
        await self._send_command(
            hw_events.ClearSlotCommand(
                device_id=self.remote_device_id,
                slot_id=slot_id,
            )
        )

    async def sleep_screen(self) -> None:
        await self._send_command(
            hw_events.SleepScreenCommand(device_id=self.remote_device_id)
        )

    async def wake_screen(self) -> None:
        await self._send_command(
            hw_events.WakeScreenCommand(device_id=self.remote_device_id)
        )


class HardwareEventsLaneHandler:
    def __init__(self, *, bridge_id: str, bus: EventBus) -> None:
        self._bridge_id = bridge_id
        self._bus = bus
        self._local_devices: dict[str, hw_events.HWDevice] = {}
        self._remote_proxies: dict[str, _BridgedHWDevice] = {}

    async def handle_local_event(self, event: Any, *, send_remote: SendRemote) -> None:
        if not isinstance(
            event,
            (
                hw_events.DeviceConnectedEvent,
                hw_events.DeviceDisconnectedEvent,
                hw_events.KeyDownEvent,
                hw_events.KeyUpEvent,
                hw_events.DialRotateEvent,
                hw_events.TouchTapEvent,
                hw_events.TouchSwipeEvent,
            ),
        ):
            return
        if event_has_bridge_metadata(event):
            return

        remote_device_id = hw_events.build_remote_device_id(self._bridge_id, event.device_id)
        if isinstance(event, hw_events.DeviceConnectedEvent):
            self._local_devices[remote_device_id] = event.device
            message = hw_events.DeviceConnectedMessage(
                device_id=remote_device_id,
                device=hw_events.device_info_to_wire(event.device),
            )
        elif isinstance(event, hw_events.DeviceDisconnectedEvent):
            self._local_devices.pop(remote_device_id, None)
            message = hw_events.DeviceDisconnectedMessage(device_id=remote_device_id)
        elif isinstance(event, hw_events.KeyDownEvent):
            message = hw_events.KeyDownMessage(
                device_id=remote_device_id,
                key_id=event.key_id,
            )
        elif isinstance(event, hw_events.KeyUpEvent):
            message = hw_events.KeyUpMessage(
                device_id=remote_device_id,
                key_id=event.key_id,
            )
        elif isinstance(event, hw_events.DialRotateEvent):
            message = hw_events.DialRotateMessage(
                device_id=remote_device_id,
                dial_id=event.dial_id,
                direction=event.direction,
            )
        elif isinstance(event, hw_events.TouchTapEvent):
            message = hw_events.TouchTapMessage(
                device_id=remote_device_id,
                touch_id=event.touch_id,
            )
        else:
            message = hw_events.TouchSwipeMessage(
                device_id=remote_device_id,
                touch_id=event.touch_id,
                direction=event.direction,
            )
        await send_remote(hw_events.hardware_message_to_wire(message), None)

    async def handle_remote_payload(
        self,
        payload: dict[str, Any],
        *,
        send_remote: SendRemote,
        remote_bridge_id: str | None,
    ) -> None:
        message = hw_events.hardware_message_from_wire(payload)
        if isinstance(
            message,
            (
                hw_events.DeviceConnectedMessage,
                hw_events.DeviceDisconnectedMessage,
                hw_events.KeyDownMessage,
                hw_events.KeyUpMessage,
                hw_events.DialRotateMessage,
                hw_events.TouchTapMessage,
                hw_events.TouchSwipeMessage,
            ),
        ):
            await self._publish_remote_event(
                message,
                send_remote=send_remote,
                remote_bridge_id=remote_bridge_id,
            )
            return

        await self._apply_remote_command(message)

    async def _publish_remote_event(
        self,
        message: hw_events.HardwareTransportMessage,
        *,
        send_remote: SendRemote,
        remote_bridge_id: str | None,
    ) -> None:
        if isinstance(message, hw_events.DeviceConnectedMessage):
            parsed = hw_events.parse_remote_device_id(message.device_id)
            target_bridge_id = parsed.get("manager_id") or remote_bridge_id
            info = hw_events.device_info_from_wire(message.device)
            proxy = _BridgedHWDevice(
                remote_device_id=message.device_id,
                target_bridge_id=target_bridge_id,
                hid=f"bridge:{target_bridge_id or 'unknown'}:{info.hid}",
                slots=list(info.slots),
                name=info.name,
                _send_remote=send_remote,
            )
            self._remote_proxies[message.device_id] = proxy
            event = hw_events.DeviceConnectedEvent(
                device_id=message.device_id,
                device=proxy,
            )
            await self._bus.send(mark_event_from_bridge(event, self._bridge_id))
            return

        if isinstance(message, hw_events.DeviceDisconnectedMessage):
            self._remote_proxies.pop(message.device_id, None)
            event = hw_events.DeviceDisconnectedEvent(device_id=message.device_id)
            await self._bus.send(mark_event_from_bridge(event, self._bridge_id))
            return

        event = hw_events.transport_message_to_event(message, device_id=message.device_id)
        await self._bus.send(mark_event_from_bridge(event, self._bridge_id))

    async def _apply_remote_command(
        self,
        message: hw_events.HardwareTransportMessage,
    ) -> None:
        remote_device_id = getattr(message, "device_id", None)
        if remote_device_id is None:
            return

        parsed = hw_events.parse_remote_device_id(remote_device_id)
        manager_id = parsed.get("manager_id")
        local_device_id = parsed.get("device_id")
        if manager_id != self._bridge_id or not local_device_id:
            return

        device = self._local_devices.get(remote_device_id)
        if device is None:
            logger.warning(
                "Ignoring bridged hardware command for unknown local device %s",
                remote_device_id,
            )
            return

        command = hw_events.transport_message_to_command(message, device_id=local_device_id)
        await hw_events.apply_command(device, command)

    async def handle_transport_disconnect(
        self,
        *,
        remote_bridge_id: str | None,
    ) -> None:
        if remote_bridge_id is None:
            return

        disconnected_device_ids = [
            device_id
            for device_id, proxy in self._remote_proxies.items()
            if proxy.target_bridge_id == remote_bridge_id
        ]
        for device_id in disconnected_device_ids:
            self._remote_proxies.pop(device_id, None)
            event = hw_events.DeviceDisconnectedEvent(device_id=device_id)
            await self._bus.send(mark_event_from_bridge(event, self._bridge_id))


def build_lane_handler(*, lane: str, bridge_id: str, bus: EventBus) -> LaneHandler:
    if lane == "plugin_messages":
        return PluginMessagesLaneHandler(bridge_id=bridge_id, bus=bus)
    if lane == "hardware_events":
        return HardwareEventsLaneHandler(bridge_id=bridge_id, bus=bus)
    raise ValueError(f"Unsupported bridge lane {lane!r}")
