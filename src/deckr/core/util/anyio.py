from __future__ import annotations

import heapq
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)
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
S = TypeVar("S")

MAX_COALESCED_STATE_IDENTITIES = 256
MAX_COALESCED_STATE_SUBSCRIPTIONS = 256


class StateSubscriptionLimitExceeded(RuntimeError):
    """Raised when a state broadcaster cannot admit another subscriber."""


@dataclass(frozen=True, slots=True)
class CoalescedStateChange(Generic[K]):
    """Immutable current-state wakeup produced by a state broadcaster."""

    version: int
    current: bool
    changed: frozenset[K]
    resnapshot_required: bool


@dataclass(slots=True, eq=False)
class _StateRegistration(Generic[K]):
    send: anyio.abc.ObjectSendStream[None]
    receive: anyio.abc.ObjectReceiveStream[None]
    version: int
    current: bool
    changed: set[K] = field(default_factory=set)
    resnapshot_required: bool = False
    notified: bool = False
    closed: bool = False


class CoalescedStateSubscription(Generic[K, S]):
    """One admitted broadcaster registration and its atomic initial snapshot."""

    def __init__(
        self,
        broadcaster: CoalescedStateBroadcaster[K],
        registration: _StateRegistration[K],
        initial: S,
    ) -> None:
        self._broadcaster = broadcaster
        self._registration = registration
        self.initial = initial

    def __aiter__(self) -> CoalescedStateSubscription[K, S]:
        return self

    async def __anext__(self) -> CoalescedStateChange[K]:
        try:
            return await self.receive()
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            raise StopAsyncIteration from None

    async def receive(self) -> CoalescedStateChange[K]:
        registration = self._registration
        while True:
            await registration.receive.receive()
            async with self._broadcaster.lock:
                if registration.closed:
                    raise anyio.ClosedResourceError
                # ``notified`` stays true until the pending state is consumed.
                # A publisher racing between the stream receive and this lock
                # therefore coalesces into this same immutable wakeup instead of
                # leaving an empty token behind or losing the wakeup entirely.
                registration.notified = False
                change = CoalescedStateChange(
                    version=registration.version,
                    current=registration.current,
                    changed=frozenset(registration.changed),
                    resnapshot_required=registration.resnapshot_required,
                )
                registration.changed.clear()
                registration.resnapshot_required = False
                return change


class CoalescedStateBroadcaster(Generic[K]):
    """Bounded, non-blocking fanout for versioned current-state wakeups.

    Owners use :attr:`lock` for both their in-memory state commit and
    :meth:`publish_locked`. Subscriber registration and initial snapshot capture
    use that same lock, making the first snapshot atomic with respect to state
    changes. Later wakeups coalesce by identity and never await a reader.
    """

    def __init__(
        self,
        *,
        current: bool = False,
        max_changed: int = MAX_COALESCED_STATE_IDENTITIES,
        max_subscriptions: int = MAX_COALESCED_STATE_SUBSCRIPTIONS,
    ) -> None:
        if max_changed <= 0:
            raise ValueError("max_changed must be greater than zero")
        if max_subscriptions <= 0:
            raise ValueError("max_subscriptions must be greater than zero")
        self.lock = anyio.Lock()
        self._version = 0
        self._current = current
        self._max_changed = max_changed
        self._max_subscriptions = max_subscriptions
        self._registrations: set[_StateRegistration[K]] = set()
        self._closed = False

    @property
    def version(self) -> int:
        return self._version

    @property
    def current(self) -> bool:
        return self._current

    @property
    def subscription_count(self) -> int:
        return len(self._registrations)

    async def publish(
        self,
        changed: Iterable[K] = (),
        *,
        current: bool | None = None,
        resnapshot_required: bool = False,
    ) -> CoalescedStateChange[K]:
        identities = frozenset(changed)
        async with self.lock:
            return self.publish_locked(
                identities,
                current=current,
                resnapshot_required=resnapshot_required,
            )

    def publish_locked(
        self,
        changed: Iterable[K] = (),
        *,
        current: bool | None = None,
        resnapshot_required: bool = False,
    ) -> CoalescedStateChange[K]:
        """Commit one view version while the broadcaster lock is held."""

        if self._closed:
            raise anyio.ClosedResourceError
        identities = frozenset(changed)
        if len(identities) > self._max_changed:
            identities = frozenset()
            resnapshot_required = True
        if current is not None:
            self._current = current
        self._version += 1
        change = CoalescedStateChange(
            version=self._version,
            current=self._current,
            changed=identities,
            resnapshot_required=resnapshot_required,
        )
        for registration in tuple(self._registrations):
            if registration.closed:
                self._registrations.discard(registration)
                continue
            registration.version = change.version
            registration.current = change.current
            if change.resnapshot_required:
                registration.changed.clear()
                registration.resnapshot_required = True
            elif not registration.resnapshot_required:
                registration.changed.update(change.changed)
                if len(registration.changed) > self._max_changed:
                    registration.changed.clear()
                    registration.resnapshot_required = True
            if registration.notified:
                continue
            try:
                registration.send.send_nowait(None)
                registration.notified = True
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                registration.closed = True
                self._registrations.discard(registration)
        return change

    async def capture(self, snapshot: Callable[[int, bool], S]) -> S:
        """Capture owner state and broadcaster metadata under the state lock."""

        async with self.lock:
            if self._closed:
                raise anyio.ClosedResourceError
            return snapshot(self._version, self._current)

    @asynccontextmanager
    async def subscribe(
        self,
        snapshot: Callable[[int, bool], S],
    ) -> AsyncIterator[CoalescedStateSubscription[K, S]]:
        send, receive = anyio.create_memory_object_stream[None](max_buffer_size=1)
        async with self.lock:
            if self._closed:
                await send.aclose()
                await receive.aclose()
                raise anyio.ClosedResourceError
            if len(self._registrations) >= self._max_subscriptions:
                await send.aclose()
                await receive.aclose()
                raise StateSubscriptionLimitExceeded(
                    "state broadcaster supports at most "
                    f"{self._max_subscriptions} simultaneous subscriptions"
                )
            registration = _StateRegistration(
                send=send,
                receive=receive,
                version=self._version,
                current=self._current,
            )
            self._registrations.add(registration)
            initial = snapshot(self._version, self._current)
        subscription = CoalescedStateSubscription(self, registration, initial)
        try:
            async with send, receive:
                yield subscription
        finally:
            async with self.lock:
                registration.closed = True
                self._registrations.discard(registration)

    async def aclose(self) -> None:
        async with self.lock:
            if self._closed:
                return
            self._closed = True
            registrations = tuple(self._registrations)
            self._registrations.clear()
            for registration in registrations:
                registration.closed = True
        for registration in registrations:
            await registration.send.aclose()


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


class CoalescedTrigger:
    """Coalesce bursty async notifications into one delayed handler call."""

    def __init__(self, *, batch_interval: float) -> None:
        if batch_interval <= 0:
            raise ValueError("batch_interval must be greater than zero")
        self._batch_interval = batch_interval
        self._condition = anyio.Condition()
        self._first_reason: str | None = None
        self._reason_count = 0
        self._closed = False

    async def request(self, reason: str) -> None:
        async with self._condition:
            if self._closed:
                return
            if self._reason_count == 0:
                self._first_reason = reason
            self._reason_count += 1
            self._condition.notify()

    async def aclose(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def run(
        self,
        handler: Callable[[str], Awaitable[None]],
        *,
        reason_prefix: str = "coalesced notifications",
    ) -> None:
        next_run_at = 0.0
        while True:
            async with self._condition:
                while self._reason_count == 0 and not self._closed:
                    await self._condition.wait()
                if self._closed:
                    return

                now = anyio.current_time()
                delay = max(0.0, next_run_at - now)

            if delay:
                await anyio.sleep(delay)

            async with self._condition:
                if self._closed:
                    return
                first_reason = self._first_reason
                reason_count = self._reason_count
                self._first_reason = None
                self._reason_count = 0

            next_run_at = anyio.current_time() + self._batch_interval
            await handler(
                _coalesced_reason(
                    reason_prefix,
                    first_reason=first_reason,
                    reason_count=reason_count,
                )
            )


def _coalesced_reason(
    prefix: str,
    *,
    first_reason: str | None,
    reason_count: int,
) -> str:
    if first_reason is None or reason_count <= 0:
        return prefix
    if reason_count == 1:
        return f"{prefix}: {first_reason}"
    return f"{prefix}: {first_reason} (+{reason_count - 1} more)"


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
