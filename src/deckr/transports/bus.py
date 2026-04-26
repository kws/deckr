from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from functools import cache
from inspect import isawaitable
from typing import Any, TypeVar

import anyio

from deckr.contracts.lanes import BackpressureHandling
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    endpoint_target,
)
from deckr.transports.routes import RouteTable

logger = logging.getLogger(__name__)

ReplyPredicate = Callable[[DeckrMessage], bool | Awaitable[bool]]


class EventBus:
    def __init__(
        self,
        lane: str,
        buffer_size: int = 100,
        *,
        send_timeout: float = 0.25,
        route_table: RouteTable | None = None,
    ):
        self.lane = lane
        self.route_table = route_table or RouteTable()
        self._buffer_size = buffer_size
        self._send_timeout = send_timeout
        self._lock = anyio.Lock()
        self._subscribers: dict[str, anyio.abc.ObjectSendStream[DeckrMessage]] = {}

    async def send(self, message: DeckrMessage) -> None:
        if not isinstance(message, DeckrMessage):
            raise TypeError("EventBus.send requires a DeckrMessage")
        if message.lane != self.lane:
            raise ValueError(
                f"Cannot send message for lane {message.lane!r} on bus {self.lane!r}"
            )
        if await self.route_table.local_message_rejection_reason(message) is not None:
            return
        # Snapshot subscribers quickly under lock
        async with self._lock:
            subs = list(self._subscribers.items())

        # Deliver outside lock. Choose a policy for slow subscribers.
        to_drop: list[str] = []

        for sid, send_stream in subs:
            # "Drop if slow": do not allow a slow subscriber to block the bus.
            with anyio.move_on_after(self._send_timeout) as scope:
                await send_stream.send(message)
            if scope.cancel_called:
                # Subscriber buffer full or send would block
                to_drop.append(sid)

        # evict slow subscribers
        if to_drop:
            delivery = self.route_table.delivery_for_lane(self.lane)
            local_backpressure = (
                delivery.local_backpressure if delivery is not None else None
            )
            if (
                local_backpressure is not None
                and local_backpressure != BackpressureHandling.DROP_SUBSCRIBER
            ):
                return
            logger.warning(
                "EventBus dropped %d slow subscriber(s) while delivering %s",
                len(to_drop),
                message.message_type,
            )
            for sid in to_drop:
                await self.route_table.message_dropped(
                    message,
                    client_id=sid,
                    reason="slowLocalSubscriber",
                )
            async with self._lock:
                for sid in to_drop:
                    s = self._subscribers.pop(sid, None)
                    if s is not None:
                        await s.aclose()

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        """Send a message and wait for the first accepted correlated reply."""
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        async with self.subscribe() as stream:
            await self.send(message)
            with anyio.fail_after(timeout):
                while True:
                    reply = await stream.receive()
                    if reply.message_id == message.message_id:
                        continue
                    if reply.in_reply_to != message.message_id:
                        continue
                    if accept is not None:
                        accepted = accept(reply)
                        if isawaitable(accepted):
                            accepted = await accepted
                        if not accepted:
                            continue
                    return reply

    async def reply_to(
        self,
        request: DeckrMessage,
        *,
        sender: str | EndpointAddress,
        message_type: str,
        body: Mapping[str, Any],
        subject: EntitySubject,
        causation_id: str | None = None,
    ) -> DeckrMessage:
        """Send a direct reply to ``request`` using Deckr envelope correlation."""
        reply = DeckrMessage(
            lane=request.lane,
            messageType=message_type,
            sender=sender,
            recipient=endpoint_target(request.sender),
            subject=subject,
            body=body,
            inReplyTo=request.message_id,
            causationId=causation_id,
        )
        await self.send(reply)
        return reply

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        sid = str(uuid.uuid4())
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=self._buffer_size
        )

        async with self._lock:
            self._subscribers[sid] = send

        try:
            yield receive
        finally:
            async with self._lock:
                s = self._subscribers.pop(sid, None)
            if s is not None:
                await s.aclose()
            await receive.aclose()

    def to_async_callback(self) -> Callable[[Any], Awaitable[None]]:
        return self.send

    async def claim_local_endpoint(self, endpoint: str | EndpointAddress) -> str:
        client_id = f"local:{self.lane}:{endpoint}"
        await self.route_table.claim_endpoint(
            endpoint=endpoint,
            lane=self.lane,
            client_id=client_id,
            client_kind="local",
        )
        return client_id

    async def withdraw_local_endpoint(
        self,
        *,
        endpoint: str | EndpointAddress,
        client_id: str,
    ) -> None:
        await self.route_table.withdraw_endpoint(
            endpoint=endpoint,
            lane=self.lane,
            client_id=client_id,
        )
        await self.route_table.client_disconnected(client_id)


T = TypeVar("T")


def event_handler(
    event_type: type[T],
) -> Callable[[Callable[[T], Awaitable[Any]]], Callable[[T], Awaitable[Any]]]:
    def decorator(func: Callable[[T], Awaitable[Any]]) -> Callable[[T], Awaitable[Any]]:
        func.__event_type__ = event_type
        return func

    return decorator


@cache
def _handler_names_by_event(cls: type) -> dict[type, tuple[str, ...]]:
    mapping: dict[type, list[str]] = {}
    # base -> derived so overrides are naturally respected by later classes
    for c in reversed(cls.__mro__):
        for name, obj in c.__dict__.items():
            et = getattr(obj, "__event_type__", None)
            if et is not None:
                mapping.setdefault(et, []).append(name)
    return {et: tuple(names) for et, names in mapping.items()}


async def attach_event_handlers(obj: Any, event_bus: EventBus):
    handler_names = _handler_names_by_event(obj.__class__)
    async with event_bus.subscribe() as subscribe:
        async for event in subscribe:
            handlers = handler_names.get(type(event), [])
            for handler_name in handlers:
                handler = getattr(obj, handler_name)
                await handler(event)
