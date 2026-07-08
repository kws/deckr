from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType

import anyio

from deckr.beacon import BEACON_ADVERTISEMENT_STORE_POLICY, Beacon
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
)
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    MessageContract,
    MessageContractRegistry,
)
from deckr.contracts.messages import CORE_LANE_NAMES, SERVICES_LANE, EndpointAddress
from deckr.lanes import (
    EndpointSession,
    Lane,
    LaneRegistry,
    MessageBus,
    endpoint_session,
)
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.nats_kv import KvBucketPolicy, NatsJsonKvBucket


class Deckr:
    def __init__(
        self,
        *,
        lane_contracts: MessageContractRegistry | Sequence[MessageContract] = (),
        lanes: Sequence[str] = (),
        message_bus: MessageBus | None = None,
    ) -> None:
        self._lane_contracts = self._build_lane_contracts(
            lane_contracts,
            lanes=lanes,
        )
        self._message_bus = message_bus or NatsSubstrate(
            lane_contracts=self._lane_contracts
        )
        self._lanes = LaneRegistry.from_names(
            tuple(sorted(set(CORE_LANE_NAMES) | set(lanes))),
            message_contracts=self._lane_contracts,
        )
        self._task_group_cm: AbstractAsyncContextManager[anyio.abc.TaskGroup] | None = (
            None
        )
        self._task_group: anyio.abc.TaskGroup | None = None
        self._beacon: Beacon | None = None
        self._concord: Concord | None = None

    @property
    def lane_contracts(self) -> MessageContractRegistry:
        return self._lane_contracts

    @property
    def lanes(self) -> LaneRegistry:
        return self._lanes

    @property
    def is_running(self) -> bool:
        return self._task_group is not None

    def lane(self, name: str) -> Lane:
        return self._lanes.require(name)

    @asynccontextmanager
    async def endpoint(
        self,
        address: str | EndpointAddress,
        *,
        session_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> AsyncIterator[EndpointSession]:
        session = endpoint_session(
            address=address,
            session_id=session_id,
            metadata=metadata,
            message_bus=self._message_bus,
        )
        try:
            yield session
        finally:
            await session.aclose()

    @property
    def beacon(self) -> Beacon:
        if self._beacon is None:
            raise RuntimeError("Deckr runtime does not provide Beacon")
        return self._beacon

    @property
    def concord(self) -> Concord:
        if self._concord is None:
            raise RuntimeError("Deckr runtime does not provide Concord")
        return self._concord

    def kv_bucket(self, policy: KvBucketPolicy) -> NatsJsonKvBucket:
        kv_bucket = getattr(self._message_bus, "kv_bucket", None)
        if kv_bucket is None:
            raise RuntimeError("Deckr message bus does not provide NATS KV buckets")
        return kv_bucket(policy)

    @asynccontextmanager
    async def services(
        self,
        endpoint: EndpointSession,
        *,
        service_use_token_refresh_seconds: float = (
            DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
        ),
    ) -> AsyncIterator[object]:
        self.lane(SERVICES_LANE)
        if self._task_group is None:
            raise RuntimeError("Deckr runtime is not running")
        if self._beacon is None or self._concord is None:
            raise RuntimeError("Deckr runtime does not provide service-use support")

        from deckr.services import DeckrServices

        services = DeckrServices(
            endpoint=endpoint,
            beacon=self._beacon,
            concord=self._concord,
            task_group=self._task_group,
            kv_bucket_for=self.kv_bucket,
            service_use_token_refresh_seconds=service_use_token_refresh_seconds,
        )
        try:
            yield services
        finally:
            await services.aclose()

    async def __aenter__(self) -> Deckr:
        if self._task_group is not None:
            raise RuntimeError("Deckr runtime is already running")
        connect = getattr(self._message_bus, "connect", None)
        if connect is not None:
            await connect()
        self._task_group_cm = anyio.create_task_group()
        self._task_group = await self._task_group_cm.__aenter__()
        start = getattr(self._message_bus, "start", None)
        if start is not None:
            start(self._task_group)
        kv_bucket = getattr(self._message_bus, "kv_bucket", None)
        if kv_bucket is not None:
            self._beacon = Beacon(kv_bucket(BEACON_ADVERTISEMENT_STORE_POLICY))
            self._beacon.start(self._task_group)
            self._concord = Concord(
                kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
                kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
                kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
            )
            self._concord.start(self._task_group)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        result = None
        cleanup_errors: list[BaseException] = []

        if self._task_group is not None:
            self._task_group.cancel_scope.shield = True
            if self._beacon is not None:
                try:
                    await self._beacon.aclose()
                except BaseException as err:
                    cleanup_errors.append(err)
            if self._concord is not None:
                try:
                    await self._concord.aclose()
                except BaseException as err:
                    cleanup_errors.append(err)
            self._task_group.cancel_scope.cancel()
            if self._task_group_cm is not None:
                try:
                    result = await self._task_group_cm.__aexit__(
                        exc_type,
                        exc,
                        traceback,
                    )
                except BaseException as err:
                    cleanup_errors.append(err)
        else:
            with anyio.CancelScope(shield=True):
                if self._beacon is not None:
                    try:
                        await self._beacon.aclose()
                    except BaseException as err:
                        cleanup_errors.append(err)
                if self._concord is not None:
                    try:
                        await self._concord.aclose()
                    except BaseException as err:
                        cleanup_errors.append(err)

        with anyio.CancelScope(shield=True):
            try:
                aclose = getattr(self._message_bus, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except BaseException as err:
                        cleanup_errors.append(err)
            finally:
                self._beacon = None
                self._concord = None
                self._task_group = None
                self._task_group_cm = None

        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise BaseExceptionGroup("Deckr runtime cleanup failed", cleanup_errors)
        return result

    @staticmethod
    def _build_lane_contracts(
        lane_contracts: MessageContractRegistry | Sequence[MessageContract],
        *,
        lanes: Sequence[str],
    ) -> MessageContractRegistry:
        contracts = dict(CORE_LANE_CONTRACTS)
        if isinstance(lane_contracts, MessageContractRegistry):
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

        return MessageContractRegistry(contracts.values())
