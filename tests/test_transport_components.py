from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from deckr.components import (
    ComponentContext,
    LaneRegistry,
    RunContext,
    runtime_name_for,
)
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    host_address,
    plugin_hosts_broadcast,
)
from deckr.hardware import messages as hw_messages
from deckr.pluginhost.messages import (
    HOST_OFFLINE,
    HOST_ONLINE,
    REQUEST_ACTIONS,
    plugin_message,
)
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


async def _next_route_event(stream, event_type: str):
    with anyio.fail_after(3):
        while True:
            event = await stream.receive()
            if event.event_type == event_type:
                return event


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

    async def publish(
        self,
        topic: str,
        payload: str,
        qos: int,
        retain: bool = False,
    ) -> None:
        del retain
        self.published.append((topic, payload, qos))
        self.published_event.set()


class _SlowFakeMqttClient:
    async def publish(
        self,
        topic: str,
        payload: str,
        qos: int,
        retain: bool = False,
    ) -> None:
        del topic, payload, qos, retain
        await anyio.sleep_forever()


class _SlowFakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, payload: str) -> None:
        del payload
        await anyio.sleep_forever()

    async def close(self) -> None:
        self.closed = True


def _plugin_message(message_type: str, value: int) -> DeckrMessage:
    del value
    return plugin_message(
        sender=host_address("python"),
        recipient=controller_address("controller-main"),
        message_type=message_type,
        body={},
        subject=entity_subject("test"),
    )


def _broadcast_plugin_message(message_type: str, value: int) -> DeckrMessage:
    del value
    return plugin_message(
        sender=controller_address("controller-main"),
        recipient=plugin_hosts_broadcast(),
        message_type=message_type,
        body={},
        subject=entity_subject("test"),
    )


def _without_route(message: DeckrMessage) -> DeckrMessage:
    return message.model_copy(update={"route": None})


async def _server_client_id(server) -> str:
    with anyio.fail_after(3):
        while not server._connection_client_ids:
            await anyio.sleep(0.01)
    return next(iter(server._connection_client_ids.values()))


def _hardware_device() -> hw_messages.HardwareDevice:
    return hw_messages.HardwareDevice(
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
    outbound_message = _plugin_message(HOST_ONLINE, 1)
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
    assert client.published[0][2] == 0
    published_frame = parse_mqtt_frame(json.loads(client.published[0][1]))
    assert _without_route(published_frame.message) == outbound_message
    assert published_frame.client_id == binding.client_id

    inbound_message = _plugin_message(HOST_OFFLINE, 7)
    async with (
        plugin_bus.subscribe() as stream,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(transport._mqtt_to_bus_loop, client, binding)
        await client.messages.inject(
            json.dumps(build_mqtt_frame("remote-mqtt", inbound_message)),
            topic="deckr/v1",
        )
        received = await _next_message(stream, HOST_OFFLINE)
        tg.cancel_scope.cancel()

    assert _without_route(received) == inbound_message
    assert received.route.current_client_id == binding.client_id


@pytest.mark.asyncio
async def test_mqtt_send_timeout_reports_drop_and_withdraws_route() -> None:
    plugin_bus = EventBus("plugin_messages")
    transport = mqtt_transport_component.factory(
        _component_context(
            mqtt_transport_component,
            raw_config={
                "transport_id": "python-mqtt",
                "hostname": "mqtt.example.net",
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
    message = _plugin_message(HOST_ONLINE, 1)
    await plugin_bus.route_table.claim_endpoint(
        endpoint=controller_address("controller-main"),
        lane="plugin_messages",
        client_id=binding.client_id,
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="python-mqtt",
        claim_source="transport_route",
    )

    async with plugin_bus.route_table.subscribe() as events:
        await transport._publish(_SlowFakeMqttClient(), binding, message)
        dropped = await _next_route_event(events, "messageDropped")
        disconnected = await _next_route_event(events, "clientDisconnected")

    assert dropped.message_id == message.message_id
    assert dropped.reason == "slowRemoteClient"
    assert disconnected.client_id == binding.client_id
    assert (
        await plugin_bus.route_table.route_for(
            controller_address("controller-main"),
            lane="plugin_messages",
        )
    ) is None


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

    inbound_message = _plugin_message(HOST_ONLINE, 1)
    outbound_message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type=REQUEST_ACTIONS,
        body={},
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

            received = await _next_message(stream, HOST_ONLINE)
            assert _without_route(received) == inbound_message

            await plugin_bus.send(outbound_message)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert frame.transport_id == "controller-ws"
            assert _without_route(frame.message) == outbound_message
            assert frame.client_id is not None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_websocket_client_send_timeout_reports_drop_and_withdraws_route() -> None:
    plugin_bus = EventBus("plugin_messages")
    client = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "transport_id": "controller-ws",
                "mode": "client",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "uri": "ws://127.0.0.1/plugin",
                    }
                },
            },
            lanes={"plugin_messages": plugin_bus},
        )
    )
    binding = client._bindings[0]
    websocket = _SlowFakeWebSocket()
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )
    await plugin_bus.route_table.claim_endpoint(
        endpoint=host_address("python"),
        lane="plugin_messages",
        client_id=binding.client_id,
        client_kind="remote",
        transport_kind="websocket",
        transport_id="controller-ws",
        claim_source="transport_route",
    )

    async with plugin_bus.route_table.subscribe() as events:
        await client._send_to_client(websocket, binding, message)
        dropped = await _next_route_event(events, "messageDropped")
        disconnected = await _next_route_event(events, "clientDisconnected")

    assert dropped.message_id == message.message_id
    assert dropped.reason == "slowRemoteClient"
    assert disconnected.client_id == binding.client_id
    assert websocket.closed is True
    assert (
        await plugin_bus.route_table.route_for(
            host_address("python"),
            lane="plugin_messages",
        )
        is None
    )


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
    inbound_message = _plugin_message(HOST_ONLINE, 1)
    outbound_message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type=REQUEST_ACTIONS,
        body={},
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
            received = await _next_message(stream, HOST_ONLINE)
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
                        _plugin_message(HOST_ONLINE, 1),
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
            outbound_message = _broadcast_plugin_message(REQUEST_ACTIONS, 2)
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
    inbound_message = _plugin_message(HOST_ONLINE, 1)

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

                received = await _next_message(stream, HOST_ONLINE)
                assert _without_route(received) == inbound_message

                await plugin_bus.send(_broadcast_plugin_message(REQUEST_ACTIONS, 2))
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
                build_websocket_frame("remote-ws", _plugin_message(HOST_ONLINE, 1))
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
                outbound_message = _broadcast_plugin_message(REQUEST_ACTIONS, 2)
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
    message = _plugin_message(HOST_ONLINE, 3)
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


def test_transport_bindings_accept_local_bridge_authority_config() -> None:
    mqtt_transport = mqtt_transport_component.factory(
        _component_context(
            mqtt_transport_component,
            raw_config={
                "hostname": "mqtt.example.net",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/plugin",
                        "trusted_bridge": True,
                        "authority_id": "local-config",
                    }
                },
            },
            lanes={"plugin_messages": EventBus("plugin_messages")},
        )
    )
    websocket_transport = websocket_transport_component.factory(
        _component_context(
            websocket_transport_component,
            raw_config={
                "mode": "server",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "path": "/plugin",
                        "trusted_bridge": True,
                        "authority_id": "local-config",
                    }
                },
            },
            lanes={"plugin_messages": EventBus("plugin_messages")},
        )
    )

    assert mqtt_transport._bindings[0].config.trusted_bridge is True
    assert mqtt_transport._bindings[0].config.authority_id == "local-config"
    assert websocket_transport._bindings[0].config.trusted_bridge is True
    assert websocket_transport._bindings[0].config.authority_id == "local-config"


def test_mqtt_core_lane_bindings_reject_non_ephemeral_delivery_options() -> None:
    with pytest.raises(ValueError, match="qos <= 0"):
        mqtt_transport_component.lanes_for(
            raw_config={
                "hostname": "mqtt.example.net",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/plugin",
                        "qos": 1,
                    }
                },
            },
            instance_id="main",
        )

    with pytest.raises(ValueError, match="must not retain"):
        mqtt_transport_component.lanes_for(
            raw_config={
                "hostname": "mqtt.example.net",
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/plugin",
                        "retain": True,
                    }
                },
            },
            instance_id="main",
        )

    with pytest.raises(
        ValueError, match="MQTT lane delivery requires clean_session=true"
    ):
        mqtt_transport_component.lanes_for(
            raw_config={
                "hostname": "mqtt.example.net",
                "clean_session": False,
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/plugin",
                    }
                },
            },
            instance_id="main",
        )

    with pytest.raises(
        ValueError, match="MQTT lane delivery requires clean_start=true"
    ):
        mqtt_transport_component.lanes_for(
            raw_config={
                "hostname": "mqtt.example.net",
                "clean_start": False,
                "bindings": {
                    "plugin": {
                        "lane": "plugin_messages",
                        "topic": "deckr/plugin",
                    }
                },
            },
            instance_id="main",
        )


@pytest.mark.asyncio
async def test_mqtt_malformed_json_logs_structured_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin_bus = EventBus("plugin_messages")
    transport = mqtt_transport_component.factory(
        _component_context(
            mqtt_transport_component,
            raw_config={
                "transport_id": "python-mqtt",
                "hostname": "mqtt.example.net",
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

    with caplog.at_level(logging.WARNING):
        async with anyio.create_task_group() as tg:
            tg.start_soon(transport._mqtt_to_bus_loop, client, binding)
            await client.messages.inject("{not-json", topic="deckr/v1")
            await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

    assert "Dropped malformed MQTT JSON message" in caplog.text


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
                    "lane": "hardware_messages",
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

    assert lanes.consumes == ("hardware_messages", "plugin_messages")
    assert lanes.publishes == ("hardware_messages", "plugin_messages")


@pytest.mark.asyncio
async def test_websocket_hardware_manager_uses_same_envelope_and_route_semantics(
    unused_tcp_port: int,
) -> None:
    device = _hardware_device()
    manager_endpoint = hardware_manager_address("room-a")
    connected = hw_messages.hardware_input_message(
        manager_id="room-a",
        device_id=device.id,
        body=hw_messages.DeviceConnectedMessage(device=device),
    )

    local_bus = EventBus("hardware_messages")
    await local_bus.claim_local_endpoint(manager_endpoint)
    async with local_bus.subscribe() as stream:
        await local_bus.send(connected)
        assert await stream.receive() == connected
    route = await local_bus.route_table.route_for(
        manager_endpoint,
        lane="hardware_messages",
    )
    assert route.client_kind == "local"

    hardware_bus = EventBus("hardware_messages")
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
                        "lane": "hardware_messages",
                        "path": "/hardware",
                    }
                },
            },
            lanes={"hardware_messages": hardware_bus},
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
            received = await _next_message(stream, hw_messages.DEVICE_CONNECTED)
            assert _without_route(received) == connected
            route = await hardware_bus.route_table.route_for(
                manager_endpoint,
                lane="hardware_messages",
            )
            assert route is not None
            assert route.client_kind == "remote"

            command = hw_messages.hardware_command_message(
                controller_id="controller-main",
                manager_id="room-a",
                message_type=hw_messages.CLEAR_SLOT,
                device_id=device.id,
                control_id="0,0",
                control_kind="slot",
                body=hw_messages.ClearSlotMessage(slot_id="0,0"),
            )
            await hardware_bus.send(command)
            frame = parse_websocket_frame(json.loads(await websocket.recv()))
            assert _without_route(frame.message) == command
            assert frame.message.recipient.endpoint == manager_endpoint
            assert hw_messages.hardware_control_ref_from_subject(
                frame.message.subject
            ) == hw_messages.HardwareControlRef(
                manager_id="room-a",
                device_id="deck",
                control_id="0,0",
                control_kind="slot",
            )

        tg.cancel_scope.cancel()
