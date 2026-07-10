from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import anyio

from deckr.contracts.models import DeckrModel
from deckr.substrates.nats_kv import KvChange, KvEntry


class ExactConcordKvPort(Protocol):
    @property
    def bucket(self) -> str: ...

    async def get_exact(self, key: str) -> KvEntry | None: ...

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry: ...

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry: ...

    async def delete(self, key: str, *, revision: int) -> int | None: ...

    async def ttl_seconds(self) -> int: ...


class ConcordMaterializedSourcePort(Protocol):
    @property
    def bucket(self) -> str: ...

    @property
    def generation(self) -> int: ...

    def start(self, task_group: anyio.abc.TaskGroup) -> None: ...

    def is_ready(self) -> bool: ...

    def is_current(self) -> bool: ...

    async def wait_ready(self) -> None: ...

    async def wait_current(self) -> None: ...

    def get_cached(self, key: str) -> KvEntry | None: ...

    def items_cached(self, prefix: str = "") -> tuple[KvEntry, ...]: ...

    def revision_cached(self, key: str) -> int | None: ...

    def subscribe(
        self,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[KvChange]]: ...


class ConcordMaintenanceScanPort(Protocol):
    @property
    def bucket(self) -> str: ...

    async def items_exact(self, prefix: str = "") -> tuple[KvEntry, ...]: ...

    async def get_exact(self, key: str) -> KvEntry | None: ...

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> KvEntry: ...

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> KvEntry: ...

    async def delete(self, key: str, *, revision: int) -> int | None: ...
