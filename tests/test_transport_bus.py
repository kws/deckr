from __future__ import annotations

from types import MappingProxyType

import pytest

from deckr.pluginhost.messages import HostMessage
from deckr.transports.bus import (
    TRANSPORT_ID_HEADER,
    TRANSPORT_KIND_HEADER,
    EventBus,
    TransportEnvelope,
)


def _message() -> HostMessage:
    return HostMessage(
        from_id="host:test",
        to_id="controller:test",
        type="test.message",
        payload={"value": 1},
    )


def test_host_message_payload_is_immutable_json() -> None:
    source_payload = {"value": {"nested": [1]}}
    message = HostMessage(
        from_id="host:test",
        to_id="controller:test",
        type="test.message",
        payload=source_payload,
    )

    source_payload["value"] = {"nested": [2]}

    assert message.payload == {"value": {"nested": (1,)}}
    with pytest.raises(TypeError):
        message.payload["added"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        message.payload["value"]["nested"] = (3,)  # type: ignore[index]
    assert message.to_dict()["payload"] == {"value": {"nested": [1]}}


@pytest.mark.asyncio
async def test_event_bus_delivers_transport_envelopes_with_immutable_headers() -> None:
    bus = EventBus()
    message = _message()
    headers = {
        TRANSPORT_KIND_HEADER: "mqtt",
        TRANSPORT_ID_HEADER: "transport-a",
    }

    async with bus.subscribe() as stream:
        await bus.send(message, headers=headers)
        headers[TRANSPORT_ID_HEADER] = "mutated-after-send"

        envelope = await stream.receive()

    assert isinstance(envelope, TransportEnvelope)
    assert envelope.message is message
    assert isinstance(envelope.headers, MappingProxyType)
    assert envelope.headers == {
        TRANSPORT_KIND_HEADER: "mqtt",
        TRANSPORT_ID_HEADER: "transport-a",
    }
    with pytest.raises(TypeError):
        envelope.headers[TRANSPORT_ID_HEADER] = "mutated"  # type: ignore[index]


@pytest.mark.asyncio
async def test_event_bus_does_not_mutate_messages_for_transport_metadata() -> None:
    bus = EventBus()
    message = _message()

    async with bus.subscribe() as stream:
        await bus.send(message, headers={TRANSPORT_KIND_HEADER: "websocket"})
        envelope = await stream.receive()

    assert envelope.message is message
    assert not hasattr(message, "internal_metadata")
    assert "internalMetadata" not in message.model_dump(mode="json", by_alias=True)
