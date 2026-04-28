from __future__ import annotations

import pytest

from deckr.contracts.lanes import LaneContract
from deckr.lanes import Lane
from deckr.runtime import Deckr


@pytest.mark.asyncio
async def test_deckr_creates_core_endpoint_bound_lanes() -> None:
    async with Deckr() as deckr:
        plugin_lane = deckr.lane("plugin_messages")
        hardware_lane = deckr.lane("hardware_messages")

    assert isinstance(plugin_lane, Lane)
    assert isinstance(hardware_lane, Lane)
    assert deckr.lanes.require("plugin_messages") is plugin_lane


@pytest.mark.asyncio
async def test_deckr_rejects_duplicate_start() -> None:
    deckr = Deckr()
    assert deckr.is_running is False
    async with deckr:
        assert deckr.is_running is True
        with pytest.raises(RuntimeError, match="already running"):
            await deckr.__aenter__()
    assert deckr.is_running is False


def test_extension_lanes_require_matching_explicit_contracts() -> None:
    lane = "acme.metrics.events"
    contract = LaneContract(
        lane=lane,
        schema_id="acme.metrics.events.v1",
        allowed_sender_families=frozenset({"acme_worker"}),
        allowed_recipient_families=frozenset({"controller"}),
    )

    with pytest.raises(ValueError, match="require lane contracts"):
        Deckr(lanes=(lane,))
    with pytest.raises(ValueError, match="require explicit lanes"):
        Deckr(lane_contracts=(contract,))

    deckr = Deckr(lane_contracts=(contract,), lanes=(lane,))
    assert deckr.lane(lane).name == lane
    assert deckr.lane_contracts.contract_for(lane) == contract
