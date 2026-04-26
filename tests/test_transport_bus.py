from __future__ import annotations

import pytest

from deckr.contracts.messages import controller_address, entity_subject, host_address
from deckr.pluginhost.messages import plugin_message
from deckr.transports.bus import EventBus


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
