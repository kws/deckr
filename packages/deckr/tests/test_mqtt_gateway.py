import json
from contextlib import asynccontextmanager

import anyio
import pytest
from deckr.core.mqtt import _gateway
from deckr.core.mqtt._gateway import QOS, MqttGateway


class _RecordingClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int]] = []

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))


class _ClosingBus:
    @asynccontextmanager
    async def subscribe(self):
        send, receive = anyio.create_memory_object_stream[dict](1)
        await send.send({"kind": "test"})
        await send.aclose()
        try:
            yield receive
        finally:
            await receive.aclose()


class _ReconnectBus:
    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.second_subscription_started = anyio.Event()

    @asynccontextmanager
    async def subscribe(self):
        self.subscribe_calls += 1
        send, receive = anyio.create_memory_object_stream[dict](1)
        if self.subscribe_calls == 1:
            await send.send({"kind": "first"})
            await send.aclose()
        else:
            self.second_subscription_started.set()
        try:
            yield receive
        finally:
            await send.aclose()
            await receive.aclose()


class _IdleMessages:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await anyio.sleep(3600)
        raise StopAsyncIteration


class _SessionClient:
    def __init__(self, entered_event: anyio.Event | None = None) -> None:
        self.entered_event = entered_event
        self.subscribe_calls: list[tuple[str, int]] = []
        self.published: list[tuple[str, str, int]] = []
        self.messages = _IdleMessages()

    async def __aenter__(self):
        if self.entered_event is not None:
            self.entered_event.set()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def subscribe(self, topic: str, qos: int) -> None:
        self.subscribe_calls.append((topic, qos))

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))


@pytest.mark.asyncio
async def test_bus_to_mqtt_loop_raises_if_event_bus_subscription_closes():
    gateway = MqttGateway(
        event_bus=_ClosingBus(),
        serialize=lambda event: event,
        deserialize=lambda payload: payload,
    )
    client = _RecordingClient()

    with pytest.raises(RuntimeError, match="subscription closed unexpectedly"):
        await gateway._bus_to_mqtt_loop(client, "deckr/test")

    assert len(client.published) == 1
    topic, payload, qos = client.published[0]
    assert topic == "deckr/test"
    assert qos == QOS
    assert json.loads(payload)["m"] == {"kind": "test"}


@pytest.mark.asyncio
async def test_run_reconnects_after_bus_subscription_closes(monkeypatch):
    bus = _ReconnectBus()
    gateway = MqttGateway(
        event_bus=bus,
        serialize=lambda event: event,
        deserialize=lambda payload: payload,
    )

    second_session_entered = anyio.Event()
    sessions = [
        _SessionClient(),
        _SessionClient(entered_event=second_session_entered),
    ]
    created_clients: list[_SessionClient] = []

    def fake_client(*args, **kwargs):
        client = sessions[len(created_clients)]
        created_clients.append(client)
        return client

    sleep_calls: list[float] = []
    real_sleep = anyio.sleep

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(_gateway.aiomqtt, "Client", fake_client)
    monkeypatch.setattr(_gateway.anyio, "sleep", fake_sleep)

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            gateway._run,
            "broker.example",
            1883,
            "deckr/test",
            None,
            None,
        )
        await second_session_entered.wait()
        await bus.second_subscription_started.wait()
        tg.cancel_scope.cancel()

    assert len(created_clients) == 2
    assert created_clients[0].subscribe_calls == [("deckr/test", QOS)]
    assert created_clients[1].subscribe_calls == [("deckr/test", QOS)]
    assert len(created_clients[0].published) == 1
    assert json.loads(created_clients[0].published[0][1])["m"] == {"kind": "first"}
    assert sleep_calls.count(1.0) == 1
