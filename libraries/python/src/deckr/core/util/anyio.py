from __future__ import annotations

import heapq
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Generic,
    TypeVar,
)

import anyio
from anyio.abc import TaskGroup

logger = logging.getLogger(__name__)

K = TypeVar("K")
V = TypeVar("V")


class ConcurrentModificationError(RuntimeError):
    """Raised when the map is modified during iteration."""

    pass


class AsyncMap(Generic[K, V]):
    """Thread-safe async map with concurrent modification detection.

    Provides a dictionary-like interface with async operations protected by
    a lock. Iteration detects concurrent modifications and raises
    ConcurrentModificationError if the map is modified during iteration.
    """

    def __init__(
        self, initial_values: Mapping[K, V] | None = None, proxy: bool = False
    ):
        """Initialize an AsyncMap.

        Args:
            initial_values: Initial key-value pairs to populate the map
            proxy: If True, act as a proxy to initial_values instead of copying.
                WARNING: External modifications to the underlying map will bypass
                thread-safety guarantees and break concurrent modification detection.
                Only use if you can guarantee the underlying map won't be modified
                externally and you need the memory/performance benefits.
        """
        assert not (
            proxy and initial_values is None
        ), "initial_values must be provided if proxy is True"
        if proxy and initial_values is not None:
            self._map = initial_values
            self._is_proxy = True
        else:
            self._map = dict(initial_values) if initial_values is not None else {}
            self._is_proxy = False
        self._lock = anyio.Lock()
        self._version = 0

    async def get(self, key: K, default: V | None = None) -> V:
        """Get the value for a key, or default if not present."""
        async with self._lock:
            return self._map.get(key, default)

    async def set(self, key: K, value: V) -> None:
        """Set a key-value pair."""
        async with self._lock:
            self._map[key] = value
            self._version += 1

    async def get_and_set(self, key: K, value: V) -> V | None:
        """Get the value for a key, and set a new value."""
        async with self._lock:
            old_value = self._map.get(key)
            self._map[key] = value
            self._version += 1
        return old_value

    async def items(self) -> list[tuple[K, V]]:
        """Return a list of (key, value) pairs."""
        async with self._lock:
            return list(self._map.items())

    async def keys(self) -> list[K]:
        """Return a list of all keys."""
        async with self._lock:
            return list(self._map.keys())

    async def values(self) -> list[V]:
        """Return a list of all values."""
        async with self._lock:
            return list(self._map.values())

    async def clear(self) -> None:
        """Remove all items from the map."""
        async with self._lock:
            self._map.clear()
            self._version += 1

    async def has_key(self, key: K) -> bool:
        """Check if a key exists in the map."""
        async with self._lock:
            return key in self._map

    async def delete(self, key: K) -> None:
        """Delete a key from the map. Does nothing if key doesn't exist."""
        async with self._lock:
            if key in self._map:
                del self._map[key]
                self._version += 1

    async def pop(self, key: K, default: V | None = None) -> V | None:
        """Remove and return the value for a key.

        Args:
            key: The key to remove
            default: Value to return if key doesn't exist (default: None)

        Returns:
            The value for the key, or default if key doesn't exist
        """
        async with self._lock:
            if key in self._map:
                value = self._map.pop(key)
                self._version += 1
                return value
            return default

    @asynccontextmanager
    async def lock(self) -> AsyncGenerator[None, None]:
        """Allows direct access to the underlying map in a single 'transaction'.
        Since the map may be modified during the transaction, the version is always incremented.
        """
        async with self._lock:
            yield self._map
            self._version += 1

    async def __aiter__(self) -> AsyncIterator[tuple[K, V]]:
        """Iterate over (key, value) pairs.

        Raises ConcurrentModificationError if the map is modified during iteration.
        """
        # Capture version and snapshot under lock
        async with self._lock:
            initial_version = self._version
            items = list(self._map.items())

        # Iterate outside lock, checking for concurrent modifications
        for key, value in items:
            async with self._lock:
                if self._version != initial_version:
                    raise ConcurrentModificationError(
                        "AsyncMap was modified during iteration"
                    )
            yield key, value


T = TypeVar("T")


@dataclass(order=True)
class _Entry(Generic[T]):
    due: float
    seq: int
    item: T = field(compare=False)


class ScheduledQueue(Generic[T]):
    def __init__(self) -> None:
        self._cv = anyio.Condition()
        self._heap: list[_Entry[T]] = []
        self._seq: int = 0

    async def put_at(self, due: float, item: T) -> None:
        async with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, _Entry(due=due, seq=self._seq, item=item))
            self._cv.notify()

    async def put_after(self, delay: float, item: T) -> None:
        await self.put_at(anyio.current_time() + delay, item)

    async def get(self) -> T:
        async with self._cv:
            while True:
                if not self._heap:
                    await self._cv.wait()
                    continue

                head = self._heap[0]
                now = anyio.current_time()

                if head.due <= now:
                    return heapq.heappop(self._heap).item

                timeout = head.due - now
                try:
                    with anyio.fail_after(timeout):
                        await self._cv.wait()
                except TimeoutError:
                    # due time reached; loop will pop next iteration
                    pass

    async def peek_due(self) -> float | None:
        async with self._cv:
            return self._heap[0].due if self._heap else None

    async def qsize(self) -> int:
        async with self._cv:
            return len(self._heap)


class SubscribableQueue(Generic[T]):
    """A FIFO queue that distributes events to multiple subscribers.

    When an event is pushed, it is distributed to all active subscribers
    without blocking. If a subscriber's buffer is full, that subscriber
    is skipped. If all subscribers are full, a RuntimeError is raised.

    Subscribers are automatically cleaned up when they close.
    """

    class SubscriberBufferFullError(RuntimeError):
        pass

    def __init__(
        self, *, maxsize: int = 100, fail_on_undelivered: bool = False
    ) -> None:
        """Initialize a SubscribableQueue.

        Args:
            maxsize: Maximum buffer size for each subscriber's queue.
                    0 means unbounded. Default is 0.
        """
        self._lock = anyio.Lock()
        self._subscribers: dict[
            anyio.abc.ObjectSendStream[T], anyio.abc.ObjectReceiveStream[T]
        ] = {}
        self._maxsize = maxsize
        self._fail_on_undelivered = fail_on_undelivered

    async def push(self, event: T) -> None:
        """Push an event to all subscribers.

        The event is delivered to all active subscribers without blocking.
        If a subscriber's buffer is full, that subscriber is skipped.
        If all subscribers are full or there are no subscribers, a SubscriberBufferFullError is raised.

        Args:
            event: The event to push

        Raises:
            RuntimeError: If all subscribers are full or there are no subscribers
        """
        # Snapshot subscribers and clean up closed ones under lock
        async with self._lock:
            # Snapshot subscribers for sending outside the lock
            subscribers = list(self._subscribers.items())

        # Try to send to all active subscribers (outside lock - send_nowait is thread-safe)
        delivered = False
        to_remove = []
        for send_stream, _ in subscribers:
            try:
                # Try to send without blocking
                send_stream.send_nowait(event)
                delivered = True
            except anyio.WouldBlock:
                # Subscriber buffer is full, skip it
                pass
            except anyio.ClosedResourceError:
                # Subscriber closed, mark for removal
                to_remove.append(send_stream)
            except anyio.BrokenResourceError:
                # Subscriber broken, mark for removal
                to_remove.append(send_stream)

        # Update state under lock
        if to_remove:
            async with self._lock:
                for send_stream in to_remove:
                    self._subscribers.pop(send_stream, None)

        # If we couldn't deliver to any subscriber, fail
        if self._fail_on_undelivered and not delivered and len(subscribers) > 0:
            raise self.SubscriberBufferFullError()

    async def subscribe(
        self, filter: Callable[[T], bool] | None = None
    ) -> AsyncIterator[T]:
        """Subscribe to events from the queue.

        Returns an async iterator that yields events as they are pushed.
        When the iterator is closed, the subscription is automatically removed.

        Args:
            filter: A function that filters events. If None, all events are yielded.

        Yields:
            Events pushed to the queue
        """
        send_stream, recv_stream = anyio.create_memory_object_stream[T](
            max_buffer_size=self._maxsize
        )

        async with self._lock:
            self._subscribers[send_stream] = recv_stream

        try:
            async with recv_stream:
                async for event in recv_stream:
                    if filter is None or filter(event):
                        yield event
        finally:
            # Clean up when subscriber closes
            async with self._lock:
                self._subscribers.pop(send_stream, None)
            await send_stream.aclose()


@dataclass
class EnsureStarted:
    _lock: anyio.Lock
    _tg: TaskGroup | None
    _started: bool
    _ready: anyio.Event

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._tg = None
        self._started = False
        self._ready = anyio.Event()

    @property
    def started(self) -> bool:
        return self._started

    def attach_task_group(self, tg: TaskGroup) -> None:
        self._tg = tg

    async def ensure_started(self, runner: Callable[[], Awaitable[None]]) -> None:
        if self._started:
            await self._ready.wait()
            return

        async with self._lock:
            if not self._started:
                if self._tg is None:
                    raise RuntimeError("No TaskGroup attached")
                self._tg.start_soon(runner)
                self._started = True

        await self._ready.wait()

    def mark_ready(self) -> None:
        self._ready.set()


async def add_signal_handler(tg: anyio.abc.TaskGroup):
    import signal

    cancelled = anyio.get_cancelled_exc_class()

    async def signal_handler():
        try:
            with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                async for sig in signals:
                    logger.info(
                        f"Received {signal.Signals(sig).name}, shutting down gracefully..."
                    )
                    tg.cancel_scope.cancel()
                    return
        except cancelled:
            raise

    tg.start_soon(signal_handler)
