from __future__ import annotations

import json
import logging

import anyio
import pytest
from message_bus_mocks import mock_deckr, mock_message_bus

from deckr.actions.endpoints import (
    action_provider_address,
    action_providers_broadcast,
)
from deckr.beacon import (
    BeaconAdvertisementSpec,
    beacon_advertisement_key,
    parse_beacon_advertisement_key,
)
from deckr.concord import (
    ConcordAgreementSpec,
    concord_contract_key,
    concord_participant_token_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.lanes import (
    DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    SERVICE_LANE_CONTRACT,
)
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
from deckr.lanes import message_is_deliverable, reply_is_accepted
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
        mock_deckr() as deckr,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(controller_address("main")) as controller,
        deckr.endpoint(controller_address("other")) as other,
    ):
        sent = await provider.send(
            lane=ACTIONS_LANE,
            recipient=controller_address("main"),
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )

    deckr._message_bus.publish.assert_awaited_once_with(sent)
    assert sent.sender == action_provider_address("python")
    assert sent.sender_session_id == provider.session_id
    assert message_is_deliverable(
        sent,
        endpoint=controller.address,
        endpoint_session_id=controller.session_id,
        contract=deckr.lane_contracts.contract_for(ACTIONS_LANE),
    )
    assert not message_is_deliverable(
        sent,
        endpoint=other.address,
        endpoint_session_id=other.session_id,
        contract=deckr.lane_contracts.contract_for(ACTIONS_LANE),
    )


@pytest.mark.asyncio
async def test_endpoint_session_id_is_reused_across_lanes() -> None:
    async with (
        mock_deckr(
            lane_contracts=(SERVICE_LANE_CONTRACT,),
            lanes=(SERVICES_LANE,),
        ) as deckr,
        deckr.endpoint(controller_address("main"), session_id="controller-fixed") as controller,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(service_address("media")) as service,
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

    assert action_message.sender_session_id == "controller-fixed"
    assert service_message.sender_session_id == "controller-fixed"


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        deckr.endpoint(action_provider_address("a")) as provider_a,
        deckr.endpoint(action_provider_address("b")) as provider_b,
        deckr.endpoint(controller_address("other")) as controller_listener,
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

    contract = deckr.lane_contracts.contract_for(ACTIONS_LANE)
    assert message_is_deliverable(
        sent,
        endpoint=provider_a.address,
        endpoint_session_id=provider_a.session_id,
        contract=contract,
    )
    assert message_is_deliverable(
        sent,
        endpoint=provider_b.address,
        endpoint_session_id=provider_b.session_id,
        contract=contract,
    )
    assert not message_is_deliverable(
        sent,
        endpoint=controller_listener.address,
        endpoint_session_id=controller_listener.session_id,
        contract=contract,
    )


@pytest.mark.asyncio
async def test_lane_validation_rejects_wrong_sender_family() -> None:
    async with (
        mock_deckr() as deckr,
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
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(action_provider_address("python")) as provider,
        deckr.endpoint(controller_address("main")) as controller,
    ):

        async def request_side_effect(message, *, timeout, accept):
            del timeout
            reply = DeckrMessage(
                lane=message.lane,
                messageType="settingsSnapshot",
                sender=controller.address,
                senderSessionId=controller.session_id,
                recipient=endpoint_target(message.sender),
                recipientSessionId=message.sender_session_id,
                subject=message.subject,
                inReplyTo=message.message_id,
                body={"target": _settings_target(), "settings": {"theme": "dark"}},
            )
            assert await reply_is_accepted(reply, request=message, accept=accept)
            return reply

        deckr._message_bus.request.side_effect = request_side_effect
        reply = await provider.request(
            lane=ACTIONS_LANE,
            recipient=controller_address("main"),
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )

    assert reply.message_type == "settingsSnapshot"
    assert reply.in_reply_to is not None
    assert reply.recipient_session_id == provider.session_id


@pytest.mark.asyncio
async def test_endpoint_context_is_local_runtime_identity_only() -> None:
    async with (
        mock_deckr() as deckr,
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
    async with mock_deckr() as deckr:
        assert not hasattr(deckr.lane(ACTIONS_LANE), "register_endpoint")


@pytest.mark.asyncio
async def test_same_endpoint_can_open_separate_sessions() -> None:
    message_bus = mock_message_bus(DEFAULT_MESSAGE_CONTRACT_REGISTRY)
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
    async with mock_deckr() as deckr:
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
async def test_endpoint_context_exit_closes_active_subscriptions() -> None:
    async with mock_deckr() as deckr:
        endpoint_cm = deckr.endpoint(controller_address("main"))
        controller = await endpoint_cm.__aenter__()
        subscription_cm = controller.subscribe(ACTIONS_LANE)
        await subscription_cm.__aenter__()

        await endpoint_cm.__aexit__(None, None, None)

        assert deckr._message_bus.subscriptions[-1].exited
        with pytest.raises(RuntimeError, match="is closed"):
            controller.subscribe(ACTIONS_LANE)


def test_sender_session_is_syntactic_and_not_presence_gated() -> None:
    provider = action_provider_address("python")
    controller = controller_address("main")
    message = DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=provider,
        senderSessionId="stale-session",
        recipient=endpoint_target(controller),
        subject=entity_subject("settings", contextId="ctx"),
        body={"target": _settings_target()},
    )

    assert message_is_deliverable(
        message,
        endpoint=controller,
        endpoint_session_id="controller-session",
        contract=DEFAULT_MESSAGE_CONTRACT_REGISTRY.contract_for(ACTIONS_LANE),
    )


def test_recipient_session_mismatch_is_not_deliverable() -> None:
    message = DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=action_provider_address("python"),
        senderSessionId="provider-session",
        recipient=endpoint_target(controller_address("main")),
        recipientSessionId="wrong-session",
        subject=entity_subject("settings", contextId="ctx"),
        body={"target": _settings_target()},
    )

    assert not message_is_deliverable(
        message,
        endpoint=controller_address("main"),
        endpoint_session_id="controller-session",
        contract=DEFAULT_MESSAGE_CONTRACT_REGISTRY.contract_for(ACTIONS_LANE),
    )


@pytest.mark.asyncio
async def test_lane_subscription_does_not_imply_beacon_advertisement() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(ACTIONS_LANE),
    ):
        assert deckr.beacon.candidates("dev.deckr.test.feature") == ()


@pytest.mark.asyncio
async def test_beacon_withdrawal_does_not_close_lane_subscription() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(ACTIONS_LANE),
    ):
        subscription = deckr._message_bus.subscriptions[-1]
        advertisement = await deckr.beacon.advertise(
            BeaconAdvertisementSpec(
                feature_id="dev.deckr.test.feature",
                endpoint=service_address("media"),
                session_id="service-session",
                advertisement_id="media",
            )
        )
        assert await advertisement.withdraw()
        assert not subscription.exited


@pytest.mark.asyncio
async def test_concord_cancellation_does_not_close_lane_subscription() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(ACTIONS_LANE),
    ):
        subscription = deckr._message_bus.subscriptions[-1]
        agreement = await deckr.concord.propose(
            ConcordAgreementSpec(
                profile="dev.deckr.test.profile.v1",
                participants=(controller.address, service_address("media")),
                local_participant=controller.address,
                local_session_id=controller.session_id,
            )
        )
        assert await agreement.cancel("test cancellation")
        assert not subscription.exited


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
            mock_deckr() as deckr,
            deckr.endpoint(action_provider_address("python")) as provider,
            deckr.endpoint(controller_address("main")) as controller,
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
    def __init__(
        self,
        message: DeckrMessage,
        *,
        subject: str | None = None,
        headers: dict[str, str] | None = None,
        reply: str | None = None,
    ) -> None:
        self.subject = subject or _subject_for(message)
        self.data = json.dumps(
            message.to_dict(),
            separators=(",", ":"),
        ).encode("utf-8")
        self.headers = headers if headers is not None else dict(_headers_for(message))
        self.reply = reply


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
        self.reply_deliveries: list[DeckrMessage | _FakeLaneMsg] = []
        self.published = []
        self._next_inbox = 0

    async def subscribe(self, subject: str, *, cb) -> _FakeLaneSubscription:
        subscription = _FakeLaneSubscription(self, subject, cb)
        self.subscriptions.append(subscription)
        return subscription

    def new_inbox(self) -> str:
        self._next_inbox += 1
        return f"_INBOX.{self._next_inbox}"

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        reply: str = "",
        headers=None,
    ) -> None:
        self.published.append(
            {
                "subject": subject,
                "payload": payload,
                "reply": reply,
                "headers": headers,
            }
        )
        if not reply:
            return
        for subscription in tuple(self.subscriptions):
            if subscription.subject != reply:
                continue
            for delivery in tuple(self.reply_deliveries):
                message = (
                    delivery
                    if isinstance(delivery, _FakeLaneMsg)
                    else _FakeLaneMsg(delivery)
                )
                await subscription.callback(message)


def _settings_request_message() -> DeckrMessage:
    return DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=action_provider_address("python"),
        senderSessionId="provider-session",
        recipient=endpoint_target(controller_address("main")),
        subject=entity_subject("settings", contextId="ctx"),
        body={"target": _settings_target()},
    )


def _settings_reply_message(
    request: DeckrMessage,
    *,
    theme: str = "dark",
    in_reply_to: str | None = None,
) -> DeckrMessage:
    return DeckrMessage(
        lane=ACTIONS_LANE,
        messageType="settingsSnapshot",
        sender=controller_address("main"),
        senderSessionId="controller-session",
        recipient=endpoint_target(request.sender),
        recipientSessionId=request.sender_session_id,
        subject=request.subject,
        inReplyTo=in_reply_to or request.message_id,
        body={"target": _settings_target(), "settings": {"theme": theme}},
    )


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
async def test_nats_request_waits_for_first_accepted_reply() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    request = _settings_request_message()
    fake_nc.reply_deliveries = [
        _settings_reply_message(request, theme="light"),
        _settings_reply_message(request, theme="dark"),
    ]

    reply = await substrate.request(
        request,
        timeout=1,
        accept=lambda message: message.body["settings"]["theme"] == "dark",
    )

    assert reply.body["settings"]["theme"] == "dark"
    assert fake_nc.published[0]["subject"] == "deckr.msg.actions.to.controller.main"
    assert fake_nc.published[0]["reply"] == "_INBOX.1"


@pytest.mark.asyncio
async def test_nats_request_drops_invalid_replies_until_timeout(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deckr.substrates.nats")
    substrate = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    request = _settings_request_message()
    invalid = _FakeLaneMsg(
        _settings_reply_message(request),
        headers={"Deckr-Message-Id": "wrong"},
    )
    fake_nc.reply_deliveries = [invalid]

    with pytest.raises(TimeoutError):
        await substrate.request(request, timeout=0.01)

    assert "Dropped invalid NATS Deckr request reply" in caplog.text


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
