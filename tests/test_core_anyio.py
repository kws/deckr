from __future__ import annotations

import anyio
import pytest

from deckr.core.util.anyio import (
    AsyncMap,
    CoalescedTrigger,
    ConcurrentModificationError,
    EnsureStarted,
    ScheduledQueue,
    SubscribableQueue,
)


@pytest.mark.asyncio
async def test_async_map_proxy_mutation_and_iteration_conflict() -> None:
    with pytest.raises(AssertionError, match="initial_values"):
        AsyncMap(proxy=True)

    backing = {"one": 1, "two": 2}
    mapping = AsyncMap(backing, proxy=True)

    assert await mapping.pop("one") == 1
    assert "one" not in backing
    assert await mapping.pop("missing", 99) == 99
    await mapping.delete("two")
    assert backing == {}
    async with mapping.lock() as locked:
        locked["three"] = 3
    assert backing == {"three": 3}

    iterator = AsyncMap({"a": 1, "b": 2}).__aiter__()
    assert await anext(iterator) == ("a", 1)
    await iterator.aclose()

    mapping = AsyncMap({"a": 1, "b": 2})
    iterator = mapping.__aiter__()
    assert await anext(iterator) == ("a", 1)
    await mapping.set("c", 3)
    with pytest.raises(ConcurrentModificationError):
        await anext(iterator)


@pytest.mark.asyncio
async def test_coalesced_trigger_validation_close_and_reason_text() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        CoalescedTrigger(batch_interval=0)

    closed = CoalescedTrigger(batch_interval=0.01)
    await closed.aclose()
    await closed.run(AsyncMockHandler())

    trigger = CoalescedTrigger(batch_interval=0.01)
    reasons: list[str] = []

    async def handler(reason: str) -> None:
        reasons.append(reason)
        if len(reasons) == 2:
            await trigger.aclose()

    async def run_trigger() -> None:
        await trigger.run(handler, reason_prefix="batch")

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_trigger)
        await trigger.request("first")
        await _wait_until(lambda: reasons == ["batch: first"])
        await trigger.request("second")
        await trigger.request("third")
        await _wait_until(lambda: reasons[-1:] == ["batch: second (+1 more)"])
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_scheduled_queue_due_ordering_and_earlier_wakeup() -> None:
    queue: ScheduledQueue[str] = ScheduledQueue()
    now = anyio.current_time()
    await queue.put_at(now + 0.02, "second")
    await queue.put_at(now, "first")

    assert await queue.get() == "first"
    assert await queue.get() == "second"

    result: list[str] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(_receive_scheduled, queue, result)
        await queue.put_after(10, "late")
        await anyio.sleep(0)
        await queue.put_after(0, "early")
        await _wait_until(lambda: result == ["early"])
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_subscribable_queue_cleanup_full_and_filtered_subscription() -> None:
    queue: SubscribableQueue[str] = SubscribableQueue(maxsize=1)
    send, receive = anyio.create_memory_object_stream[str](1)
    queue._subscribers[send] = receive  # noqa: SLF001
    await send.aclose()

    await queue.push("ignored")

    assert queue._subscribers == {}  # noqa: SLF001

    queue = SubscribableQueue[str](maxsize=1, fail_on_undelivered=True)
    send, receive = anyio.create_memory_object_stream[str](1)
    queue._subscribers[send] = receive  # noqa: SLF001
    await queue.push("first")
    with pytest.raises(SubscribableQueue.SubscriberBufferFullError):
        await queue.push("second")
    await send.aclose()
    await receive.aclose()

    filtered = SubscribableQueue[int]()
    received: list[int] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(_receive_filtered, filtered, received)
        await anyio.sleep(0)
        await filtered.push(1)
        await filtered.push(2)
        await _wait_until(lambda: received == [2])
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_ensure_started_requires_task_group_and_waits_for_ready() -> None:
    starter = EnsureStarted()
    with pytest.raises(RuntimeError, match="No TaskGroup"):
        await starter.ensure_started(lambda: anyio.sleep_forever())

    starter = EnsureStarted()
    started: list[str] = []
    returned: list[str] = []
    ready = anyio.Event()

    async def runner() -> None:
        started.append("runner")
        await ready.wait()
        starter.mark_ready()
        await anyio.sleep_forever()

    async def ensure(label: str) -> None:
        await starter.ensure_started(runner)
        returned.append(label)

    async with anyio.create_task_group() as tg:
        starter.attach_task_group(tg)
        tg.start_soon(ensure, "first")
        tg.start_soon(ensure, "second")
        await _wait_until(lambda: started == ["runner"])
        assert returned == []
        ready.set()
        await _wait_until(lambda: sorted(returned) == ["first", "second"])
        await starter.ensure_started(runner)
        assert started == ["runner"]
        tg.cancel_scope.cancel()


class AsyncMockHandler:
    async def __call__(self, _reason: str) -> None:
        raise AssertionError("closed trigger should not call the handler")


async def _receive_scheduled(queue: ScheduledQueue[str], result: list[str]) -> None:
    result.append(await queue.get())


async def _receive_filtered(
    queue: SubscribableQueue[int],
    received: list[int],
) -> None:
    async for event in queue.subscribe(lambda value: value % 2 == 0):
        received.append(event)
        return


async def _wait_until(predicate) -> None:
    with anyio.fail_after(1):
        while not predicate():
            await anyio.sleep(0)
