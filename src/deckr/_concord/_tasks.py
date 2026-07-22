from __future__ import annotations

import anyio

CONCORD_TASK_JOIN_SECONDS = 5.0


class ConcordTaskGroup:
    """A private task group whose cancel scope is owned by its runner task."""

    def __init__(self, parent: anyio.abc.TaskGroup | None) -> None:
        self._parent = parent
        self._group: anyio.abc.TaskGroup | None = None
        self._done = anyio.Event()

    @property
    def group(self) -> anyio.abc.TaskGroup:
        if self._group is None:
            raise RuntimeError("Concord context task group has not started")
        return self._group

    async def start(self) -> anyio.abc.TaskGroup:
        if self._group is not None:
            raise RuntimeError("Concord context task group is already started")
        if self._parent is None:
            raise RuntimeError("Concord must be started before entering its contexts")
        self._group = await self._parent.start(self._run)
        return self._group

    async def aclose(self) -> None:
        group = self._group
        if group is None:
            return
        group.cancel_scope.cancel()
        with anyio.move_on_after(CONCORD_TASK_JOIN_SECONDS, shield=True):
            await self._done.wait()
        self._group = None

    async def _run(
        self,
        *,
        task_status: anyio.abc.TaskStatus[anyio.abc.TaskGroup] = (
            anyio.TASK_STATUS_IGNORED
        ),
    ) -> None:
        try:
            async with anyio.create_task_group() as group:
                task_status.started(group)
                await anyio.sleep_forever()
        finally:
            self._done.set()


__all__ = ["CONCORD_TASK_JOIN_SECONDS", "ConcordTaskGroup"]
