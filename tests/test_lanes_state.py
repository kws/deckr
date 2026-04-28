from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest

from deckr.contracts.messages import (
    controller_address,
    endpoint_address,
    entity_subject,
    host_address,
    plugin_hosts_broadcast,
)
from deckr.runtime import Deckr
from deckr.state import StateConflict, decode_key_token, encode_key_token
from deckr.substrates.nats import _headers_for, _subject_for


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


@pytest.mark.asyncio
async def test_endpoint_send_stamps_sender_and_filters_direct_recipient() -> None:
    async with Deckr() as deckr:
        host = deckr.lane("plugin_messages").endpoint(host_address("python"))
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        other = deckr.lane("plugin_messages").endpoint(controller_address("other"))
        async with controller.subscribe() as controller_stream, other.subscribe() as other_stream:
            sent = await host.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )
            received = await _receive(controller_stream)
            with anyio.move_on_after(0.05) as scope:
                await other_stream.receive()

    assert received == sent
    assert received.sender == host_address("python")
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with Deckr() as deckr:
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        host_a = deckr.lane("plugin_messages").endpoint(host_address("a"))
        host_b = deckr.lane("plugin_messages").endpoint(host_address("b"))
        controller_listener = deckr.lane("plugin_messages").endpoint(
            controller_address("other")
        )
        async with (
            host_a.subscribe() as stream_a,
            host_b.subscribe() as stream_b,
            controller_listener.subscribe() as controller_stream,
        ):
            sent = await controller.send(
                recipient=plugin_hosts_broadcast(),
                subject=entity_subject("page", contextId="ctx"),
                message_type="setTitle",
                body={"title": "Ready"},
            )
            received_a = await _receive(stream_a)
            received_b = await _receive(stream_b)
            with anyio.move_on_after(0.05) as scope:
                await controller_stream.receive()

    assert received_a == sent
    assert received_b == sent
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_lane_validation_rejects_wrong_sender_family() -> None:
    async with Deckr() as deckr:
        worker = deckr.lane("plugin_messages").endpoint(endpoint_address("worker", "x"))
        with pytest.raises(ValueError, match="Sender family"):
            await worker.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )


@pytest.mark.asyncio
async def test_endpoint_request_uses_deckr_correlation() -> None:
    async with Deckr() as deckr:
        host = deckr.lane("plugin_messages").endpoint(host_address("python"))
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        ready = anyio.Event()

        async def responder() -> None:
            async with controller.subscribe() as stream:
                ready.set()
                request = await _receive(stream)
                await controller.reply_to(
                    request,
                    message_type="hereAreSettings",
                    body={"settings": {"theme": "dark"}},
                )

        async with anyio.create_task_group() as tg:
            tg.start_soon(responder)
            await ready.wait()
            reply = await host.request(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )
            tg.cancel_scope.cancel()

    assert reply.message_type == "hereAreSettings"
    assert reply.in_reply_to is not None


@pytest.mark.asyncio
async def test_state_create_update_conflicts_and_ttl_expiry() -> None:
    async with Deckr() as deckr:
        state = deckr.state("test_state")
        async with state.watch("claim.") as changes:
            created = await state.create(
                "claim.device.main.deck",
                {"owner": "controller"},
            )
            assert (await _receive(changes)).entry == created

            with pytest.raises(StateConflict):
                await state.create("claim.device.main.deck", {"owner": "other"})
            with pytest.raises(StateConflict):
                await state.update(
                    "claim.device.main.deck",
                    {"owner": "controller"},
                    revision=created.revision + 10,
                )

            old = datetime.now(UTC) - timedelta(seconds=2)
            await state.put(
                "claim.device.main.expiring",
                {
                    "owner": "controller",
                    "timestamp": old.isoformat().replace("+00:00", "Z"),
                    "ttlSeconds": 1,
                },
            )
            await _receive(changes)
            expired = await _receive(changes)

    assert expired.operation == "expire"
    assert expired.key == "claim.device.main.expiring"


def test_key_token_encoding_round_trips_nats_safe_and_fallback_tokens() -> None:
    assert encode_key_token("deck_1") == "deck_1"
    assert decode_key_token("deck_1") == "deck_1"
    encoded = encode_key_token("b64_native")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "b64_native"
    encoded = encode_key_token("deck:one")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "deck:one"


def test_nats_subject_and_headers_are_delivery_hints_for_canonical_envelope() -> None:
    # Build through the public lane API so sender stamping and validation stay covered.
    async def build():
        async with Deckr() as deckr:
            host = deckr.lane("plugin_messages").endpoint(host_address("python"))
            controller = deckr.lane("plugin_messages").endpoint(
                controller_address("main")
            )
            async with controller.subscribe():
                return await host.send(
                    recipient=controller_address("main"),
                    subject=entity_subject("settings", contextId="ctx"),
                    message_type="requestSettings",
                    body={},
                )

    message = anyio.run(build)
    assert _subject_for(message) == "deckr.lane.plugin_messages.host.python"
    assert _headers_for(message)["Deckr-Message-Id"] == message.message_id
    assert _headers_for(message)["Deckr-Sender"] == "host:python"
    assert _headers_for(message)["Deckr-Recipient"] == "controller:main"
