from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anyio

from deckr.contracts.models import DeckrModel, freeze_json

DEFAULT_STATE_STORE_NAME = "deckr_state_v1"
DEFAULT_STATE_TTL_SECONDS = 30
DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS = 5.0
DEFAULT_STATE_RECONCILE_SECONDS = 300.0
DEFAULT_STATE_NOTIFICATION_BATCH_SECONDS = 1.0


class StateConflict(RuntimeError):
    """Raised when a first-writer or revision-checked state write fails."""


class StateUnavailable(RuntimeError):
    """Raised when the current-state substrate cannot answer safely."""


@dataclass(frozen=True, slots=True)
class StateStorePolicy:
    broker_ttl_seconds: float | None
    allow_write_ttl: bool = False
    description: str = "persistent current state"

    def __post_init__(self) -> None:
        if self.broker_ttl_seconds is not None and self.broker_ttl_seconds <= 0:
            raise ValueError("broker_ttl_seconds must be greater than zero")
        if self.allow_write_ttl and self.broker_ttl_seconds is None:
            raise ValueError("allow_write_ttl requires broker_ttl_seconds")


TTL_STATE_STORE_POLICY = StateStorePolicy(
    broker_ttl_seconds=float(DEFAULT_STATE_TTL_SECONDS),
    allow_write_ttl=True,
    description="TTL current state",
)
PERSISTENT_STATE_STORE_POLICY = StateStorePolicy(
    broker_ttl_seconds=None,
    allow_write_ttl=False,
    description="persistent current state",
)


@dataclass(frozen=True, slots=True)
class StateEntry:
    key: str
    value: Mapping[str, Any]
    revision: int


@dataclass(frozen=True, slots=True)
class StateChange:
    operation: Literal["put", "delete", "expire"]
    key: str
    entry: StateEntry | None = None


class StateStore(Protocol):
    async def get(self, key: str) -> StateEntry | None: ...

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]:
        """Observe entries by prefix; omission is not an authoritative absence."""
        ...

    async def put(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def create(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def update(
        self,
        key: str,
        value: Mapping[str, Any] | DeckrModel,
        *,
        revision: int,
        ttl: float | None = None,
    ) -> StateEntry: ...

    async def delete(self, key: str, *, revision: int | None = None) -> None: ...

    def watch(
        self,
        prefix: str = "",
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[StateChange]]: ...


@dataclass(frozen=True, slots=True)
class PrefixObservation:
    entries: tuple[StateEntry, ...]
    confirmed_missing: frozenset[str]


async def observe_prefix_current(
    state: StateStore,
    prefix: str = "",
    *,
    known_keys: Iterable[str] = (),
) -> PrefixObservation:
    """Observe a prefix and exact-confirm omitted known keys.

    `items(prefix)` is a discovery observation. This helper preserves observed
    entries, then uses exact-key `get()` calls to decide whether omitted known
    keys are truly missing or merely absent from the prefix observation.
    """

    observed = {entry.key: entry for entry in await state.items(prefix)}
    confirmed_missing: set[str] = set()
    for key in sorted(set(known_keys)):
        if not key.startswith(prefix) or key in observed:
            continue
        current = await state.get(key)
        if current is None:
            confirmed_missing.add(key)
        else:
            observed[key] = current
    return PrefixObservation(
        entries=tuple(entry for _key, entry in sorted(observed.items())),
        confirmed_missing=frozenset(confirmed_missing),
    )


def state_value(value: Mapping[str, Any] | DeckrModel) -> Mapping[str, Any]:
    if isinstance(value, DeckrModel):
        return freeze_json(value.model_dump(by_alias=True, exclude_none=True, mode="json"))
    return freeze_json(dict(value))
