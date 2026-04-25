from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import Any, TypeVar

import anyio

logger = logging.getLogger(__name__)

TRANSPORT_KIND_HEADER = "deckr.transport.kind"
TRANSPORT_ID_HEADER = "deckr.transport.id"
TRANSPORT_REMOTE_ID_HEADER = "deckr.transport.remote_id"
TRANSPORT_LANE_HEADER = "deckr.transport.lane"


@dataclass(frozen=True, slots=True)
class TransportEnvelope:
    message: Any
    headers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class EventBus:
    def __init__(
        self,
        buffer_size: int = 100,
        *,
        send_timeout: float = 0.25,
    ):
        self._buffer_size = buffer_size
        self._send_timeout = send_timeout
        self._lock = anyio.Lock()
        self._subscribers: dict[
            str, anyio.abc.ObjectSendStream[TransportEnvelope]
        ] = {}

    async def send(
        self,
        message: Any,
        *,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        envelope = (
            message
            if isinstance(message, TransportEnvelope) and headers is None
            else TransportEnvelope(message=message, headers=headers or {})
        )
        # Snapshot subscribers quickly under lock
        async with self._lock:
            subs = list(self._subscribers.items())

        # Deliver outside lock. Choose a policy for slow subscribers.
        to_drop: list[str] = []

        for sid, send_stream in subs:
            # "Drop if slow": do not allow a slow subscriber to block the bus.
            with anyio.move_on_after(self._send_timeout) as scope:
                await send_stream.send(envelope)
            if scope.cancel_called:
                # Subscriber buffer full or send would block
                to_drop.append(sid)

        # evict slow subscribers
        if to_drop:
            logger.warning(
                "EventBus dropped %d slow subscriber(s) while delivering %s",
                len(to_drop),
                type(envelope.message).__name__,
            )
            async with self._lock:
                for sid in to_drop:
                    s = self._subscribers.pop(sid, None)
                    if s is not None:
                        await s.aclose()

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[TransportEnvelope]]:
        sid = str(uuid.uuid4())
        send, receive = anyio.create_memory_object_stream[TransportEnvelope](
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
        async for envelope in subscribe:
            event = envelope.message
            handlers = handler_names.get(type(event), [])
            for handler_name in handlers:
                handler = getattr(obj, handler_name)
                await handler(event)
