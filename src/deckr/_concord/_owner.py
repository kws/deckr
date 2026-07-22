from __future__ import annotations

import random
from collections import defaultdict

import anyio

from deckr._concord._models import (
    ConcordConflict,
    ConcordConflictCode,
    ConcordUnavailable,
    ContractState,
)
from deckr._concord._store import ConcordKvStore
from deckr._concord._view import ConcordView
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address

_OWNER_RETRY_INITIAL_SECONDS = 0.1
_OWNER_RETRY_MAX_SECONDS = 5.0


class ConcordOwnerBootstrap:
    """Process-local ownership registry and stale-contract cleanup gate.

    A new :class:`Concord` instance starts with an empty registry.  Its first
    proposal for an endpoint therefore cancels every open contract created by
    that endpoint before the new prepared pointer may be committed.  Pointers
    already owned by live contexts in this process are excluded only when they
    belong to the same endpoint session.
    """

    def __init__(self, store: ConcordKvStore, view: ConcordView) -> None:
        self._store = store
        self._view = view
        self._locks: defaultdict[str, anyio.Lock] = defaultdict(anyio.Lock)
        self._active_lock = anyio.Lock()
        self._active: defaultdict[str, dict[ContractPointer, str]] = defaultdict(dict)

    async def claim(
        self,
        endpoint: str | EndpointAddress,
        session_id: str,
        pointer: ContractPointer,
    ) -> None:
        address = parse_endpoint_address(endpoint)
        key = str(address)
        async with self._locks[key]:
            await self._cleanup_stale(address, session_id)
            async with self._active_lock:
                self._active[key][pointer] = session_id

    async def bootstrap(
        self,
        endpoint: str | EndpointAddress,
        session_id: str,
    ) -> None:
        """Confirm stale local proposals are cancelled before owner readiness."""

        address = parse_endpoint_address(endpoint)
        async with self._locks[str(address)]:
            await self._cleanup_stale(address, session_id)

    async def release(
        self,
        endpoint: str | EndpointAddress,
        pointer: ContractPointer,
    ) -> None:
        key = str(parse_endpoint_address(endpoint))
        async with self._active_lock:
            self._active[key].pop(pointer, None)

    async def _cancel_until_resolved(
        self,
        pointer: ContractPointer,
        endpoint: EndpointAddress,
    ) -> None:
        delay = _OWNER_RETRY_INITIAL_SECONDS
        while True:
            try:
                await self._store.cancel_pointer(
                    pointer,
                    endpoint,
                    reason="concord_owner_bootstrap_stale_contract",
                )
                return
            except ConcordConflict as exc:
                if exc.code != ConcordConflictCode.REVISION_CHANGED:
                    raise
                delay = _OWNER_RETRY_INITIAL_SECONDS
                continue
            except ConcordUnavailable:
                pass
            await anyio.sleep(random.uniform(delay * 0.5, delay))
            delay = min(delay * 2, _OWNER_RETRY_MAX_SECONDS)

    async def _cleanup_stale(
        self,
        endpoint: EndpointAddress,
        session_id: str,
    ) -> None:
        await self._view.wait_current()
        snapshot = await self._view.snapshot(
            participant=endpoint,
            state=ContractState.OPEN,
        )
        key = str(endpoint)
        async with self._active_lock:
            current = {
                pointer
                for pointer, owned_session in self._active[key].items()
                if owned_session == session_id
            }
        stale = tuple(
            state.contract.pointer
            for state in snapshot.contracts
            if state.record.created_by == endpoint
            and state.contract.pointer not in current
        )
        for stale_pointer in stale:
            await self._cancel_until_resolved(stale_pointer, endpoint)


__all__ = ["ConcordOwnerBootstrap"]
