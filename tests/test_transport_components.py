from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from websockets.asyncio.client import connect

from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    host_address,
)
from deckr.core.component import RunContext
from deckr.core.components import ComponentContext, LaneRegistry, runtime_name_for
from deckr.pluginhost.messages import plugin_message
from deckr.transports.bus import EventBus
from deckr.transports.mqtt import (
    build_mqtt_frame,
    parse_mqtt_frame,
)
from deckr.transports.mqtt import (
    component as mqtt_transport_component,
)
from deckr.transports.websocket import (
    build_websocket_frame,
    parse_websocket_frame,
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


async def _next_message(stream, message_type: str) -> DeckrMessage:
    with anyio.fail_after(3):
        while True:
            message = await stream.receive()
            if message.message_type == message_type:
                return message


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


def _plugin_message(message_type: str, value: int) -> DeckrMessage:
    return plugin_message(
        sender=host_address("python"),
        recipient=controller_address("controller-main"),
        message_type=message_type,
        payload={"value": value},
        subject=entity_subject("test"),
    )


@pytest.mark.asyncio
async def test_mqtt_transport_forwards_deckr_messages_unchanged() -> None:
    plugin_bus = EventBus("plugin_messages")
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
    outbound_message = _plugin_message("deckr.ready", 1)

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
            json.dumps(build_mqtt_frame("python-mqtt", outbound_message)),
            2,
        )
    ]

    inbound_message = _plugin_message("deckr.inbound", 7)
    async with (
        plugin_bus.subscribe() as stream,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(transport._mqtt_to_bus_loop, client, binding)
        await client.messages.inject(
            json.dumps(build_mqtt_frame("remote-mqtt", inbound_message)),
            topic="deckr/v1",
        )
        received = await _next_message(stream, "deckr.inbound")
        tg.cancel_scope.cancel()

    assert received == inbound_message


@pytest.mark.asyncio
async def test_websocket_transport_frames_deckr_messages(
    unused_tcp_port: int,
) -> None:
    plugin_bus = EventBus("plugin_messages")
    server = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "controller-ws",
                "mode": "server",
                "host": "127.0.0.1",
                "port": unused_tcp_port,
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "path": "/plugin",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )

    inbound_message = _plugin_message("host.event", 1)
    outbound_message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type="controller.event",
        payload={"value": 2},
        subject=entity_subject("test"),
    )

    async with anyio.create_task_group() as tg:
        await server.start(RunContext(tg=tg, stopping=anyio.Event()))
        await anyio.sleep(0.1)

        async with plugin_bus.subscribe() as stream, connect(
            f"ws://127.0.0.1:{unused_tcp_port}/plugin"
        ) as websocket:
            await websocket.send(
                json.dumps(build_websocket_frame("remote-ws", inbound_message))
            )

            received = await _next_message(stream, "host.event")
            assert received == inbound_message

            await plugin_bus.send(outbound_message)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert frame.transport_id == "controller-ws"
            assert frame.message == outbound_message

        tg.cancel_scope.cancel()


def test_transport_frames_validate_deckr_messages() -> None:
    message = _plugin_message("schema.check", 3)
    assert parse_mqtt_frame(build_mqtt_frame("mqtt-a", message)).message == message
    assert parse_websocket_frame(
        build_websocket_frame("ws-a", message)
    ).message == message
