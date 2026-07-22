from __future__ import annotations

import anyio

from deckr.core.util.anyio import OwnedTaskGroup

CONCORD_TASK_JOIN_SECONDS = 5.0


class ConcordTaskGroup(OwnedTaskGroup):
    """A private task group whose cancel scope is owned by its runner task."""

    def __init__(self, parent: anyio.abc.TaskGroup | None) -> None:
        super().__init__(
            parent,
            not_started_message=(
                "Concord must be started before entering its contexts"
            ),
            join_seconds=CONCORD_TASK_JOIN_SECONDS,
        )


__all__ = ["CONCORD_TASK_JOIN_SECONDS", "ConcordTaskGroup"]
