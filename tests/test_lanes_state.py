from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace

import anyio
import pytest
from message_bus_mocks import mock_deckr, mock_message_bus

from deckr.actions.endpoints import (
    action_provider_address,
)
from deckr.concord import (
    ConcordAgreementSpec,
)
from deckr.contracts.lanes import (
    DEFAULT_MESSAGE_CONTRACT_REGISTRY,
)
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    SERVICES_LANE,
    DeckrMessage,
    controller_address,
    controllers_broadcast,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    service_address,
)
from deckr.lanes import message_is_deliverable, reply_is_accepted
from deckr.runtime import Deckr
from deckr.services.messages import SERVICE_MESSAGE
from deckr.substrates.nats import (
    NatsSubstrate,
    _headers_for,
    _subject_for,
)
from deckr.substrates.nats_kv import KvBucketPolicy

_CONTRACT = {"contractId": "contract-1", "generation": 1}


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
async def test_endpoint_session_id_is_reused_across_lanes() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main"), session_id="controller-fixed") as controller,
        deckr.endpoint(hardware_manager_address("hardware")) as hardware,
        deckr.endpoint(service_address("media")) as service,
    ):
        hardware_message = await controller.send(
            lane=HARDWARE_MESSAGES_LANE,
            recipient=hardware.address,
            subject=entity_subject("hardware", deviceId="device-1"),
            message_type="controlCommand",
            body={
                "deviceRef": {"managerId": "hardware", "deviceId": "device-1"},
                "controlId": "key-1",
                "capabilityId": "raster",
                "commandType": "clear",
                "params": {},
            },
            contract=_CONTRACT,
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
            message_type=SERVICE_MESSAGE,
            body={
                "serviceNamespace": "org.example.media",
                "name": "play",
                "intent": "command",
                "exchangePattern": "request_reply",
                "params": {},
            },
            contract={"contractId": "service-contract-1", "generation": 1},
        )

    assert hardware_message.sender_session_id == "controller-fixed"
    assert service_message.sender_session_id == "controller-fixed"


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(hardware_manager_address("main")) as hardware,
        deckr.endpoint(controller_address("a")) as controller_a,
        deckr.endpoint(controller_address("b")) as controller_b,
        deckr.endpoint(hardware_manager_address("other")) as hardware_listener,
    ):
        sent = await hardware.send(
            lane=HARDWARE_MESSAGES_LANE,
            recipient=controllers_broadcast(),
            subject=entity_subject("hardware", deviceId="device-1"),
            message_type="capabilityStateChanged",
            body={
                "deviceRef": {"managerId": "main", "deviceId": "device-1"},
                "capabilityId": "raster",
                "value": "ready",
            },
            contract=_CONTRACT,
        )

    contract = deckr.lane_contracts.contract_for(HARDWARE_MESSAGES_LANE)
    assert message_is_deliverable(
        sent,
        endpoint=controller_a.address,
        endpoint_session_id=controller_a.session_id,
        contract=contract,
    )
    assert message_is_deliverable(
        sent,
        endpoint=controller_b.address,
        endpoint_session_id=controller_b.session_id,
        contract=contract,
    )
    assert not message_is_deliverable(
        sent,
        endpoint=hardware_listener.address,
        endpoint_session_id=hardware_listener.session_id,
        contract=contract,
    )


@pytest.mark.asyncio
async def test_lane_validation_rejects_wrong_sender_family() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(service_address("x")) as worker,
    ):
        with pytest.raises(ValueError, match="Sender family"):
            await worker.send(
                lane=HARDWARE_MESSAGES_LANE,
                recipient=controller_address("main"),
                subject=entity_subject("hardware", deviceId="device-1"),
                message_type="controlCommand",
                body={
                    "deviceRef": {"managerId": "x", "deviceId": "device-1"},
                    "controlId": "key-1",
                    "capabilityId": "raster",
                    "commandType": "clear",
                    "params": {},
                },
                contract=_CONTRACT,
            )


@pytest.mark.asyncio
async def test_endpoint_request_uses_deckr_correlation() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(service_address("media")) as service,
        deckr.endpoint(controller_address("main")) as controller,
    ):

        async def request_side_effect(message, *, timeout, accept):
            del timeout
            reply = DeckrMessage(
                lane=message.lane,
                messageType=SERVICE_MESSAGE,
                sender=service.address,
                senderSessionId=service.session_id,
                recipient=endpoint_target(message.sender),
                recipientSessionId=message.sender_session_id,
                subject=message.subject,
                inReplyTo=message.message_id,
                contract=message.contract,
                body={
                    "serviceNamespace": "org.example.media",
                    "name": "play",
                    "intent": "command",
                    "exchangePattern": "request_reply",
                    "status": "ok",
                    "result": {"state": "playing"},
                },
            )
            assert await reply_is_accepted(reply, request=message, accept=accept)
            return reply

        deckr._message_bus.request.side_effect = request_side_effect
        reply = await controller.request(
            lane=SERVICES_LANE,
            recipient=service.address,
            subject=entity_subject(
                "service",
                serviceId="media",
                namespace="org.example.media",
                operation="play",
            ),
            message_type=SERVICE_MESSAGE,
            body={
                "serviceNamespace": "org.example.media",
                "name": "play",
                "intent": "command",
                "exchangePattern": "request_reply",
                "params": {},
            },
            contract=_CONTRACT,
        )

    assert reply.message_type == SERVICE_MESSAGE
    assert reply.in_reply_to is not None
    assert reply.recipient_session_id == controller.session_id


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
        subscription_cm = controller.subscribe(SERVICES_LANE)
        await subscription_cm.__aenter__()

        await endpoint_cm.__aexit__(None, None, None)

        assert deckr._message_bus.subscriptions[-1].exited
        with pytest.raises(RuntimeError, match="is closed"):
            controller.subscribe(SERVICES_LANE)


def test_recipient_session_mismatch_is_not_deliverable() -> None:
    message = DeckrMessage(
        lane=SERVICES_LANE,
        messageType=SERVICE_MESSAGE,
        sender=controller_address("main"),
        senderSessionId="controller-session",
        recipient=endpoint_target(service_address("media")),
        recipientSessionId="wrong-session",
        subject=entity_subject(
            "service",
            serviceId="media",
            namespace="org.example.media",
            name="play",
        ),
        contract=_CONTRACT,
        body={
            "serviceNamespace": "org.example.media",
            "name": "play",
            "intent": "command",
            "exchangePattern": "one_way",
            "params": {},
        },
    )

    assert not message_is_deliverable(
        message,
        endpoint=service_address("media"),
        endpoint_session_id="service-session",
        contract=DEFAULT_MESSAGE_CONTRACT_REGISTRY.contract_for(SERVICES_LANE),
    )


@pytest.mark.asyncio
async def test_concord_cancellation_does_not_close_lane_subscription() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(controller_address("main")) as controller,
        controller.subscribe(SERVICES_LANE),
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
        lane=SERVICES_LANE,
        messageType=SERVICE_MESSAGE,
        sender=controller_address("main"),
        senderSessionId="controller-session",
        recipient=endpoint_target(service_address("media")),
        subject=entity_subject(
            "service",
            serviceId="media",
            namespace="org.example.media",
            name="play",
        ),
        contract=_CONTRACT,
        body={
            "serviceNamespace": "org.example.media",
            "name": "play",
            "intent": "command",
            "exchangePattern": "request_reply",
            "params": {},
        },
    )


def _settings_reply_message(
    request: DeckrMessage,
    *,
    theme: str = "dark",
    in_reply_to: str | None = None,
    recipient_session_id: str | None = None,
) -> DeckrMessage:
    return DeckrMessage(
        lane=SERVICES_LANE,
        messageType=SERVICE_MESSAGE,
        sender=service_address("media"),
        senderSessionId="service-session",
        recipient=endpoint_target(request.sender),
        recipientSessionId=recipient_session_id or request.sender_session_id,
        subject=request.subject,
        inReplyTo=in_reply_to or request.message_id,
        contract=request.contract,
        body={
            "serviceNamespace": "org.example.media",
            "name": "play",
            "intent": "command",
            "exchangePattern": "request_reply",
            "status": "ok",
            "result": {"theme": theme},
        },
    )


@pytest.mark.asyncio
async def test_nats_disconnected_operations_raise_clear_runtime_error() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    message = _settings_request_message()

    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.publish(message)
    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.publish_reply(message, request=message)
    with pytest.raises(RuntimeError, match="not connected"):
        await substrate.request(message, timeout=0.01)
    with pytest.raises(RuntimeError, match="not connected"):
        async with substrate.subscribe(
            SERVICES_LANE,
            controller_address("main"),
            endpoint_session_id="controller-session",
        ):
            pass
    with pytest.raises(RuntimeError, match="not connected"):
        substrate.kv_bucket(KvBucketPolicy(bucket="views", ttl_seconds=None))


@pytest.mark.asyncio
async def test_nats_publish_reply_falls_back_without_stored_reply_subject() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    request = _settings_request_message()
    reply = _settings_reply_message(request)

    await substrate.publish_reply(reply, request=request)

    assert fake_nc.published[0]["subject"] == _subject_for(reply)
    assert fake_nc.published[0]["reply"] == ""


@pytest.mark.asyncio
async def test_nats_connect_passes_auth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeNc:
        def jetstream(self):
            return "jetstream"

    async def connect(url: str, **options):
        captured["url"] = url
        captured["options"] = options
        return FakeNc()

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))
    substrate = NatsSubstrate(
        url="nats://nats.example:4222",
        auth_token="secret-token",
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    )

    await substrate.connect()

    assert captured == {
        "url": "nats://nats.example:4222",
        "options": {"token": "secret-token"},
    }
    assert substrate._js == "jetstream"  # noqa: SLF001


@pytest.mark.asyncio
async def test_nats_subject_payload_mismatch_is_dropped_and_logged(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="deckr.substrates.nats")
    substrate = NatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY,
        buffer_size=1,
    )
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    message = _settings_request_message()

    async with substrate.subscribe(
        SERVICES_LANE,
        service_address("media"),
        endpoint_session_id="service-session",
    ) as stream:
        await fake_nc.subscriptions[0].deliver(
            message,
            subject="deckr.msg.services.to.service.other",
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
        accept=lambda message: message.body["result"]["theme"] == "dark",
    )

    assert reply.body["result"]["theme"] == "dark"
    assert fake_nc.published[0]["subject"] == "deckr.msg.services.to.service.media"
    assert fake_nc.published[0]["reply"] == "_INBOX.1"


@pytest.mark.asyncio
async def test_nats_request_ignores_wrong_recipient_session_reply() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    fake_nc = _FakeNc()
    substrate._nc = fake_nc
    request = _settings_request_message()
    fake_nc.reply_deliveries = [
        _settings_reply_message(
            request,
            theme="wrong-session",
            recipient_session_id="other-session",
        ),
        _settings_reply_message(request, theme="accepted"),
    ]

    reply = await substrate.request(request, timeout=1)

    assert reply.body["result"]["theme"] == "accepted"


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
    service = service_address("media")
    first = _settings_request_message()
    second = _settings_request_message()

    async with substrate.subscribe(
        SERVICES_LANE,
        service,
        endpoint_session_id="service-session",
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
    assert "lane=services endpoint=service:media session=service-session" in caplog.text
