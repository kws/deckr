from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest

from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    DeliveryGuarantee,
    DeliveryPersistence,
    DeliveryReplay,
    LaneContract,
    LaneContractRegistry,
    LaneRoutePolicy,
)
from deckr.contracts.messages import (
    BUILTIN_ACTION_PROVIDER_ID,
    LEGACY_BUILTIN_ACTION_PROVIDER_ID,
    BroadcastTarget,
    controller_address,
    endpoint_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    hardware_managers_broadcast,
    host_address,
    plugin_hosts_broadcast,
)
from deckr.pluginhost.messages import (
    HERE_ARE_SETTINGS,
    HOST_ONLINE,
    REQUEST_ACTIONS,
    REQUEST_SETTINGS,
    context_subject,
    plugin_body_dict,
    plugin_message,
)
from deckr.transports._lanes import build_lane_handler
from deckr.transports.bus import EventBus
from deckr.transports.routes import (
    DEFAULT_ROUTE_LEASE_DURATION_MS,
    MAX_ROUTE_HISTORY,
    MAX_ROUTE_LEASE_DURATION_MS,
    MIN_ROUTE_LEASE_DURATION_MS,
    RouteTable,
    mark_forwarded_to_client,
    mark_received_from_client,
    route_targets_client,
    should_forward_to_client,
)


def _message(lane: str = "plugin_messages"):
    message = plugin_message(
        sender=host_address("test"),
        recipient=controller_address("test"),
        message_type=HOST_ONLINE,
        body={},
        subject=entity_subject("test"),
    )
    if lane == "plugin_messages":
        return message
    return message.model_copy(update={"lane": lane, "message_type": "deviceConnected"})


async def _next_route_event(stream, event_type: str):
    while True:
        event = await stream.receive()
        if event.event_type == event_type:
            return event


async def _next_message(stream, message_type: str):
    while True:
        message = await stream.receive()
        if message.message_type == message_type:
            return message


@pytest.mark.asyncio
async def test_event_bus_delivers_deckr_messages_directly() -> None:
    bus = EventBus("plugin_messages")
    message = _message()

    async with bus.subscribe() as stream:
        await bus.send(message)
        received = await stream.receive()

    assert received is message
    assert received.lane == "plugin_messages"
    assert received.message_type == HOST_ONLINE


@pytest.mark.asyncio
async def test_event_bus_request_returns_correlated_reply() -> None:
    bus = EventBus("plugin_messages")
    request = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("main"),
        message_type=REQUEST_SETTINGS,
        body={},
        subject=context_subject("context-1"),
    )
    ready = anyio.Event()

    async def responder() -> None:
        async with bus.subscribe() as stream:
            ready.set()
            received = await _next_message(stream, REQUEST_SETTINGS)
            await bus.reply_to(
                received,
                sender=controller_address("main"),
                message_type=HERE_ARE_SETTINGS,
                body={"settings": {"theme": "dark"}},
                subject=received.subject,
            )

    async with anyio.create_task_group() as tg:
        tg.start_soon(responder)
        await ready.wait()
        reply = await bus.request(request, timeout=1.0)
        tg.cancel_scope.cancel()

    assert reply.message_type == HERE_ARE_SETTINGS
    assert reply.in_reply_to == request.message_id
    assert plugin_body_dict(reply) == {"settings": {"theme": "dark"}}


@pytest.mark.asyncio
async def test_event_bus_request_accept_predicate_filters_replies() -> None:
    bus = EventBus("plugin_messages")
    request = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("main"),
        message_type=REQUEST_SETTINGS,
        body={},
        subject=context_subject("context-1"),
    )
    ready = anyio.Event()

    async def responder() -> None:
        async with bus.subscribe() as stream:
            ready.set()
            received = await _next_message(stream, REQUEST_SETTINGS)
            await bus.reply_to(
                received,
                sender=controller_address("main"),
                message_type=HERE_ARE_SETTINGS,
                body={"settings": {"theme": "ignore"}},
                subject=received.subject,
            )
            await bus.reply_to(
                received,
                sender=controller_address("main"),
                message_type=HERE_ARE_SETTINGS,
                body={"settings": {"theme": "dark"}},
                subject=received.subject,
            )

    def accepted(reply) -> bool:
        return plugin_body_dict(reply)["settings"].get("theme") == "dark"

    async with anyio.create_task_group() as tg:
        tg.start_soon(responder)
        await ready.wait()
        reply = await bus.request(request, timeout=1.0, accept=accepted)
        tg.cancel_scope.cancel()

    assert plugin_body_dict(reply) == {"settings": {"theme": "dark"}}


@pytest.mark.asyncio
async def test_event_bus_request_timeout_is_owned_by_requester() -> None:
    bus = EventBus("plugin_messages")
    request = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("main"),
        message_type=REQUEST_SETTINGS,
        body={},
        subject=context_subject("context-1"),
    )

    with pytest.raises(TimeoutError):
        await bus.request(request, timeout=0.01)

    late = await bus.reply_to(
        request,
        sender=controller_address("main"),
        message_type=HERE_ARE_SETTINGS,
        body={"settings": {"theme": "too-late"}},
        subject=request.subject,
    )
    assert late.in_reply_to == request.message_id


@pytest.mark.asyncio
async def test_event_bus_rejects_wrong_lane() -> None:
    bus = EventBus("hardware_messages")

    with pytest.raises(ValueError, match="Cannot send message"):
        await bus.send(_message())


@pytest.mark.asyncio
async def test_event_bus_rejects_non_deckr_message() -> None:
    bus = EventBus("plugin_messages")

    with pytest.raises(TypeError, match="DeckrMessage"):
        await bus.send({"legacy": "payload"})  # type: ignore[arg-type]


def test_core_lane_delivery_profiles_are_structured() -> None:
    for lane in ("plugin_messages", "hardware_messages"):
        contract = CORE_LANE_CONTRACTS[lane]
        assert contract.delivery is not None
        assert contract.delivery.persistence == DeliveryPersistence.EPHEMERAL
        assert contract.delivery.guarantee == DeliveryGuarantee.AT_MOST_ONCE
        assert contract.delivery.replay == DeliveryReplay.NONE
        assert not hasattr(contract.route_policy, "delivery_semantics")


def test_plugin_message_delivery_families_cover_core_message_types() -> None:
    contract = CORE_LANE_CONTRACTS["plugin_messages"]
    assert contract.delivery is not None
    family_message_types = frozenset(
        message_type
        for family in contract.delivery.message_families
        for message_type in family.message_types
    )
    assert contract.message_types == family_message_types


@pytest.mark.asyncio
async def test_event_bus_reports_unsupported_core_message_type() -> None:
    bus = EventBus("plugin_messages")
    message = _message().model_copy(update={"message_type": "unsupported"})

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await bus.send(message)
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.message_id == message.message_id
    assert event.reason == (
        "message type 'unsupported' is not supported on lane 'plugin_messages'"
    )


@pytest.mark.asyncio
async def test_event_bus_drops_expired_local_message_with_rejection_event() -> None:
    bus = EventBus("plugin_messages")
    message = _message().model_copy(
        update={
            "created_at": datetime.now(UTC) - timedelta(seconds=1),
            "ttl_ms": 1,
        }
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await bus.send(message)
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.message_id == message.message_id
    assert event.reason == f"message {message.message_id!r} expired"


@pytest.mark.asyncio
async def test_remote_ingress_drops_expired_message_before_sender_claim() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = _message().model_copy(
        update={
            "created_at": datetime.now(UTC) - timedelta(seconds=1),
            "ttl_ms": 1,
        }
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(message, client_id="websocket:host")
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.reason == f"message {message.message_id!r} expired"
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
async def test_transport_egress_drops_expired_message() -> None:
    bus = EventBus("plugin_messages")
    await bus.route_table.claim_endpoint(
        endpoint=controller_address("controller-main"),
        lane="plugin_messages",
        client_id="websocket:controller",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    message = _message().model_copy(
        update={
            "created_at": datetime.now(UTC) - timedelta(seconds=1),
            "ttl_ms": 1,
        }
    )

    async with bus.route_table.subscribe() as events:
        assert (
            await route_targets_client(
                bus.route_table,
                message,
                client_id="websocket:controller",
            )
        ) is False
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == f"message {message.message_id!r} expired"


@pytest.mark.asyncio
async def test_event_bus_reports_slow_local_subscriber_drop() -> None:
    bus = EventBus("plugin_messages", buffer_size=0, send_timeout=0.01)

    async with bus.route_table.subscribe() as events, bus.subscribe():
        await bus.send(_message())
        event = await _next_route_event(events, "messageDropped")

    assert event.message_type == HOST_ONLINE
    assert event.reason == "slowLocalSubscriber"


@pytest.mark.asyncio
async def test_route_table_local_claim_rejects_remote_conflict() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")

    async with bus.route_table.subscribe() as stream:
        await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="local:host:python",
            client_kind="local",
        )
        await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="websocket:remote",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )
        reachable = await _next_route_event(stream, "endpointReachable")
        rejected = await _next_route_event(stream, "endpointClaimRejected")

    assert reachable.event_type == "endpointReachable"
    assert rejected.event_type == "endpointClaimRejected"
    route = await bus.route_table.route_for(endpoint, lane="plugin_messages")
    assert route.client_id == "local:host:python"


@pytest.mark.asyncio
async def test_route_table_first_remote_claim_owns_until_disconnect() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")

    accepted = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:first",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )
    rejected = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="mqtt:second",
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="mqtt-main",
        claim_source="message_sender",
    )

    assert accepted is not None
    assert rejected is None
    route = await bus.route_table.route_for(endpoint, lane="plugin_messages")
    assert route.client_id == "websocket:first"

    await bus.route_table.client_disconnected("websocket:first")
    accepted_after_disconnect = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="mqtt:second",
        client_kind="remote",
        transport_kind="mqtt",
        transport_id="mqtt-main",
        claim_source="message_sender",
    )
    assert accepted_after_disconnect is not None
    route = await bus.route_table.route_for(endpoint, lane="plugin_messages")
    assert route.client_id == "mqtt:second"


@pytest.mark.asyncio
async def test_route_table_local_claim_replaces_remote_claim() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:remote",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    async with bus.route_table.subscribe() as stream:
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="local:host:python",
            client_kind="local",
        )
        unreachable = await _next_route_event(stream, "endpointUnreachable")
        reachable = await _next_route_event(stream, "endpointReachable")

    assert accepted is not None
    assert unreachable.client_id == "websocket:remote"
    assert unreachable.reason == "localClaimReplaced"
    assert reachable.client_id == "local:host:python"
    route = await bus.route_table.route_for(endpoint, lane="plugin_messages")
    assert route.client_id == "local:host:python"
    assert await bus.route_table.routes_for_client("websocket:remote") == ()


@pytest.mark.asyncio
async def test_route_table_trusted_remote_replaces_untrusted_claim() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:untrusted",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    async with bus.route_table.subscribe() as stream:
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="websocket:trusted",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="transport_route",
            trust_status="trusted",
        )
        unreachable = await _next_route_event(stream, "endpointUnreachable")
        reachable = await _next_route_event(stream, "endpointReachable")

    assert accepted is not None
    assert unreachable.client_id == "websocket:untrusted"
    assert unreachable.reason == "higherAuthorityClaimReplaced"
    assert reachable.route is not None
    assert reachable.route.trust_status == "trusted"
    route = await bus.route_table.route_for(endpoint, lane="plugin_messages")
    assert route.client_id == "websocket:trusted"


@pytest.mark.asyncio
async def test_remote_route_claim_stores_soft_lease_timestamps() -> None:
    bus = EventBus("plugin_messages")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    route = await bus.route_table.claim_endpoint(
        endpoint=host_address("python"),
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
        now=now,
    )

    assert route is not None
    assert route.claimed_at == now
    assert route.last_seen_at == now
    assert route.lease_duration_ms == DEFAULT_ROUTE_LEASE_DURATION_MS
    assert route.lease_expires_at == now + timedelta(
        milliseconds=DEFAULT_ROUTE_LEASE_DURATION_MS
    )


@pytest.mark.asyncio
async def test_route_lease_duration_bounds_are_authoritative() -> None:
    bus = EventBus("plugin_messages")

    with pytest.raises(ValueError, match="at least"):
        await bus.route_table.claim_endpoint(
            endpoint=host_address("too-fast"),
            lane="plugin_messages",
            client_id="websocket:too-fast",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="transport_route",
            lease_duration_ms=MIN_ROUTE_LEASE_DURATION_MS - 1,
        )

    with pytest.raises(ValueError, match="at most"):
        await bus.route_table.claim_endpoint(
            endpoint=host_address("too-slow"),
            lane="plugin_messages",
            client_id="websocket:too-slow",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="transport_route",
            lease_duration_ms=MAX_ROUTE_LEASE_DURATION_MS + 1,
        )


@pytest.mark.asyncio
async def test_same_client_sender_claim_renews_transport_route() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    endpoint = host_address("test")
    claimed_at = datetime(2026, 1, 1, tzinfo=UTC)
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
        lease_duration_ms=120_000,
        now=claimed_at,
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(_message(), client_id="websocket:host")
        renewed = await _next_route_event(events, "routeLeaseRenewed")
        received = await _next_message(messages, HOST_ONLINE)

    assert received.route.current_client_id == "websocket:host"
    assert renewed.route is not None
    assert renewed.route.claim_source == "transport_route"
    assert renewed.route.claimed_at == claimed_at
    assert renewed.route.last_seen_at > claimed_at
    assert renewed.route.lease_duration_ms == 120_000
    assert renewed.route.lease_expires_at == renewed.route.last_seen_at + timedelta(
        milliseconds=120_000
    )


@pytest.mark.asyncio
async def test_expire_routes_removes_remote_routes_and_reports_route_loss() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    route = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
        now=now,
    )
    assert route is not None
    assert route.lease_expires_at is not None

    async with bus.route_table.subscribe() as events:
        expired = await bus.route_table.expire_routes(
            now=route.lease_expires_at + timedelta(milliseconds=1)
        )
        route_expired = await _next_route_event(events, "routeExpired")
        unreachable = await _next_route_event(events, "endpointUnreachable")

    assert expired == (route,)
    assert route_expired.reason == "leaseExpired"
    assert unreachable.reason == "leaseExpired"
    assert await bus.route_table.route_for(endpoint, lane="plugin_messages") is None


@pytest.mark.asyncio
async def test_local_routes_do_not_lease_expire() -> None:
    bus = EventBus("plugin_messages")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    route = await bus.route_table.claim_endpoint(
        endpoint=host_address("local"),
        lane="plugin_messages",
        client_id="local:host:local",
        client_kind="local",
        now=now,
    )

    assert route is not None
    assert route.lease_expires_at is None
    assert await bus.route_table.expire_routes(now=now + timedelta(days=1)) == ()
    assert (
        await bus.route_table.route_for(host_address("local"), lane="plugin_messages")
    ) == route


@pytest.mark.asyncio
async def test_endpoint_capability_events_do_not_change_route_selection() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")

    async with bus.route_table.subscribe() as events:
        route = await bus.route_table.claim_endpoint(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="websocket:host",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="transport_route",
            capabilities=("actions",),
        )
        advertised = await _next_route_event(
            events,
            "endpointCapabilitiesAdvertised",
        )

    assert route is not None
    assert advertised.route is not None
    assert advertised.route.capabilities == ("actions",)

    async with bus.route_table.subscribe() as events:
        changed_route = await bus.route_table.update_endpoint_capabilities(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="websocket:host",
            capabilities=("actions", "settings"),
        )
        changed = await _next_route_event(events, "endpointCapabilitiesChanged")

    assert changed_route is not None
    assert changed.route is not None
    assert changed.route.capabilities == ("actions", "settings")
    assert (
        await bus.route_table.route_for(endpoint, lane="plugin_messages")
    ).client_id == "websocket:host"

    async with bus.route_table.subscribe() as events:
        withdrawn_route = await bus.route_table.update_endpoint_capabilities(
            endpoint=endpoint,
            lane="plugin_messages",
            client_id="websocket:host",
            capabilities=(),
        )
        withdrawn = await _next_route_event(events, "endpointCapabilitiesWithdrawn")

    assert withdrawn_route is not None
    assert withdrawn.route is not None
    assert withdrawn.route.capabilities == ()
    assert (
        await bus.route_table.route_for(endpoint, lane="plugin_messages")
    ).client_id == "websocket:host"


@pytest.mark.asyncio
async def test_rejected_remote_endpoint_claim_does_not_enter_bus() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("test")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
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
async def test_remote_ingress_rejects_sender_family_before_claim() -> None:
    bus = EventBus("hardware_messages")
    handler = build_lane_handler(
        lane="hardware_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = _message("hardware_messages")

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(message, client_id="websocket:host")
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.message_id == message.message_id
    assert event.message_type == message.message_type
    assert event.sender == message.sender
    assert (
        event.reason == "sender family 'host' is not allowed on lane 'hardware_messages'"
    )
    assert (
        await bus.route_table.route_for(message.sender, lane="hardware_messages") is None
    )


@pytest.mark.asyncio
async def test_remote_ingress_rejects_recipient_family_before_claim() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=host_address("python"),
        recipient=hardware_manager_address("deck"),
        message_type=HOST_ONLINE,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(message, client_id="websocket:host")
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.reason == (
        "recipient family 'hardware_manager' is not allowed on lane 'plugin_messages'"
    )
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved_host_id",
    [BUILTIN_ACTION_PROVIDER_ID, LEGACY_BUILTIN_ACTION_PROVIDER_ID],
)
async def test_remote_ingress_rejects_reserved_sender_before_claim(
    reserved_host_id: str,
) -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=host_address(reserved_host_id),
        recipient=controller_address("controller-main"),
        message_type=HOST_ONLINE,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(message, client_id="websocket:host")
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert "reserved endpoint id" in event.reason
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
async def test_remote_ingress_rejects_reserved_recipient_before_claim() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=host_address(BUILTIN_ACTION_PROVIDER_ID),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events, bus.subscribe() as messages:
        await handler.handle_remote_message(message, client_id="websocket:controller")
        event = await _next_route_event(events, "messageRejected")
        with anyio.move_on_after(0.05) as scope:
            await messages.receive()

    assert scope.cancel_called
    assert event.reason == (
        "reserved endpoint id 'deckr.controller.builtin' cannot be used as "
        "recipient for family 'host' on lane 'plugin_messages'"
    )
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
async def test_remote_ingress_rejects_disallowed_broadcast_scope_before_claim() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=hardware_managers_broadcast(),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events:
        await handler.handle_remote_message(message, client_id="websocket:controller")
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == (
        "broadcast scope 'hardware_managers' is not allowed on lane 'plugin_messages'"
    )
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
async def test_remote_ingress_rejects_broadcast_scope_family_mismatch() -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=host_address("python"),
        recipient=BroadcastTarget(scope="controllers", endpointFamily="host"),
        message_type=HOST_ONLINE,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events:
        await handler.handle_remote_message(message, client_id="websocket:host")
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == (
        "broadcast scope 'controllers' on lane 'plugin_messages' requires "
        "endpoint family 'controller', got 'host'"
    )
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recipient", "reason"),
    [
        (
            plugin_hosts_broadcast(domain="foreign"),
            "broadcast domain 'foreign' is not supported on lane 'plugin_messages'",
        ),
        (
            plugin_hosts_broadcast(hop_limit=0),
            "broadcast hop_limit=0 cannot cross a transport boundary",
        ),
        (
            plugin_hosts_broadcast(hop_limit=2),
            "multi-hop broadcast requires bridge policy",
        ),
    ],
)
async def test_remote_ingress_rejects_broadcast_domain_and_hop_policy(
    recipient,
    reason: str,
) -> None:
    bus = EventBus("plugin_messages")
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=recipient,
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events:
        await handler.handle_remote_message(message, client_id="websocket:controller")
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == reason
    assert (
        await bus.route_table.route_for(message.sender, lane="plugin_messages") is None
    )


@pytest.mark.asyncio
async def test_broadcast_egress_expands_to_clients_with_matching_claimed_routes() -> (
    None
):
    bus = EventBus("plugin_messages")
    host_route = await bus.route_table.claim_endpoint(
        endpoint=host_address("python"),
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    controller_route = await bus.route_table.claim_endpoint(
        endpoint=controller_address("controller-main"),
        lane="plugin_messages",
        client_id="websocket:controller",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=plugin_hosts_broadcast(),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    assert host_route is not None
    assert controller_route is not None
    assert (
        await route_targets_client(bus.route_table, message, client_id="websocket:host")
    ) is True
    assert (
        await route_targets_client(
            bus.route_table,
            message,
            client_id="websocket:controller",
        )
    ) is False


@pytest.mark.asyncio
async def test_extension_broadcast_scope_expands_matching_claimed_routes() -> None:
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.alerts",
                    schema_id="acme.alerts.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset(
                            {"acme_worker", "acme_observer"}
                        ),
                        allowed_sender_families=frozenset({"acme_controller"}),
                        broadcast_targets={"acme_workers": "acme_worker"},
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.alerts", route_table=route_table)
    first_worker = await bus.route_table.claim_endpoint(
        endpoint=endpoint_address("acme_worker", "one"),
        lane="acme.alerts",
        client_id="websocket:worker-one",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    second_worker = await bus.route_table.claim_endpoint(
        endpoint=endpoint_address("acme_worker", "two"),
        lane="acme.alerts",
        client_id="websocket:worker-two",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    observer = await bus.route_table.claim_endpoint(
        endpoint=endpoint_address("acme_observer", "one"),
        lane="acme.alerts",
        client_id="websocket:observer",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    message = _message("acme.alerts").model_copy(
        update={
            "sender": endpoint_address("acme_controller", "main"),
            "recipient": BroadcastTarget(
                scope="acme_workers",
                endpointFamily="acme_worker",
            ),
            "message_type": "alert",
            "subject": entity_subject("acme_alert", alertId="smoke"),
        }
    )

    assert first_worker is not None
    assert second_worker is not None
    assert observer is not None
    assert (
        await route_targets_client(
            bus.route_table,
            message,
            client_id="websocket:worker-one",
        )
    ) is True
    assert (
        await route_targets_client(
            bus.route_table,
            message,
            client_id="websocket:worker-two",
        )
    ) is True
    assert (
        await route_targets_client(
            bus.route_table,
            message,
            client_id="websocket:observer",
        )
    ) is False


@pytest.mark.asyncio
async def test_broadcast_hop_limit_zero_blocks_transport_egress() -> None:
    bus = EventBus("plugin_messages")
    await bus.route_table.claim_endpoint(
        endpoint=host_address("python"),
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=plugin_hosts_broadcast(hop_limit=0),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.route_table.subscribe() as events:
        assert (
            await route_targets_client(
                bus.route_table,
                message,
                client_id="websocket:host",
            )
        ) is False
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == "broadcast hop_limit=0 cannot cross a transport boundary"


@pytest.mark.asyncio
async def test_local_only_message_type_blocks_ingress_and_egress() -> None:
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.private",
                    schema_id="acme.private.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme"}),
                        allowed_sender_families=frozenset({"acme"}),
                        allowed_recipient_families=frozenset({"acme"}),
                        local_only_message_types=frozenset({"private"}),
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.private", route_table=route_table)
    handler = build_lane_handler(
        lane="acme.private",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    message = _message("acme.private").model_copy(
        update={
            "message_type": "private",
            "sender": endpoint_address("acme", "one"),
            "recipient": endpoint_target(endpoint_address("acme", "two")),
        }
    )

    async with bus.route_table.subscribe() as events:
        await handler.handle_remote_message(message, client_id="websocket:one")
        ingress_event = await _next_route_event(events, "messageRejected")

    assert (
        ingress_event.reason
        == "message type 'private' on lane 'acme.private' is local-only"
    )
    assert await bus.route_table.route_for(message.sender, lane="acme.private") is None

    await bus.route_table.claim_endpoint(
        endpoint=endpoint_address("acme", "two"),
        lane="acme.private",
        client_id="websocket:two",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    async with bus.route_table.subscribe() as events:
        assert (
            await route_targets_client(
                bus.route_table,
                message,
                client_id="websocket:two",
            )
        ) is False
        egress_event = await _next_route_event(events, "messageRejected")

    assert (
        egress_event.reason
        == "message type 'private' on lane 'acme.private' is local-only"
    )


@pytest.mark.asyncio
async def test_local_only_message_type_still_delivers_locally() -> None:
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.private",
                    schema_id="acme.private.v1",
                    route_policy=LaneRoutePolicy(
                        local_only_message_types=frozenset({"private"}),
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.private", route_table=route_table)
    message = _message("acme.private").model_copy(update={"message_type": "private"})

    async with bus.subscribe() as messages:
        await bus.send(message)
        received = await messages.receive()

    assert received is message


@pytest.mark.asyncio
async def test_remote_origin_message_is_not_forwarded_to_another_remote_client() -> (
    None
):
    bus = EventBus("plugin_messages")
    await bus.route_table.claim_endpoint(
        endpoint=host_address("python"),
        lane="plugin_messages",
        client_id="websocket:host",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="transport_route",
    )
    handler = build_lane_handler(
        lane="plugin_messages",
        transport_kind="websocket",
        transport_id="ws-main",
        bus=bus,
    )
    inbound = plugin_message(
        sender=controller_address("controller-main"),
        recipient=plugin_hosts_broadcast(),
        message_type=REQUEST_ACTIONS,
        body={},
        subject=entity_subject("test"),
    )

    async with bus.subscribe() as messages:
        await handler.handle_remote_message(inbound, client_id="websocket:controller")
        received = await _next_message(messages, REQUEST_ACTIONS)

    async with bus.route_table.subscribe() as events:
        assert (
            await route_targets_client(
                bus.route_table,
                received,
                client_id="websocket:host",
            )
        ) is False
        event = await _next_route_event(events, "messageRejected")

    assert event.reason == (
        "lane 'plugin_messages' does not allow remote-to-remote forwarding"
    )


@pytest.mark.asyncio
async def test_route_table_disconnect_removes_endpoint_routes() -> None:
    bus = EventBus("plugin_messages")
    endpoint = host_address("python")
    await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:remote",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    async with bus.route_table.subscribe() as stream:
        await bus.route_table.client_disconnected("websocket:remote")
        unreachable = await _next_route_event(stream, "endpointUnreachable")
        disconnected = await _next_route_event(stream, "clientDisconnected")

    assert unreachable.event_type == "endpointUnreachable"
    assert disconnected.event_type == "clientDisconnected"
    assert await bus.route_table.route_for(endpoint, lane="plugin_messages") is None


@pytest.mark.asyncio
async def test_remote_endpoint_claim_requires_explicit_claim_source() -> None:
    bus = EventBus("plugin_messages")

    with pytest.raises(ValueError, match="claim_source"):
        await bus.route_table.claim_endpoint(
            endpoint=host_address("python"),
            lane="plugin_messages",
            client_id="websocket:remote",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
        )


@pytest.mark.asyncio
async def test_route_table_rejects_remote_claim_outside_lane_policy() -> None:
    bus = EventBus("plugin_messages")

    async with bus.route_table.subscribe() as stream:
        rejected = await bus.route_table.claim_endpoint(
            endpoint=hardware_manager_address("deck"),
            lane="plugin_messages",
            client_id="websocket:remote",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )
        event = await _next_route_event(stream, "endpointClaimRejected")

    assert rejected is None
    assert event.rejected_route is not None
    assert event.rejected_route.claim_source == "message_sender"
    assert event.rejected_route.trust_status == "untrusted"
    assert event.reason == (
        "endpoint family 'hardware_manager' cannot be claimed on lane 'plugin_messages'"
    )


@pytest.mark.asyncio
async def test_route_table_rejects_extension_remote_claim_without_policy() -> None:
    bus = EventBus("acme.metrics.events")

    async with bus.route_table.subscribe() as stream:
        rejected = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_worker", "one"),
            lane="acme.metrics.events",
            client_id="websocket:remote",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )
        event = await _next_route_event(stream, "endpointClaimRejected")

    assert rejected is None
    assert event.rejected_route is not None
    assert event.reason == (
        "remote endpoint claims are not allowed on lane 'acme.metrics.events'"
    )


@pytest.mark.asyncio
async def test_route_table_accepts_extension_remote_claim_with_policy() -> None:
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.metrics.events",
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.metrics.events", route_table=route_table)

    accepted = await bus.route_table.claim_endpoint(
        endpoint=endpoint_address("acme_worker", "one"),
        lane="acme.metrics.events",
        client_id="websocket:remote",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    assert accepted is not None
    assert accepted.endpoint == endpoint_address("acme_worker", "one")


@pytest.mark.asyncio
async def test_untrusted_bridged_endpoint_claim_is_rejected() -> None:
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.bridge",
                    schema_id="acme.bridge.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme"}),
                        bridgeable=True,
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.bridge", route_table=route_table)

    async with bus.route_table.subscribe() as events:
        rejected = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme", "behind-bridge"),
            lane="acme.bridge",
            client_id="websocket:bridge",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            direct=False,
            claim_source="transport_route",
            trust_status="trusted",
        )
        event = await _next_route_event(events, "endpointClaimRejected")

    assert rejected is None
    assert event.reason == "bridged endpoint claims require trusted bridge authority"


@pytest.mark.asyncio
async def test_trusted_bridged_endpoint_claim_requires_bridgeable_lane() -> None:
    bus = EventBus("plugin_messages")

    async with bus.route_table.subscribe() as events:
        rejected = await bus.route_table.claim_endpoint(
            endpoint=host_address("behind-bridge"),
            lane="plugin_messages",
            client_id="websocket:bridge",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            direct=False,
            claim_source="transport_route",
            trust_status="trusted",
            trusted_bridge=True,
            authority_id="local-config",
        )
        event = await _next_route_event(events, "endpointClaimRejected")

    assert rejected is None
    assert event.reason == "lane 'plugin_messages' does not allow bridged endpoint claims"


@pytest.mark.asyncio
async def test_trusted_bridge_forwards_bridgeable_extension_lane_and_drops_duplicate() -> (
    None
):
    route_table = RouteTable(
        lane_contracts=LaneContractRegistry(
            [
                LaneContract(
                    lane="acme.bridge",
                    schema_id="acme.bridge.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme"}),
                        allowed_sender_families=frozenset({"acme"}),
                        allowed_recipient_families=frozenset({"acme"}),
                        bridgeable=True,
                    ),
                )
            ]
        )
    )
    bus = EventBus("acme.bridge", route_table=route_table)
    target = endpoint_address("acme", "behind-bridge")
    accepted = await bus.route_table.claim_endpoint(
        endpoint=target,
        lane="acme.bridge",
        client_id="websocket:bridge",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        direct=False,
        claim_source="transport_route",
        trust_status="trusted",
        trusted_bridge=True,
        authority_id="local-config",
    )
    message = _message("acme.bridge").model_copy(
        update={
            "sender": endpoint_address("acme", "origin"),
            "recipient": endpoint_target(target),
            "subject": entity_subject("bridge"),
        }
    )
    received = mark_received_from_client(message, client_id="websocket:origin")

    assert accepted is not None
    assert (
        await route_targets_client(
            bus.route_table,
            received,
            client_id="websocket:bridge",
        )
    ) is True

    async with bus.route_table.subscribe() as events:
        assert (
            await route_targets_client(
                bus.route_table,
                received,
                client_id="websocket:bridge",
            )
        ) is False
        dropped = await _next_route_event(events, "messageDropped")

    assert dropped.reason == "duplicateBridgedMessage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved_host_id",
    [BUILTIN_ACTION_PROVIDER_ID, LEGACY_BUILTIN_ACTION_PROVIDER_ID],
)
async def test_route_table_rejects_reserved_plugin_host_endpoint_claim(
    reserved_host_id: str,
) -> None:
    bus = EventBus("plugin_messages")

    async with bus.route_table.subscribe() as stream:
        rejected = await bus.route_table.claim_endpoint(
            endpoint=host_address(reserved_host_id),
            lane="plugin_messages",
            client_id=f"websocket:{reserved_host_id}",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )
        event = await _next_route_event(stream, "endpointClaimRejected")

    assert rejected is None
    assert event.rejected_route is not None
    assert event.rejected_route.endpoint == host_address(reserved_host_id)
    assert event.reason == (
        f"reserved endpoint id {reserved_host_id!r} cannot be claimed "
        "for family 'host' on lane 'plugin_messages'"
    )


@pytest.mark.asyncio
async def test_route_table_claims_are_lane_scoped() -> None:
    bus = EventBus("plugin_messages")
    endpoint = controller_address("controller-main")

    plugin_route = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="plugin_messages",
        client_id="websocket:plugin",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )
    hardware_route = await bus.route_table.claim_endpoint(
        endpoint=endpoint,
        lane="hardware_messages",
        client_id="websocket:hardware",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    assert plugin_route is not None
    assert hardware_route is not None
    assert plugin_route.lane == "plugin_messages"
    assert plugin_route.scope == "lane"
    assert plugin_route.transport_kind == "websocket"
    assert plugin_route.claim_source == "message_sender"
    assert plugin_route.trust_status == "untrusted"
    assert (
        await bus.route_table.route_for(endpoint, lane="plugin_messages")
    ).client_id == "websocket:plugin"
    assert (
        await bus.route_table.route_for(endpoint, lane="hardware_messages")
    ).client_id == "websocket:hardware"


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
