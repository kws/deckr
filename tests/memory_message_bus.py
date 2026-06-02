from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import anyio
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    MessageContract,
    MessageContractRegistry,
)
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.runtime import Deckr
from deckr.substrates.nats_kv import KvBucketPolicy


def memory_deckr(
    *,
    lane_contracts: MessageContractRegistry | Sequence[MessageContract] = (),
    lanes: Sequence[str] = (),
) -> Deckr:
    registry = _message_contract_registry(lane_contracts)
    return Deckr(
        lane_contracts=registry,
        lanes=lanes,
        message_bus=MemoryMessageBus(lane_contracts=registry),
    )


def _message_contract_registry(
    lane_contracts: MessageContractRegistry | Sequence[MessageContract],
) -> MessageContractRegistry:
    if isinstance(lane_contracts, MessageContractRegistry):
        provided = tuple(lane_contracts.contracts.values())
    else:
        provided = tuple(lane_contracts)
    contracts = dict(CORE_LANE_CONTRACTS)
    contracts.update((contract.lane, contract) for contract in provided)
    return MessageContractRegistry(contracts.values())


class MemoryMessageBus:
    def __init__(
        self,
        *,
        lane_contracts: MessageContractRegistry,
        buffer_size: int = 100,
    ) -> None:
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._lock = anyio.Lock()
        self._subscribers: dict[
            tuple[str, EndpointAddress, str],
            set[anyio.abc.ObjectSendStream[DeckrMessage]],
        ] = {}
        self._kv_buckets: dict[str, MemoryJsonKvBucket] = {}

    async def publish(self, message: DeckrMessage) -> None:
        contract = self._lane_contracts.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        async with self._lock:
            subscribers = [
                (endpoint, endpoint_session_id, tuple(streams))
                for (
                    lane,
                    endpoint,
                    endpoint_session_id,
                ), streams in self._subscribers.items()
                if lane == message.lane
            ]
        for endpoint, endpoint_session_id, streams in subscribers:
            if not message_is_deliverable(
                message,
                endpoint=endpoint,
                endpoint_session_id=endpoint_session_id,
                contract=contract,
            ):
                continue
            for stream in streams:
                await stream.send(message)

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None:
        del request
        await self.publish(message)

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        async with self.subscribe(
            message.lane,
            message.sender,
            endpoint_session_id=message.sender_session_id,
        ) as stream:
            await self.publish(message)
            with anyio.fail_after(timeout):
                while True:
                    reply = await stream.receive()
                    if await reply_is_accepted(reply, request=message, accept=accept):
                        return reply

    @asynccontextmanager
    async def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
        *,
        endpoint_session_id: str,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=self._buffer_size
        )
        key = (lane, endpoint, endpoint_session_id)
        async with self._lock:
            self._subscribers.setdefault(key, set()).add(send)
        try:
            yield receive
        finally:
            async with self._lock:
                streams = self._subscribers.get(key)
                if streams is not None:
                    streams.discard(send)
                    if not streams:
                        self._subscribers.pop(key, None)
            await send.aclose()
            await receive.aclose()

    def kv_bucket(self, policy: KvBucketPolicy) -> MemoryJsonKvBucket:
        bucket = self._kv_buckets.get(policy.bucket)
        if bucket is None:
            bucket = MemoryJsonKvBucket(
                bucket=policy.bucket,
                buffer_size=self._buffer_size,
            )
            self._kv_buckets[policy.bucket] = bucket
        return bucket
