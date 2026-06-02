from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType

import anyio

from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    LaneContract,
    LaneContractRegistry,
)
from deckr.contracts.messages import CORE_LANE_NAMES
from deckr.lanes import Lane, LaneRegistry, LaneSubstrate
from deckr.services.views import ServiceViewStore
from deckr.state import DEFAULT_STATE_STORE_NAME, StateStore, StateStorePolicy
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.nats_kv import KvBucketPolicy


class Deckr:
    def __init__(
        self,
        *,
        lane_contracts: LaneContractRegistry | Sequence[LaneContract] = (),
        lanes: Sequence[str] = (),
        substrate: LaneSubstrate | None = None,
    ) -> None:
        self._lane_contracts = self._build_lane_contracts(
            lane_contracts,
            lanes=lanes,
        )
        self._substrate = substrate or NatsSubstrate(lane_contracts=self._lane_contracts)
        self._lanes = LaneRegistry.from_names(
            tuple(sorted(set(CORE_LANE_NAMES) | set(lanes))),
            lane_contracts=self._lane_contracts,
            substrate=self._substrate,
        )
        self._service_view_stores: dict[tuple[str, float | None], ServiceViewStore] = {}
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
    def is_running(self) -> bool:
        return self._task_group is not None

    def lane(self, name: str) -> Lane:
        return self._lanes.require(name)

    def state(
        self,
        name: str = DEFAULT_STATE_STORE_NAME,
        *,
        policy: StateStorePolicy | None = None,
    ) -> StateStore:
        return self._substrate.state(name, policy=policy)

    def service_view_store(
        self,
        bucket: str,
        *,
        ttl_seconds: float | None = None,
    ) -> ServiceViewStore:
        if self._task_group is None:
            raise RuntimeError("Deckr runtime must be running")
        kv_bucket = getattr(self._substrate, "kv_bucket", None)
        if kv_bucket is None:
            raise RuntimeError("Deckr substrate does not provide NATS KV buckets")
        key = (bucket, ttl_seconds)
        store = self._service_view_stores.get(key)
        if store is None:
            store = ServiceViewStore(
                bucket=kv_bucket(
                    KvBucketPolicy(
                        bucket=bucket,
                        ttl_seconds=ttl_seconds,
                        allow_write_ttl=ttl_seconds is not None,
                        description="service view KV",
                    )
                )
            )
            store.start(self._task_group)
            self._service_view_stores[key] = store
        return store

    async def __aenter__(self) -> Deckr:
        if self._task_group is not None:
            raise RuntimeError("Deckr runtime is already running")
        connect = getattr(self._substrate, "connect", None)
        if connect is not None:
            await connect()
        self._task_group_cm = anyio.create_task_group()
        self._task_group = await self._task_group_cm.__aenter__()
        start = getattr(self._substrate, "start", None)
        if start is not None:
            start(self._task_group)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
        result = None
        if self._task_group_cm is not None:
            result = await self._task_group_cm.__aexit__(exc_type, exc, traceback)
        aclose = getattr(self._substrate, "aclose", None)
        if aclose is not None:
            await aclose()
        self._service_view_stores.clear()
        self._task_group = None
        self._task_group_cm = None
        return result

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
