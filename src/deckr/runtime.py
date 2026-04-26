from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType

import anyio

from deckr.components import LaneRegistry
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    LaneContract,
    LaneContractRegistry,
)
from deckr.contracts.messages import CORE_LANE_NAMES
from deckr.transports.bus import EventBus
from deckr.transports.routes import RouteEvent, RouteTable


class Deckr:
    def __init__(
        self,
        *,
        lane_contracts: LaneContractRegistry | Sequence[LaneContract] = (),
        lanes: Sequence[str] = (),
        route_expiry_interval: float = 1.0,
    ) -> None:
        if route_expiry_interval <= 0:
            raise ValueError("route_expiry_interval must be greater than zero")

        self._lane_contracts = self._build_lane_contracts(
            lane_contracts,
            lanes=lanes,
        )
        self._route_table = RouteTable(lane_contracts=self._lane_contracts)
        self._lanes = LaneRegistry.from_names(
            tuple(sorted(set(CORE_LANE_NAMES) | set(lanes))),
            route_table=self._route_table,
        )
        self._route_expiry_interval = route_expiry_interval
        self._task_group_cm: AbstractAsyncContextManager[anyio.abc.TaskGroup] | None = (
            None
        )
        self._task_group: anyio.abc.TaskGroup | None = None

    @property
    def lane_contracts(self) -> LaneContractRegistry:
        return self._lane_contracts

    @property
    def lanes(self) -> LaneRegistry:
        return self._lanes

    @property
    def route_table(self) -> RouteTable:
        return self._route_table

    @property
    def is_running(self) -> bool:
        return self._task_group is not None

    def bus(self, name: str) -> EventBus:
        return self._lanes.require(name)

    def route_events(
        self,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[RouteEvent]]:
        return self._route_table.subscribe()

    async def __aenter__(self) -> Deckr:
        if self._task_group is not None:
            raise RuntimeError("Deckr runtime is already running")
        self._task_group_cm = anyio.create_task_group()
        self._task_group = await self._task_group_cm.__aenter__()
        self._task_group.start_soon(self._expire_routes)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
        if self._task_group_cm is None:
            return None
        try:
            return await self._task_group_cm.__aexit__(exc_type, exc, traceback)
        finally:
            self._task_group = None
            self._task_group_cm = None

    async def _expire_routes(self) -> None:
        while True:
            await self._route_table.expire_routes()
            await anyio.sleep(self._route_expiry_interval)

    @staticmethod
    def _build_lane_contracts(
        lane_contracts: LaneContractRegistry | Sequence[LaneContract],
        *,
        lanes: Sequence[str],
    ) -> LaneContractRegistry:
        contracts = dict(CORE_LANE_CONTRACTS)
        if isinstance(lane_contracts, LaneContractRegistry):
            provided = tuple(lane_contracts.contracts.values())
        else:
            provided = tuple(lane_contracts)

        for contract in provided:
            if contract.lane in CORE_LANE_CONTRACTS:
                if CORE_LANE_CONTRACTS[contract.lane] != contract:
                    raise ValueError(
                        f"Deckr lane contract input must not override "
                        f"core lane contract {contract.lane!r}"
                    )
                continue
            existing = contracts.get(contract.lane)
            if existing is not None and existing != contract:
                raise ValueError(
                    f"Duplicate lane contract {contract.lane!r} declarations "
                    "must be identical"
                )
            contracts[contract.lane] = existing or contract

        extension_lanes = set(lanes) - set(CORE_LANE_NAMES)
        missing_contracts = sorted(
            lane for lane in extension_lanes if lane not in contracts
        )
        if missing_contracts:
            names = ", ".join(repr(lane) for lane in missing_contracts)
            raise ValueError(f"Extension lane(s) require lane contracts: {names}")

        unlisted_contracts = sorted(
            lane
            for lane in contracts
            if lane not in CORE_LANE_CONTRACTS and lane not in lanes
        )
        if unlisted_contracts:
            names = ", ".join(repr(lane) for lane in unlisted_contracts)
            raise ValueError(
                f"Extension lane contract(s) require explicit lanes: {names}"
            )

        return LaneContractRegistry(contracts.values())
