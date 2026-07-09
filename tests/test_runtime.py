from __future__ import annotations

import anyio
import pytest
from message_bus_mocks import mock_deckr

from deckr.contracts.lanes import (
    DEFAULT_MESSAGE_CONTRACT_REGISTRY,
    SERVICE_LANE_CONTRACT,
    MessageContract,
    MessageContractRegistry,
)
from deckr.contracts.messages import SERVICES_LANE, service_address
from deckr.runtime import Deckr
from deckr.services import DeckrServices
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.supervised_nats import SupervisedNatsSubstrate


class _ClosableMessageBus:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.closed = True


@pytest.mark.asyncio
async def test_deckr_services_context_builds_with_default_core_lanes() -> None:
    async with (
        mock_deckr() as deckr,
        deckr.endpoint(service_address("action-runtime.python-dev.deckr.demo")) as endpoint,
        deckr.services(endpoint) as services,
    ):
        assert isinstance(services, DeckrServices)


@pytest.mark.asyncio
async def test_deckr_services_context_builds_managed_client() -> None:
    async with (
        mock_deckr(
            lane_contracts=(SERVICE_LANE_CONTRACT,),
            lanes=(SERVICES_LANE,),
        ) as deckr,
        deckr.endpoint(
            service_address("action-runtime.python-dev.deckr.demo")
        ) as endpoint,
        deckr.services(endpoint) as services,
    ):
        assert isinstance(services, DeckrServices)
        assert not hasattr(services, "beacon")
        assert not hasattr(services, "concord")


@pytest.mark.asyncio
async def test_deckr_rejects_duplicate_start() -> None:
    deckr = mock_deckr()
    assert deckr.is_running is False
    async with deckr:
        assert deckr.is_running is True
        with pytest.raises(RuntimeError, match="already running"):
            await deckr.__aenter__()
    assert deckr.is_running is False


@pytest.mark.asyncio
async def test_deckr_exit_closes_message_bus_in_cancelled_scope() -> None:
    bus = _ClosableMessageBus()
    deckr = Deckr(message_bus=bus)

    with anyio.CancelScope() as scope:
        await deckr.__aenter__()
        scope.cancel()
        await deckr.__aexit__(None, None, None)

    assert bus.closed is True
    assert deckr.is_running is False


def test_extension_lanes_require_matching_explicit_contracts() -> None:
    lane = "acme.metrics.events"
    contract = MessageContract(
        lane=lane,
        schema_id="acme.metrics.events.v1",
        allowed_sender_families=frozenset({"acme_worker"}),
        allowed_recipient_families=frozenset({"controller"}),
    )

    with pytest.raises(ValueError, match="require lane contracts"):
        Deckr(lanes=(lane,))
    with pytest.raises(ValueError, match="require explicit lanes"):
        Deckr(lane_contracts=(contract,))

    deckr = mock_deckr(lane_contracts=(contract,), lanes=(lane,))
    assert deckr.lane(lane).name == lane
    assert deckr.lane_contracts.contract_for(lane) == contract


def test_message_contract_registry_rejects_duplicates_and_unknown_lanes() -> None:
    contract = MessageContract(lane="acme.metrics.events")

    with pytest.raises(ValueError, match="Duplicate message contract"):
        MessageContractRegistry((contract, contract))

    with pytest.raises(LookupError, match="not registered"):
        MessageContractRegistry().contract_for("acme.metrics.events")


def test_nats_substrates_expose_message_bus_contract_lookup() -> None:
    expected = DEFAULT_MESSAGE_CONTRACT_REGISTRY.contract_for(SERVICES_LANE)

    nats = NatsSubstrate(lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY)
    supervised = SupervisedNatsSubstrate(
        lane_contracts=DEFAULT_MESSAGE_CONTRACT_REGISTRY
    )

    assert nats.contract_for(SERVICES_LANE) == expected
    assert supervised.contract_for(SERVICES_LANE) == expected
