from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from deckr.core.component import RunContext
from deckr.core.components import ComponentContext, LaneRegistry, runtime_name_for
from deckr.hardware import events as hw_events
from deckr.plugin.messages import HostMessage
from deckr.transports.bus import (
    TRANSPORT_ID_HEADER,
    TRANSPORT_KIND_HEADER,
    TRANSPORT_LANE_HEADER,
    TRANSPORT_REMOTE_ID_HEADER,
    EventBus,
)
from deckr.transports.mqtt import (
    build_mqtt_envelope,
)
from deckr.transports.mqtt import (
    component as mqtt_transport_component,
)
from deckr.transports.websocket import (
    build_websocket_envelope,
    parse_websocket_envelope,
)
from deckr.transports.websocket import (
    component as websocket_transport_component,
)


def _component_context(
    definition,
    *,
    raw_config: dict,
    lanes: dict[str, EventBus],
    instance_id: str = "main",
) -> ComponentContext:
    return ComponentContext(
        component_id=definition.manifest.component_id,
        instance_id=instance_id,
        runtime_name=runtime_name_for(definition.manifest.component_id, instance_id),
        manifest=definition.manifest,
        raw_config=raw_config,
        base_dir=Path.cwd(),
        lanes=LaneRegistry(lanes),
    )


async def _next_envelope(stream, event_type):
    with anyio.fail_after(3):
        while True:
            envelope = await stream.receive()
            if isinstance(envelope.message, event_type):
                return envelope


async def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


class StubDevice:
    def __init__(self, device_id: str = "virtual-1") -> None:
        self.id = device_id
        self.hid = f"virtual:{device_id}"
        self.name = "Virtual Device"
        self.slots = [
            hw_events.WireHWSlot(
                id="0,0",
                coordinates=hw_events.WireCoordinates(column=0, row=0),
                image_format=hw_events.WireHWSImageFormat(width=72, height=72),
                gestures=["key_down", "key_up"],
            )
        ]
        self.commands: list[tuple[str, object]] = []

    async def set_image(self, slot_id: str, image: bytes) -> None:
        self.commands.append(("set_image", slot_id, image))

    async def clear_slot(self, slot_id: str) -> None:
        self.commands.append(("clear_slot", slot_id))

    async def sleep_screen(self) -> None:
        self.commands.append(("sleep_screen",))

    async def wake_screen(self) -> None:
        self.commands.append(("wake_screen",))


def _device_info(device: StubDevice) -> hw_events.WireHWDevice:
    return hw_events.WireHWDevice(
        id=device.id,
        hid=device.hid,
        slots=list(device.slots),
        name=device.name,
    )


class _FakeMqttMessages:
    def __init__(self) -> None:
        self._send, self._receive = anyio.create_memory_object_stream(10)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._receive.receive()

    async def inject(self, payload: str, *, topic: str) -> None:
        await self._send.send(SimpleNamespace(payload=payload, topic=topic))


class _FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int]] = []
        self.published_event = anyio.Event()
        self.messages = _FakeMqttMessages()

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))
        self.published_event.set()


def _host_message(message_type: str, *, payload: dict | None = None) -> HostMessage:
    return HostMessage(
        from_id="host:test",
        to_id="controller:test",
        type=message_type,
        payload=payload or {},
    )


@pytest.mark.asyncio
async def test_websocket_transport_server_handles_remote_hardware_devices(
    unused_tcp_port: int,
) -> None:
    hardware_bus = EventBus()
    server = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "controller-ws",
                "mode": "server",
                "host": "127.0.0.1",
                "port": unused_tcp_port,
                "bindings": {
                    "hardware": {
                        "lane": "hardware_events",
                        "path": "/hardware",
                    }
                },
            },
            lanes={"hardware_events": hardware_bus},
        )
    )

    remote_device_id = hw_events.build_remote_device_id("bedroom-pi", "virtual-1")
    device = StubDevice()

    async with anyio.create_task_group() as tg:
        await server.start(RunContext(tg=tg, stopping=anyio.Event()))
        await _wait_for(lambda: server._server is not None)

        async with hardware_bus.subscribe() as stream:
            async with connect(f"ws://127.0.0.1:{unused_tcp_port}/hardware") as websocket:
                await websocket.send(
                    json.dumps(
                        build_websocket_envelope(
                            "bedroom-pi",
                            "hardware_events",
                            hw_events.hardware_message_to_wire(
                                hw_events.DeviceConnectedMessage(
                                    device_id=remote_device_id,
                                    device=_device_info(device),
                                )
                            ),
                        )
                    )
                )

                connected_envelope = await _next_envelope(
                    stream, hw_events.DeviceConnectedMessage
                )
                connected = connected_envelope.message
                assert connected.device_id == remote_device_id
                assert connected_envelope.headers[TRANSPORT_KIND_HEADER] == "websocket"
                assert connected_envelope.headers[TRANSPORT_ID_HEADER] == "controller-ws"
                assert (
                    connected_envelope.headers[TRANSPORT_REMOTE_ID_HEADER]
                    == "bedroom-pi"
                )
                assert (
                    connected_envelope.headers[TRANSPORT_LANE_HEADER]
                    == "hardware_events"
                )

                await hardware_bus.send(
                    hw_events.SetImageMessage(
                        device_id=remote_device_id,
                        slot_id="0,0",
                        image=b"\x01\x02\x03",
                    )
                )
                _, lane, payload = parse_websocket_envelope(
                    json.loads(await websocket.recv())
                )
                command = hw_events.hardware_message_from_wire(payload)
                assert lane == "hardware_events"
                assert isinstance(command, hw_events.SetImageMessage)
                assert command.device_id == remote_device_id
                assert command.slot_id == "0,0"
                assert command.image == b"\x01\x02\x03"

                await websocket.send(
                    json.dumps(
                        build_websocket_envelope(
                            "bedroom-pi",
                            "hardware_events",
                            hw_events.hardware_message_to_wire(
                                hw_events.KeyDownMessage(
                                    device_id=remote_device_id,
                                    key_id="0,0",
                                )
                            ),
                        )
                    )
                )
                key_down = (
                    await _next_envelope(stream, hw_events.KeyDownMessage)
                ).message
                assert key_down.device_id == remote_device_id
                assert key_down.key_id == "0,0"

            disconnected = (
                await _next_envelope(stream, hw_events.DeviceDisconnectedMessage)
            ).message
            assert disconnected.device_id == remote_device_id

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_websocket_transport_client_applies_commands_to_local_devices(
    unused_tcp_port: int,
) -> None:
    hardware_bus = EventBus()
    client = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "bedroom-pi",
                "mode": "client",
                "bindings": {
                    "hardware": {
                        "lane": "hardware_events",
                        "uri": f"ws://127.0.0.1:{unused_tcp_port}/hardware",
                    }
                },
            },
            lanes={"hardware_events": hardware_bus},
        )
    )

    local_device = StubDevice()
    remote_device_id = hw_events.build_remote_device_id("bedroom-pi", local_device.id)
    received_connected = anyio.Event()

    async def handler(websocket) -> None:
        raw = json.loads(await websocket.recv())
        remote_transport_id, lane, payload = parse_websocket_envelope(raw)
        message = hw_events.hardware_message_from_wire(payload)
        assert remote_transport_id == "bedroom-pi"
        assert lane == "hardware_events"
        assert isinstance(message, hw_events.DeviceConnectedMessage)
        assert message.device_id == remote_device_id
        received_connected.set()

        await websocket.send(
            json.dumps(
                build_websocket_envelope(
                    "controller-ws",
                    "hardware_events",
                    hw_events.hardware_message_to_wire(
                        hw_events.SetImageMessage(
                            device_id=remote_device_id,
                            slot_id="0,0",
                            image=b"\x10\x20",
                        )
                    ),
                )
            )
        )
        await anyio.sleep(0.1)

    async with (
        serve(handler, "127.0.0.1", unused_tcp_port),
        hardware_bus.subscribe() as stream,
        anyio.create_task_group() as tg,
    ):
        await client.start(RunContext(tg=tg, stopping=anyio.Event()))
        await anyio.sleep(0.1)

        await hardware_bus.send(
            hw_events.DeviceConnectedMessage(
                device_id=local_device.id,
                device=_device_info(local_device),
            )
        )

        with anyio.fail_after(3):
            await received_connected.wait()

        command = (await _next_envelope(stream, hw_events.SetImageMessage)).message
        assert command.device_id == local_device.id
        assert command.slot_id == "0,0"
        assert command.image == b"\x10\x20"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_mqtt_transport_forwards_plugin_messages() -> None:
    plugin_bus = EventBus()
    transport = mqtt_transport_component.factory(
        _component_context(
            mqtt_transport_component,
            raw_config={
                "transport_id": "python-mqtt",
                "hostname": "mqtt.example.net",
                "port": 1883,
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/v1",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )
    binding = transport._bindings[0]
    client = _FakeMqttClient()
    outbound_message = _host_message("deckr.ready", payload={"value": 1})

    async with anyio.create_task_group() as tg:
        tg.start_soon(transport._bus_to_mqtt_loop, client, binding)
        await anyio.sleep(0.05)
        await plugin_bus.send(outbound_message)
        with anyio.fail_after(3):
            await client.published_event.wait()
        tg.cancel_scope.cancel()

    assert client.published == [
        (
            "deckr/v1",
            json.dumps(
                build_mqtt_envelope(
                    "python-mqtt",
                    "plugin_messages",
                    outbound_message.to_dict(),
                )
            ),
            2,
        )
    ]

    inbound_message = _host_message("deckr.inbound", payload={"value": 7})
    async with (
        plugin_bus.subscribe() as stream,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(transport._mqtt_to_bus_loop, client, binding)
        await client.messages.inject(
            json.dumps(
                build_mqtt_envelope(
                    "remote-mqtt",
                    "plugin_messages",
                    inbound_message.to_dict(),
                )
            ),
            topic="deckr/v1",
        )
        received_envelope = await _next_envelope(stream, HostMessage)
        received = received_envelope.message
        tg.cancel_scope.cancel()

    assert received.type == "deckr.inbound"
    assert received.payload == {"value": 7}
    assert received_envelope.headers[TRANSPORT_KIND_HEADER] == "mqtt"
    assert received_envelope.headers[TRANSPORT_ID_HEADER] == "python-mqtt"
    assert received_envelope.headers[TRANSPORT_REMOTE_ID_HEADER] == "remote-mqtt"
    assert received_envelope.headers[TRANSPORT_LANE_HEADER] == "plugin_messages"
