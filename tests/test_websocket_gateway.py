import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from deckr.core._bridge import BRIDGE_METADATA_KEY, build_bridge_envelope
from deckr.core.component import RunContext
from deckr.core.messaging import EventBus
from deckr.core.mqtt import MqttGateway, MqttGatewayConfig
from deckr.core.websocket import (
    WebSocketClientGateway,
    WebSocketClientGatewayConfig,
    WebSocketServerGateway,
    WebSocketServerGatewayConfig,
)
from deckr.plugin.messages import HostMessage


def _host_message(message_type: str, *, payload: dict | None = None) -> HostMessage:
    return HostMessage(
        from_id="host:test",
        to_id="controller:test",
        type=message_type,
        payload=payload or {},
    )


def _wire_message(event: HostMessage, gateway_id: str = "remote-gateway") -> str:
    return json.dumps(build_bridge_envelope(gateway_id, event.to_dict()))


@asynccontextmanager
async def _running_components(*components):
    stopping = anyio.Event()
    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        for component in components:
            await component.start(ctx)
        for component in components:
            wait_ready = getattr(component, "wait_ready", None)
            if wait_ready is not None:
                assert await wait_ready(timeout=2.0)
        try:
            yield
        finally:
            stopping.set()
            for component in reversed(components):
                try:
                    await component.stop()
                except Exception:
                    pass
            tg.cancel_scope.cancel()


@asynccontextmanager
async def _recording_server(host: str, port: int, handler):
    server = await serve(handler, host, port)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


class _FakeMqttMessages:
    def __init__(self) -> None:
        self._send, self._receive = anyio.create_memory_object_stream(10)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._receive.receive()

    async def inject(self, payload: str) -> None:
        await self._send.send(SimpleNamespace(payload=payload))


class _FakeMqttClient:
    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[str, int]] = []
        self.published: list[tuple[str, str, int]] = []
        self.published_event = anyio.Event()
        self.messages = _FakeMqttMessages()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def subscribe(self, topic: str, qos: int) -> None:
        self.subscribe_calls.append((topic, qos))

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))
        self.published_event.set()


class _SlowClient:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.closed = False

    async def send(self, payload: str) -> None:
        await anyio.sleep(self.delay)

    async def close(self) -> None:
        self.closed = True


class _FastClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_websocket_server_rejects_non_matching_path(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )

    async with _running_components(gateway):
        with pytest.raises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{unused_tcp_port}/wrong"):
                pass


@pytest.mark.asyncio
async def test_websocket_server_enforces_origin_allowlist(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(
            host="127.0.0.1",
            port=unused_tcp_port,
            allowed_origins=("https://allowed.example",),
        ),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )

    async with _running_components(gateway):
        with pytest.raises(InvalidStatus):
            async with connect(
                f"ws://127.0.0.1:{unused_tcp_port}/ws",
                origin="https://blocked.example",
            ):
                pass

        async with connect(f"ws://127.0.0.1:{unused_tcp_port}/ws"):
            pass

        async with connect(
            f"ws://127.0.0.1:{unused_tcp_port}/ws",
            origin="https://allowed.example",
        ):
            pass


@pytest.mark.asyncio
async def test_websocket_server_forwards_bus_events_to_clients(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    event = _host_message("busToClient", payload={"value": 1})

    async with _running_components(gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as websocket:
        await bus.send(event)
        raw = await websocket.recv()

    payload = json.loads(raw)
    assert payload["m"] == event.to_dict()
    assert payload["_g"] == gateway._gateway_id


@pytest.mark.asyncio
async def test_websocket_server_forwards_client_events_to_bus(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    event = _host_message("clientToBus", payload={"value": 2})

    async with bus.subscribe() as stream, _running_components(gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as websocket:
        await websocket.send(_wire_message(event))
        with anyio.fail_after(1.0):
            received = await stream.receive()

    assert received.type == event.type
    assert received.payload == event.payload
    assert gateway._gateway_id in received.internal_metadata[BRIDGE_METADATA_KEY]


@pytest.mark.asyncio
async def test_websocket_server_fans_out_to_peers_without_echo(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    event = _host_message("fanout", payload={"value": 3})
    wire = _wire_message(event, gateway_id="peer-a")

    async with bus.subscribe() as stream, _running_components(gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as sender, connect(f"ws://127.0.0.1:{unused_tcp_port}/ws") as peer:
        await sender.send(wire)
        with anyio.fail_after(1.0):
            peer_raw = await peer.recv()
            received = await stream.receive()

        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.2):
                await sender.recv()

    assert peer_raw == wire
    assert received.type == event.type


@pytest.mark.asyncio
async def test_websocket_server_drops_malformed_messages_without_closing(
    unused_tcp_port,
):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    event = _host_message("validAfterMalformed", payload={"value": 4})

    async with bus.subscribe() as stream, _running_components(gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as websocket:
        await websocket.send("not-json")
        await websocket.send(json.dumps({"m": "not-an-object"}))
        await websocket.send(_wire_message(event))
        with anyio.fail_after(1.0):
            received = await stream.receive()

    assert received.type == event.type
    assert received.payload == event.payload


@pytest.mark.asyncio
async def test_websocket_server_rejects_binary_frames(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )

    async with _running_components(gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as websocket:
        await websocket.send(b"binary-data")
        with pytest.raises(ConnectionClosed):
            await websocket.recv()
        assert websocket.close_code == 1003


@pytest.mark.asyncio
async def test_websocket_server_evicts_slow_clients(monkeypatch):
    import deckr.core.websocket._gateway as websocket_gateway_module

    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=8765),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    slow = _SlowClient(delay=0.05)
    fast = _FastClient()

    monkeypatch.setattr(websocket_gateway_module, "_SEND_TIMEOUT", 0.01)

    await gateway._add_connection(slow)
    await gateway._add_connection(fast)
    await gateway._broadcast_text("payload")

    assert fast.sent == ["payload"]
    assert slow.closed is True

    async with gateway._connections_lock:
        assert slow not in gateway._connections


@pytest.mark.asyncio
async def test_websocket_server_stop_closes_open_clients(unused_tcp_port):
    bus = EventBus()
    gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )

    async with _running_components(gateway):
        websocket = await connect(f"ws://127.0.0.1:{unused_tcp_port}/ws")
        try:
            await gateway.stop()
            with anyio.fail_after(1.0):
                await websocket.wait_closed()
        finally:
            await websocket.close()


@pytest.mark.asyncio
async def test_websocket_client_publishes_bus_events_upstream(unused_tcp_port):
    bus = EventBus()
    received: list[str] = []
    received_event = anyio.Event()

    async def handler(websocket):
        received.append(await websocket.recv())
        received_event.set()
        await websocket.wait_closed()

    gateway = WebSocketClientGateway(
        event_bus=bus,
        config=WebSocketClientGatewayConfig(uri=f"ws://127.0.0.1:{unused_tcp_port}/ws"),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    event = _host_message("busToRemote", payload={"value": 5})

    async with _recording_server(
        "127.0.0.1", unused_tcp_port, handler
    ), _running_components(gateway):
        await bus.send(event)
        await received_event.wait()

    assert json.loads(received[0])["m"] == event.to_dict()


@pytest.mark.asyncio
async def test_websocket_client_forwards_inbound_messages_to_bus(unused_tcp_port):
    bus = EventBus()
    event = _host_message("remoteToBus", payload={"value": 6})

    async def handler(websocket):
        await websocket.send(_wire_message(event))
        await websocket.wait_closed()

    gateway = WebSocketClientGateway(
        event_bus=bus,
        config=WebSocketClientGatewayConfig(uri=f"ws://127.0.0.1:{unused_tcp_port}/ws"),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )

    async with bus.subscribe() as stream, _recording_server(
        "127.0.0.1", unused_tcp_port, handler
    ), _running_components(gateway):
        with anyio.fail_after(1.0):
            received = await stream.receive()

    assert received.type == event.type
    assert received.payload == event.payload
    assert gateway._gateway_id in received.internal_metadata[BRIDGE_METADATA_KEY]


@pytest.mark.asyncio
async def test_websocket_client_reconnects_and_emits_online_offline_events(
    unused_tcp_port,
):
    bus = EventBus()
    online_event = _host_message("websocketOnline")
    offline_event = _host_message("websocketOffline")
    first_connected = anyio.Event()
    second_connected = anyio.Event()
    release_first = anyio.Event()
    state = {"connections": 0}

    async def handler(websocket):
        state["connections"] += 1
        if state["connections"] == 1:
            first_connected.set()
            await release_first.wait()
            await websocket.close()
            await websocket.wait_closed()
            return
        second_connected.set()
        await websocket.wait_closed()

    gateway = WebSocketClientGateway(
        event_bus=bus,
        config=WebSocketClientGatewayConfig(uri=f"ws://127.0.0.1:{unused_tcp_port}/ws"),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
        online_event=online_event,
        offline_event=offline_event,
    )

    async with bus.subscribe() as stream, _recording_server(
        "127.0.0.1", unused_tcp_port, handler
    ), _running_components(gateway):
        with anyio.fail_after(1.0):
            first = await stream.receive()
        await first_connected.wait()

        release_first.set()

        with anyio.fail_after(2.0):
            second = await stream.receive()
            third = await stream.receive()
        await second_connected.wait()

    assert first.type == "websocketOnline"
    assert second.type == "websocketOffline"
    assert third.type == "websocketOnline"


@pytest.mark.asyncio
async def test_websocket_and_mqtt_gateways_coexist(monkeypatch, unused_tcp_port):
    import deckr.core.mqtt._gateway as mqtt_gateway_module

    fake_client = _FakeMqttClient()
    monkeypatch.setattr(
        mqtt_gateway_module.aiomqtt, "Client", lambda *args, **kwargs: fake_client
    )

    bus = EventBus()
    websocket_gateway = WebSocketServerGateway(
        event_bus=bus,
        config=WebSocketServerGatewayConfig(host="127.0.0.1", port=unused_tcp_port),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    mqtt_gateway = MqttGateway(
        event_bus=bus,
        config=MqttGatewayConfig(
            hostname="broker.example",
            port=1883,
            topic="deckr/test",
            username=None,
            password=None,
        ),
        serialize=lambda event: event.to_dict(),
        deserialize=HostMessage.from_dict,
    )
    ws_event = _host_message("fromWebSocket", payload={"value": 7})
    mqtt_event = _host_message("fromMqtt", payload={"value": 8})

    async with _running_components(websocket_gateway, mqtt_gateway), connect(
        f"ws://127.0.0.1:{unused_tcp_port}/ws"
    ) as websocket:
        await websocket.send(_wire_message(ws_event, gateway_id="remote-websocket"))
        await _wait_for(lambda: bool(fake_client.published), timeout=1.0)

        published_payload = json.loads(fake_client.published[-1][1])
        assert published_payload["m"] == ws_event.to_dict()

        await fake_client.messages.inject(
            json.dumps(build_bridge_envelope("remote-mqtt", mqtt_event.to_dict()))
        )
        with anyio.fail_after(1.0):
            websocket_payload = json.loads(await websocket.recv())

        assert websocket_payload["m"] == mqtt_event.to_dict()

        await fake_client.messages.inject(
            json.dumps(
                build_bridge_envelope(
                    mqtt_gateway._gateway_id,
                    _host_message("ignored").to_dict(),
                )
            )
        )
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.2):
                await websocket.recv()
