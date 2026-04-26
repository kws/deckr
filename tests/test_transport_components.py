from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    host_address,
    plugin_hosts_broadcast,
)
from deckr.core.component import RunContext
from deckr.core.components import ComponentContext, LaneRegistry, runtime_name_for
from deckr.hardware import events as hw_events
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


def _broadcast_plugin_message(message_type: str, value: int) -> DeckrMessage:
    return plugin_message(
        sender=controller_address("controller-main"),
        recipient=plugin_hosts_broadcast(),
        message_type=message_type,
        payload={"value": value},
        subject=entity_subject("test"),
    )


def _without_route(message: DeckrMessage) -> DeckrMessage:
    return message.model_copy(update={"route": None})


async def _server_client_id(server) -> str:
    with anyio.fail_after(3):
        while not server._connection_client_ids:
            await anyio.sleep(0.01)
    return next(iter(server._connection_client_ids.values()))


def _hardware_device() -> hw_events.HardwareDevice:
    return hw_events.HardwareDevice(
        id="deck",
        name="Deck",
        hid="hid:deck",
        fingerprint="serial:deck",
        slots=[],
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
    await plugin_bus.route_table.claim_endpoint(
        endpoint=controller_address("controller-main"),
        lane="plugin_messages",
        client_id=binding.client_id,
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="python-mqtt",
        claim_source="message_sender",
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(transport._bus_to_mqtt_loop, client, binding)
        await anyio.sleep(0.05)
        await plugin_bus.send(outbound_message)
        with anyio.fail_after(3):
            await client.published_event.wait()
        tg.cancel_scope.cancel()

    assert len(client.published) == 1
    assert client.published[0][0] == "deckr/v1"
    assert client.published[0][2] == 2
    published_frame = parse_mqtt_frame(json.loads(client.published[0][1]))
    assert _without_route(published_frame.message) == outbound_message
    assert published_frame.client_id == binding.client_id

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

    assert _without_route(received) == inbound_message
    assert received.route.current_client_id == binding.client_id


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

        async with (
            plugin_bus.subscribe() as stream,
            connect(f"ws://127.0.0.1:{unused_tcp_port}/plugin") as websocket,
        ):
            await websocket.send(
                json.dumps(build_websocket_frame("remote-ws", inbound_message))
            )

            received = await _next_message(stream, "host.event")
            assert _without_route(received) == inbound_message

            await plugin_bus.send(outbound_message)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert frame.transport_id == "controller-ws"
            assert _without_route(frame.message) == outbound_message
            assert frame.client_id is not None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_websocket_ingress_binding_does_not_forward_local_messages(
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
                        "direction": "ingress",
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

        async with (
            plugin_bus.subscribe() as stream,
            connect(f"ws://127.0.0.1:{unused_tcp_port}/plugin") as websocket,
        ):
            await websocket.send(
                json.dumps(build_websocket_frame("remote-ws", inbound_message))
            )
            received = await _next_message(stream, "host.event")
            assert _without_route(received) == inbound_message

            await plugin_bus.send(outbound_message)
            with anyio.move_on_after(0.1) as scope:
                await websocket.recv()
            assert scope.cancel_called

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_websocket_egress_binding_does_not_admit_remote_messages(
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
                        "direction": "egress",
                        "path": "/plugin",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )

    async with anyio.create_task_group() as tg:
        await server.start(RunContext(tg=tg, stopping=anyio.Event()))
        await anyio.sleep(0.1)

        async with (
            plugin_bus.subscribe() as stream,
            connect(f"ws://127.0.0.1:{unused_tcp_port}/plugin") as websocket,
        ):
            await websocket.send(
                json.dumps(
                    build_websocket_frame(
                        "remote-ws",
                        _plugin_message("host.event", 1),
                    )
                )
            )
            with anyio.move_on_after(0.1) as scope:
                await stream.receive()
            assert scope.cancel_called

            await plugin_bus.route_table.claim_endpoint(
                endpoint=host_address("python"),
                lane="plugin_messages",
                client_id=await _server_client_id(server),
                client_kind="remote",
                transport_kind="websocket",
                transport_id="controller-ws",
                claim_source="transport_route",
            )
            outbound_message = _broadcast_plugin_message("controller.event", 2)
            await plugin_bus.send(outbound_message)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert _without_route(frame.message) == outbound_message

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_websocket_client_ingress_binding_does_not_forward_local_messages(
    unused_tcp_port: int,
) -> None:
    plugin_bus = EventBus("plugin_messages")
    remote_received_send, remote_received = anyio.create_memory_object_stream[str](10)
    remote_connected = anyio.Event()
    inbound_message = _plugin_message("host.event", 1)

    async def remote_server(websocket) -> None:
        remote_connected.set()
        await websocket.send(
            json.dumps(build_websocket_frame("remote-ws", inbound_message))
        )
        async for raw in websocket:
            await remote_received_send.send(raw)

    server = await serve(remote_server, "127.0.0.1", unused_tcp_port)
    client = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "controller-ws",
                "mode": "client",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "direction": "ingress",
                        "uri": f"ws://127.0.0.1:{unused_tcp_port}/plugin",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )

    try:
        async with anyio.create_task_group() as tg:
            async with plugin_bus.subscribe() as stream:
                await client.start(RunContext(tg=tg, stopping=anyio.Event()))
                with anyio.fail_after(3):
                    await remote_connected.wait()

                received = await _next_message(stream, "host.event")
                assert _without_route(received) == inbound_message

                await plugin_bus.send(_broadcast_plugin_message("controller.event", 2))
                with anyio.move_on_after(0.1) as scope:
                    await remote_received.receive()
                assert scope.cancel_called

            tg.cancel_scope.cancel()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_websocket_client_egress_binding_does_not_admit_remote_messages(
    unused_tcp_port: int,
) -> None:
    plugin_bus = EventBus("plugin_messages")
    remote_received_send, remote_received = anyio.create_memory_object_stream[str](10)
    remote_connected = anyio.Event()

    async def remote_server(websocket) -> None:
        remote_connected.set()
        await websocket.send(
            json.dumps(
                build_websocket_frame("remote-ws", _plugin_message("host.event", 1))
            )
        )
        async for raw in websocket:
            await remote_received_send.send(raw)

    server = await serve(remote_server, "127.0.0.1", unused_tcp_port)
    client = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "controller-ws",
                "mode": "client",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "direction": "egress",
                        "uri": f"ws://127.0.0.1:{unused_tcp_port}/plugin",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )

    try:
        async with anyio.create_task_group() as tg:
            async with plugin_bus.subscribe() as stream:
                await client.start(RunContext(tg=tg, stopping=anyio.Event()))
                with anyio.fail_after(3):
                    await remote_connected.wait()

                with anyio.move_on_after(0.1) as scope:
                    await stream.receive()
                assert scope.cancel_called

                await plugin_bus.route_table.claim_endpoint(
                    endpoint=host_address("python"),
                    lane="plugin_messages",
                    client_id=client._bindings[0].client_id,
                    client_kind="remote",
                    transport_kind="websocket",
                    transport_id="controller-ws",
                    claim_source="transport_route",
                )
                outbound_message = _broadcast_plugin_message("controller.event", 2)
                await plugin_bus.send(outbound_message)
                frame = parse_websocket_frame(
                    json.loads(await remote_received.receive())
                )
                assert _without_route(frame.message) == outbound_message

            tg.cancel_scope.cancel()
    finally:
        server.close()
        await server.wait_closed()


def test_transport_frames_validate_deckr_messages() -> None:
    message = _plugin_message("schema.check", 3)
    assert parse_mqtt_frame(build_mqtt_frame("mqtt-a", message)).message == message
    assert (
        parse_websocket_frame(build_websocket_frame("ws-a", message)).message == message
    )


def test_extension_lane_bindings_require_schema_id() -> None:
    with pytest.raises(ValueError, match="requires schema_id"):
        mqtt_transport_component.lanes_for(
            raw_config={
                "hostname": "mqtt.example.net",
                "bindings": {
                    "metrics": {
                        "lane": "acme.metrics.events",
                        "topic": "deckr/metrics",
                    }
                },
            },
            instance_id="main",
        )

    with pytest.raises(ValueError, match="requires schema_id"):
        websocket_transport_component.lanes_for(
            raw_config={
                "mode": "server",
                "bindings": {
                    "metrics": {
                        "lane": "acme.metrics.events",
                        "path": "/metrics",
                    }
                },
            },
            instance_id="main",
        )


def test_core_lane_bindings_use_deckr_schema_contracts() -> None:
    lanes = mqtt_transport_component.lanes_for(
        raw_config={
            "hostname": "mqtt.example.net",
            "bindings": {
                "plugin": {
                    "lane": "plugin_messages",
                    "topic": "deckr/plugin",
                }
            },
        },
        instance_id="main",
    )
    assert lanes.consumes == ("plugin_messages",)
    assert lanes.publishes == ("plugin_messages",)

    with pytest.raises(ValueError, match="core lane 'plugin_messages'"):
        websocket_transport_component.lanes_for(
            raw_config={
                "mode": "server",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "schema_id": "acme.override.v1",
                        "path": "/plugin",
                    }
                },
        },
        instance_id="main",
    )


def test_websocket_server_rejects_duplicate_path_lane_bindings() -> None:
    with pytest.raises(ValueError, match="duplicate path/lane"):
        websocket_transport_component.lanes_for(
            raw_config={
                "mode": "server",
                "bindings": {
                    "plugin-a": {
                        "lane": "plugin_messages",
                        "path": "plugin",
                    },
                    "plugin-b": {
                        "lane": "plugin_messages",
                        "path": "/plugin",
                    },
                },
            },
            instance_id="main",
        )


def test_websocket_server_allows_non_duplicate_path_lane_bindings() -> None:
    lanes = websocket_transport_component.lanes_for(
        raw_config={
            "mode": "server",
            "bindings": {
                "plugin": {
                    "lane": "plugin_messages",
                    "path": "/shared",
                },
                "hardware": {
                    "lane": "hardware_events",
                    "path": "/shared",
                },
                "plugin-alt": {
                    "lane": "plugin_messages",
                    "path": "/plugin-alt",
                },
            },
        },
        instance_id="main",
    )

    assert lanes.consumes == ("hardware_events", "plugin_messages")
    assert lanes.publishes == ("hardware_events", "plugin_messages")


@pytest.mark.asyncio
async def test_websocket_hardware_manager_uses_same_envelope_and_route_semantics(
    unused_tcp_port: int,
) -> None:
    device = _hardware_device()
    manager_endpoint = hardware_manager_address("room-a")
    connected = hw_events.hardware_input_message(
        manager_id="room-a",
        device_id=device.id,
        body=hw_events.DeviceConnectedMessage(device=device),
    )

    local_bus = EventBus("hardware_events")
    await local_bus.claim_local_endpoint(manager_endpoint)
    async with local_bus.subscribe() as stream:
        await local_bus.send(connected)
        assert await stream.receive() == connected
    route = await local_bus.route_table.route_for(
        manager_endpoint,
        lane="hardware_events",
    )
    assert route.client_kind == "local"

    hardware_bus = EventBus("hardware_events")
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

    async with anyio.create_task_group() as tg:
        await server.start(RunContext(tg=tg, stopping=anyio.Event()))
        await anyio.sleep(0.1)

        async with (
            hardware_bus.subscribe() as stream,
            connect(f"ws://127.0.0.1:{unused_tcp_port}/hardware") as websocket,
        ):
            await websocket.send(
                json.dumps(build_websocket_frame("manager-ws", connected))
            )
            received = await _next_message(stream, hw_events.DEVICE_CONNECTED)
            assert _without_route(received) == connected
            route = await hardware_bus.route_table.route_for(
                manager_endpoint,
                lane="hardware_events",
            )
            assert route is not None
            assert route.client_kind == "remote"

            command = hw_events.hardware_command_message(
                controller_id="controller-main",
                manager_id="room-a",
                message_type=hw_events.CLEAR_SLOT,
                device_id=device.id,
                control_id="0,0",
                control_kind="slot",
                body=hw_events.ClearSlotMessage(slot_id="0,0"),
            )
            await hardware_bus.send(command)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert _without_route(frame.message) == command
            assert frame.message.recipient.endpoint == manager_endpoint
            assert hw_events.hardware_control_ref_from_subject(
                frame.message.subject
            ) == hw_events.HardwareControlRef(
                manager_id="room-a",
                device_id="deck",
                control_id="0,0",
                control_kind="slot",
            )

        tg.cancel_scope.cancel()
