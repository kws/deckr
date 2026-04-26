from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest

from deckr.contracts.lanes import LaneContract, LaneRoutePolicy
from deckr.contracts.messages import host_address
from deckr.runtime import Deckr
from deckr.transports.bus import EventBus


async def _next_route_event(stream, event_type: str):
    with anyio.fail_after(3):
        while True:
            event = await stream.receive()
            if event.event_type == event_type:
                return event


@pytest.mark.asyncio
async def test_deckr_creates_core_lanes_with_one_shared_route_table() -> None:
    async with Deckr(route_expiry_interval=0.01) as deckr:
        plugin_bus = deckr.bus("plugin_messages")
        hardware_bus = deckr.bus("hardware_events")

        assert isinstance(plugin_bus, EventBus)
        assert plugin_bus.route_table is deckr.route_table
        assert hardware_bus.route_table is deckr.route_table
        assert deckr.lanes.require("plugin_messages") is plugin_bus


@pytest.mark.asyncio
async def test_deckr_exposes_route_events() -> None:
    async with Deckr(route_expiry_interval=0.01) as deckr, deckr.route_events() as events:
        await deckr.route_table.claim_endpoint(
            endpoint=host_address("python"),
            lane="plugin_messages",
            client_id="local:host:python",
            client_kind="local",
        )
        event = await _next_route_event(events, "endpointReachable")

    assert event.endpoint == host_address("python")
    assert event.lane == "plugin_messages"


@pytest.mark.asyncio
async def test_deckr_rejects_duplicate_start() -> None:
    deckr = Deckr(route_expiry_interval=0.01)
    assert deckr.is_running is False
    async with deckr:
        assert deckr.is_running is True
        with pytest.raises(RuntimeError, match="already running"):
            await deckr.__aenter__()
    assert deckr.is_running is False


@pytest.mark.asyncio
async def test_managed_runtime_expires_remote_routes_without_disconnect() -> None:
    old = datetime.now(UTC) - timedelta(minutes=2)
    async with Deckr(route_expiry_interval=0.01) as deckr:
        async with deckr.route_events() as events:
            route = await deckr.route_table.claim_endpoint(
                endpoint=host_address("python"),
                lane="plugin_messages",
                client_id="websocket:host",
                client_kind="remote",
                transport_kind="websocket",
                transport_id="ws-main",
                claim_source="transport_route",
                now=old,
            )
            assert route is not None

            expired = await _next_route_event(events, "routeExpired")
            unreachable = await _next_route_event(events, "endpointUnreachable")

        assert expired.reason == "leaseExpired"
        assert unreachable.reason == "leaseExpired"
        assert (
            await deckr.route_table.route_for(
                host_address("python"),
                lane="plugin_messages",
            )
        ) is None


@pytest.mark.asyncio
async def test_managed_runtime_does_not_expire_local_routes() -> None:
    old = datetime.now(UTC) - timedelta(days=1)
    async with Deckr(route_expiry_interval=0.01) as deckr:
        route = await deckr.route_table.claim_endpoint(
            endpoint=host_address("local"),
            lane="plugin_messages",
            client_id="local:host:local",
            client_kind="local",
            now=old,
        )
        await anyio.sleep(0.05)

        assert (
            await deckr.route_table.route_for(
                host_address("local"),
                lane="plugin_messages",
            )
        ) == route


def test_extension_lanes_require_matching_explicit_contracts() -> None:
    lane = "acme.metrics.events"
    contract = LaneContract(
        lane=lane,
        schema_id="acme.metrics.events.v1",
        route_policy=LaneRoutePolicy(
            remote_claim_endpoint_families=frozenset({"acme_worker"}),
        ),
    )

    with pytest.raises(ValueError, match="require lane contracts"):
        Deckr(lanes=(lane,))
    with pytest.raises(ValueError, match="require explicit lanes"):
        Deckr(lane_contracts=(contract,))

    deckr = Deckr(lane_contracts=(contract,), lanes=(lane,))
    assert deckr.bus(lane).lane == lane
    assert deckr.lane_contracts.contract_for(lane) == contract
