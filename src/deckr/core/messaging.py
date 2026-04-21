import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import cache
from typing import Any, TypeVar

import anyio


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
        self._subscribers: dict[str, anyio.abc.ObjectSendStream[Any]] = {}

    async def send(self, event: Any) -> None:
        # Snapshot subscribers quickly under lock
        async with self._lock:
            subs = list(self._subscribers.items())

        # Deliver outside lock. Choose a policy for slow subscribers.
        to_drop: list[str] = []

        for sid, send_stream in subs:
            # "Drop if slow": do not allow a slow subscriber to block the bus.
            with anyio.move_on_after(self._send_timeout) as scope:
                await send_stream.send(event)
            if scope.cancel_called:
                # Subscriber buffer full or send would block
                to_drop.append(sid)

        # evict slow subscribers
        if to_drop:
            async with self._lock:
                for sid in to_drop:
                    s = self._subscribers.pop(sid, None)
                    if s is not None:
                        await s.aclose()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[anyio.abc.ObjectReceiveStream[Any]]:
        sid = str(uuid.uuid4())
        send, receive = anyio.create_memory_object_stream[Any](
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
        async for event in subscribe:
            handlers = handler_names.get(type(event), [])
            for handler_name in handlers:
                handler = getattr(obj, handler_name)
                await handler(event)
