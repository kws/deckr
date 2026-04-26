from __future__ import annotations

import anyio
import pytest

from deckr.contracts.messages import controller_address, entity_subject, host_address
from deckr.pluginhost.messages import plugin_message
from deckr.transports._lanes import build_lane_handler
from deckr.transports.bus import EventBus
from deckr.transports.routes import (
    MAX_ROUTE_HISTORY,
    mark_forwarded_to_client,
    mark_received_from_client,
    should_forward_to_client,
)


def _message(lane: str = "plugin_messages"):
    message = plugin_message(
        sender=host_address("test"),
        recipient=controller_address("test"),
        message_type="test.message",
        payload={"value": 1},
        subject=entity_subject("test"),
    )
    if lane == "plugin_messages":
        return message
    return message.model_copy(update={"lane": lane})


async def _next_route_event(stream, event_type: str):
    while True:
        event = await stream.receive()
        if event.event_type == event_type:
            return event


@pytest.mark.asyncio
async def test_event_bus_delivers_deckr_messages_directly() -> None:
    bus = EventBus("plugin_messages")
    message = _message()

    async with bus.subscribe() as stream:
        await bus.send(message)
        received = await stream.receive()

    assert received is message
    assert received.lane == "plugin_messages"
    assert received.message_type == "test.message"


@pytest.mark.asyncio
async def test_event_bus_rejects_wrong_lane() -> None:
    bus = EventBus("hardware_events")

    with pytest.raises(ValueError, match="Cannot send message"):
        await bus.send(_message())


@pytest.mark.asyncio
async def test_event_bus_rejects_non_deckr_message() -> None:
    bus = EventBus("plugin_messages")

    with pytest.raises(TypeError, match="DeckrMessage"):
        await bus.send({"legacy": "payload"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_route_table_local_claim_rejects_remote_conflict() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")

    async with bus.route_table.subscribe() as stream:
        await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            client_id="local:host:python",
            client_kind="local",
        )
        await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            client_id="websocket:remote",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
        )
        reachable = await _next_route_event(stream, "endpointReachable")
        rejected = await _next_route_event(stream, "endpointClaimRejected")

    assert reachable.event_type == "endpointReachable"
    assert rejected.event_type == "endpointClaimRejected"
    assert (await bus.route_table.route_for(endpoint)).client_id == "local:host:python"


@pytest.mark.asyncio
async def test_route_table_first_remote_claim_owns_until_disconnect() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")

    accepted = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="websocket:first",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
    )
    rejected = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="mqtt:second",
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="mqtt-main",
    )

    assert accepted is not None
    assert rejected is None
    assert (await bus.route_table.route_for(endpoint)).client_id == "websocket:first"

    await bus.route_table.client_disconnected("websocket:first")
    accepted_after_disconnect = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="mqtt:second",
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="mqtt-main",
    )
    assert accepted_after_disconnect is not None
    assert (await bus.route_table.route_for(endpoint)).client_id == "mqtt:second"


@pytest.mark.asyncio
async def test_route_table_local_claim_replaces_remote_claim() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="websocket:remote",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
    )

    async with bus.route_table.subscribe() as stream:
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            client_id="local:host:python",
            client_kind="local",
        )
        unreachable = await _next_route_event(stream, "endpointUnreachable")
        reachable = await _next_route_event(stream, "endpointReachable")

    assert accepted is not None
    assert unreachable.client_id == "websocket:remote"
    assert unreachable.reason == "localClaimReplaced"
    assert reachable.client_id == "local:host:python"
    assert (await bus.route_table.route_for(endpoint)).client_id == "local:host:python"
    assert await bus.route_table.routes_for_client("websocket:remote") == ()


@pytest.mark.asyncio
async def test_rejected_remote_endpoint_claim_does_not_enter_bus() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("test")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="local:host:test",
        client_kind="local",
    )
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )

    async with bus.subscribe() as stream:
        await handler.handle_remote_message(_message(), client_id="websocket:spoof")
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called


@pytest.mark.asyncio
async def test_route_table_disconnect_removes_endpoint_routes() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        client_id="websocket:remote",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
    )

    async with bus.route_table.subscribe() as stream:
        await bus.route_table.client_disconnected("websocket:remote")
        unreachable = await _next_route_event(stream, "endpointUnreachable")
        disconnected = await _next_route_event(stream, "clientDisconnected")

    assert unreachable.event_type == "endpointUnreachable"
    assert disconnected.event_type == "clientDisconnected"
    assert await bus.route_table.route_for(endpoint) is None


def test_forwarding_metadata_prevents_echo_without_message_id_cache() -> None:
    message = _message()
    received = mark_received_from_client(message, client_id="ws:one")

    assert should_forward_to_client(received, client_id="ws:one") is False
    assert should_forward_to_client(received, client_id="ws:two") is True


def test_forwarding_metadata_preserves_origin_client() -> None:
    message = _message()
    received = mark_received_from_client(message, client_id="ws:origin")
    forwarded = mark_forwarded_to_client(received, client_id="mqtt:target")

    assert forwarded.route.origin_client_id == "ws:origin"
    assert forwarded.route.current_client_id == "mqtt:target"
    assert forwarded.route.route_history == ("ws:origin", "mqtt:target")


def test_route_history_is_capped() -> None:
    message = _message()

    for index in range(MAX_ROUTE_HISTORY + 3):
        message = mark_received_from_client(message, client_id=f"client:{index}")

    assert len(message.route.route_history) == MAX_ROUTE_HISTORY
    assert message.route.route_history[0] == "client:3"
