from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock, Mock

import anyio
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.contracts.lanes import MessageContract, MessageContractRegistry
from deckr.contracts.messages import DeckrMessage
from deckr.runtime import Deckr
from deckr.substrates.nats_kv import KvBucketPolicy


class MockSubscriptionContext:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self._send, self._receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=100
        )

    async def __aenter__(self) -> anyio.abc.ObjectReceiveStream[DeckrMessage]:
        self.entered = True
        return self._receive

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        self.exited = True
        await self._send.aclose()
        await self._receive.aclose()
        return None


def mock_message_bus(
    lane_contracts: MessageContractRegistry,
    *,
    provide_kv: bool = True,
):
    spec = [
        "contract_for",
        "publish",
        "publish_reply",
        "request",
        "subscribe",
        "subscriptions",
    ]
    if provide_kv:
        spec.extend(("kv_bucket", "kv_buckets"))
    bus = Mock(spec_set=spec)
    bus.contract_for.side_effect = lane_contracts.contract_for
    bus.publish = AsyncMock()
    bus.publish_reply = AsyncMock()
    bus.request = AsyncMock()
    bus.subscriptions = []

    def subscribe(*_args, **_kwargs) -> MockSubscriptionContext:
        context = MockSubscriptionContext()
        bus.subscriptions.append(context)
        return context

    bus.subscribe.side_effect = subscribe

    buckets: dict[str, MemoryJsonKvBucket] = {}

    def kv_bucket(policy: KvBucketPolicy) -> MemoryJsonKvBucket:
        bucket = buckets.get(policy.bucket)
        if bucket is None:
            bucket = MemoryJsonKvBucket(
                bucket=policy.bucket,
                buffer_size=100,
                ttl_seconds=policy.ttl_seconds,
            )
            buckets[policy.bucket] = bucket
        return bucket

    if provide_kv:
        bus.kv_bucket.side_effect = kv_bucket
        bus.kv_buckets = buckets
    return bus


def mock_deckr(
    *,
    lane_contracts: MessageContractRegistry | Sequence[MessageContract] = (),
    lanes: Sequence[str] = (),
    provide_kv: bool = True,
) -> Deckr:
    registry = Deckr._build_lane_contracts(lane_contracts, lanes=lanes)  # noqa: SLF001
    return Deckr(
        lane_contracts=registry,
        lanes=lanes,
        message_bus=mock_message_bus(registry, provide_kv=provide_kv),
    )
