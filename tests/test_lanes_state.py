from __future__ import annotations

import json
import logging

import anyio
import pytest
from memory_message_bus import MemoryMessageBus, memory_deckr

from deckr.actions.endpoints import (
    action_provider_address,
    action_providers_broadcast,
)
from deckr.beacon import (
    beacon_advertisement_key,
    parse_beacon_advertisement_key,
)
from deckr.concord import (
    concord_contract_key,
    concord_participant_token_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.lanes import DEFAULT_MESSAGE_CONTRACT_REGISTRY
from deckr.contracts.messages import (
    ACTIONS_LANE,
    SERVICES_LANE,
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    service_address,
)
from deckr.runtime import Deckr
from deckr.services.messages import SERVICE_COMMAND
from deckr.substrates.nats import (
    NatsSubstrate,
    _headers_for,
    _subject_for,
)


def _settings_target() -> dict[str, str]:
    return {
        "scope": "action_instance",
        "controllerId": "main",
        "configId": "device-config",
        "providerInstanceId": "demo-provider",
        "providerId": "demo.provider",
        "actionId": "demo.action",
        "actionInstanceId": "instance-a",
    }


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


@pytest.mark.asyncio
async def test_endpoint_send_stamps_sender_and_filters_direct_recipient() -> None:
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(controller_address("main")) as controller,
        deckr.endpoint(controller_address("other")) as other,
        controller.subscribe(ACTIONS_LANE) as controller_stream,
        other.subscribe(ACTIONS_LANE) as other_stream,
    ):
        sent = await provider.send(
            lane=ACTIONS_LANE,
            recipient=controller_address("main"),
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        received = await _receive(controller_stream)
        with anyio.move_on_after(0.05) as scope:
            await other_stream.receive()

    assert received == sent
    assert received.sender == action_provider_address("python")
    assert received.sender_session_id == provider.session_id
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_endpoint_session_id_is_reused_across_lanes() -> None:
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(controller_address("main"), session_id="controller-fixed") as controller,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(service_address("media")) as service,
        provider.subscribe(ACTIONS_LANE) as action_stream,
        service.subscribe(SERVICES_LANE) as service_stream,
    ):
        action_message = await controller.send(
            lane=ACTIONS_LANE,
            recipient=provider.address,
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        service_message = await controller.send(
            lane=SERVICES_LANE,
            recipient=service.address,
            subject=entity_subject(
                "service",
                serviceId="media",
                namespace="org.example.media",
                operation="play",
            ),
            message_type=SERVICE_COMMAND,
            body={
                "serviceNamespace": "org.example.media",
                "operation": "play",
                "params": {},
            },
        )

        assert await _receive(action_stream) == action_message
        assert await _receive(service_stream) == service_message

    assert action_message.sender_session_id == "controller-fixed"
    assert service_message.sender_session_id == "controller-fixed"


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        deckr.endpoint(action_provider_address("a")) as provider_a,
        deckr.endpoint(action_provider_address("b")) as provider_b,
        deckr.endpoint(controller_address("other")) as controller_listener,
        provider_a.subscribe(ACTIONS_LANE) as stream_a,
        provider_b.subscribe(ACTIONS_LANE) as stream_b,
        controller_listener.subscribe(ACTIONS_LANE) as controller_stream,
    ):
        sent = await controller.send(
            lane=ACTIONS_LANE,
            recipient=action_providers_broadcast(),
            subject=entity_subject("page", contextId="ctx"),
            message_type="actionExtension",
            body={
                "extensionType": "test.broadcast",
                "extensionSchemaId": "test.broadcast.v1",
                "data": {"title": "Ready"},
            },
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
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(hardware_manager_address("x")) as worker,
    ):
        with pytest.raises(ValueError, match="Sender family"):
            await worker.send(
                lane=ACTIONS_LANE,
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )


@pytest.mark.asyncio
async def test_endpoint_request_uses_deckr_correlation() -> None:
    async with memory_deckr() as deckr:
        ready = anyio.Event()

        async def responder(controller) -> None:
            async with controller.subscribe(ACTIONS_LANE) as stream:
                ready.set()
                request = await _receive(stream)
                await controller.reply_to(
                    request,
                    message_type="settingsSnapshot",
                    body={"target": _settings_target(), "settings": {"theme": "dark"}},
                )

        async with (
            deckr.endpoint(action_provider_address("python")) as provider,
            deckr.endpoint(controller_address("main")) as controller,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(responder, controller)
            await ready.wait()
            reply = await provider.request(
                lane=ACTIONS_LANE,
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )
            tg.cancel_scope.cancel()

    assert reply.message_type == "settingsSnapshot"
    assert reply.in_reply_to is not None
    assert reply.recipient_session_id == provider.session_id


@pytest.mark.asyncio
async def test_endpoint_context_is_local_runtime_identity_only() -> None:
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(
            action_provider_address("python"),
            metadata={"runtime": "test-provider"},
        ) as provider,
    ):
        assert provider.address == action_provider_address("python")
        assert provider.session_id
        assert provider.metadata == {"runtime": "test-provider"}


@pytest.mark.asyncio
async def test_lane_does_not_register_endpoints() -> None:
    async with memory_deckr() as deckr:
        assert not hasattr(deckr.lane(ACTIONS_LANE), "register_endpoint")


@pytest.mark.asyncio
async def test_same_endpoint_can_open_separate_sessions() -> None:
    message_bus = MemoryMessageBus(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    async with (
        Deckr(message_bus=message_bus) as deckr_a,
        Deckr(message_bus=message_bus) as deckr_b,
        deckr_a.endpoint(action_provider_address("python")) as provider_a,
        deckr_b.endpoint(action_provider_address("python")) as provider_b,
    ):
        assert provider_a.address == provider_b.address
        assert provider_a.session_id != provider_b.session_id


@pytest.mark.asyncio
async def test_closed_endpoint_session_is_local_terminal_state() -> None:
    async with memory_deckr() as deckr:
        endpoint_cm = deckr.endpoint(action_provider_address("python"))
        provider = await endpoint_cm.__aenter__()
        await endpoint_cm.__aexit__(None, None, None)

        with pytest.raises(RuntimeError, match="is closed"):
            await provider.send(
                lane=ACTIONS_LANE,
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )


@pytest.mark.asyncio
async def test_sender_session_is_syntactic_and_not_presence_gated() -> None:
    message_bus = MemoryMessageBus(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    async with (
        Deckr(message_bus=message_bus) as deckr,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(ACTIONS_LANE) as stream,
    ):
        message = DeckrMessage(
            lane=ACTIONS_LANE,
            messageType="settingsRequest",
            sender=provider.address,
            senderSessionId="stale-session",
            recipient=endpoint_target(controller.address),
            subject=entity_subject("settings", contextId="ctx"),
            body={"target": _settings_target()},
        )
        await message_bus.publish(message)
        received = await _receive(stream)

    assert received == message
    assert received.sender_session_id == "stale-session"


@pytest.mark.asyncio
async def test_recipient_session_mismatch_is_not_delivered() -> None:
    async with (
        memory_deckr() as deckr,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(ACTIONS_LANE) as stream,
    ):
        await provider.send(
            lane=ACTIONS_LANE,
            recipient=controller.address,
            recipient_session_id="wrong-session",
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called


def test_key_token_encoding_round_trips_nats_safe_and_fallback_tokens() -> None:
    assert encode_key_token("deck_1") == "deck_1"
    assert decode_key_token("deck_1") == "deck_1"
    encoded = encode_key_token("b64_native")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "b64_native"
    encoded = encode_key_token("deck:one")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "deck:one"


def test_protocol_key_helpers_round_trip_encoded_tokens() -> None:
    advertisement_key = beacon_advertisement_key(
        feature_id="dev.deckr.hardware",
        advertisement_id="room/a",
    )
    contract_key = concord_contract_key(contract_id="hardware contract/1", generation=2)
    token_key = concord_participant_token_key(
        contract_id="hardware contract/1",
        generation=2,
        participant=hardware_manager_address("room/a"),
    )

    assert parse_beacon_advertisement_key(advertisement_key) == (
        "dev.deckr.hardware",
        "room/a",
    )
    assert parse_concord_contract_key(contract_key) == ("hardware contract/1", 2)
    assert parse_concord_participant_token_key(token_key) == (
        "hardware contract/1",
        2,
        hardware_manager_address("room/a"),
    )


def test_nats_subject_and_headers_are_delivery_hints_for_canonical_envelope() -> None:
    async def build():
        async with (
            memory_deckr() as deckr,
            deckr.endpoint(action_provider_address("python")) as provider,
            deckr.endpoint(controller_address("main")) as controller,
            controller.subscribe(ACTIONS_LANE),
        ):
            return await provider.send(
                lane=ACTIONS_LANE,
                recipient=controller.address,
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )

    message = anyio.run(build)
    assert _subject_for(message) == "deckr.msg.actions.to.controller.main"
    assert _headers_for(message)["Deckr-Message-Id"] == message.message_id
    assert _headers_for(message)["Deckr-Sender"] == "action_provider:python"
    assert _headers_for(message)["Deckr-Sender-Session"] == message.sender_session_id
    assert _headers_for(message)["Deckr-Recipient"] == "controller:main"


class _FakeLaneMsg:
    def __init__(self, message: DeckrMessage, *, subject: str | None = None) -> None:
        self.subject = subject or _subject_for(message)
        self.data = json.dumps(
            message.to_dict(),
            separators=(",", ":"),
        ).encode("utf-8")
        self.headers = dict(_headers_for(message))
        self.reply = None


class _FakeLaneSubscription:
    def __init__(self, nc: _FakeNc, subject: str, callback) -> None:
        self._nc = nc
        self.subject = subject
        self.callback = callback
        self.unsubscribed = False

    async def deliver(
        self,
        message: DeckrMessage,
        *,
        subject: str | None = None,
    ) -> None:
        await self.callback(_FakeLaneMsg(message, subject=subject))

    async def unsubscribe(self) -> None:
        self.unsubscribed = True
        if self in self._nc.subscriptions:
            self._nc.subscriptions.remove(self)


class _FakeNc:
    def __init__(self) -> None:
        self.subscriptions: list[_FakeLaneSubscription] = []

    async def subscribe(self, subject: str, *, cb) -> _FakeLaneSubscription:
        subscription = _FakeLaneSubscription(self, subject, cb)
        self.subscriptions.append(subscription)
        return subscription


@pytest.mark.asyncio
async def test_nats_subscribes_to_recipient_hinted_direct_and_broadcast_subjects() -> None:
    substrate = NatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        buffer_size=1,
    )
    fake_nc = _FakeNc()
    substrate._nc = fake_nc

    async with substrate.subscribe(
        ACTIONS_LANE,
        controller_address("main"),
        endpoint_session_id="controller-session",
    ):
        assert [subscription.subject for subscription in fake_nc.subscriptions] == [
            "deckr.msg.actions.to.controller.main",
            "deckr.msg.actions.broadcast.*.controller",
        ]


@pytest.mark.asyncio
async def test_nats_subject_payload_mismatch_is_dropped_and_logged(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deckr.substrates.nats")
    substrate = NatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        buffer_size=1,
    )
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    message = DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=action_provider_address("python"),
        senderSessionId="provider-session",
        recipient=endpoint_target(controller_address("main")),
        recipientSessionId="controller-session",
        subject=entity_subject("settings", contextId="ctx"),
        body={"target": _settings_target()},
    )

    async with substrate.subscribe(
        ACTIONS_LANE,
        controller_address("main"),
        endpoint_session_id="controller-session",
    ) as stream:
        await fake_nc.subscriptions[0].deliver(
            message,
            subject="deckr.msg.actions.to.controller.other",
        )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called
    assert "Dropped invalid NATS Deckr lane message" in caplog.text


@pytest.mark.asyncio
async def test_nats_lane_subscriber_buffer_full_unsubscribes(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="deckr.substrates.nats")
    substrate = NatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        buffer_size=1,
    )
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    controller = controller_address("main")
    provider = action_provider_address("python")
    first = DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=provider,
        senderSessionId="provider-session",
        recipient=endpoint_target(controller),
        recipientSessionId="controller-session",
        subject=entity_subject("settings", contextId="ctx-1"),
        body={"target": _settings_target()},
    )
    second = DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=provider,
        senderSessionId="provider-session",
        recipient=endpoint_target(controller),
        recipientSessionId="controller-session",
        subject=entity_subject("settings", contextId="ctx-2"),
        body={"target": _settings_target()},
    )

    async with substrate.subscribe(
        ACTIONS_LANE,
        controller,
        endpoint_session_id="controller-session",
    ) as stream:
        subscription = fake_nc.subscriptions[0]
        all_subscriptions = tuple(fake_nc.subscriptions)
        await subscription.deliver(first)
        await subscription.deliver(second)

        assert all(subscription.unsubscribed for subscription in all_subscriptions)
        assert await _receive(stream) == first
        with pytest.raises(anyio.EndOfStream):
            await stream.receive()

    assert "subscriber buffer full" in caplog.text
    assert "lane=actions endpoint=controller:main session=controller-session" in caplog.text
