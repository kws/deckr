from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

import anyio

from deckr._concord._models import (
    ConcordConflict,
    ConcordConflictCode,
    ConcordUnavailable,
    ContractHandle,
    ParticipantHandle,
)
from deckr._concord._store import (
    MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS,
    ConcordKvStore,
)
from deckr._concord._view import ConcordView
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address

CONCORD_LIFECYCLE_CLEANUP_SECONDS = 5.0

logger = logging.getLogger(__name__)


class ConcordParticipantLeasePhase(StrEnum):
    STARTING = "starting"
    ATTACHING = "attaching"
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    TERMINAL = "terminal"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ConcordParticipantLeaseState:
    version: int
    phase: ConcordParticipantLeasePhase
    local_authority: bool
    reason: str | None = None


ConcordParticipantLeaseCallback = Callable[
    [ConcordParticipantLeaseState],
    None | Awaitable[None],
]


def concord_token_refresh_delay(ttl_seconds: int | float) -> float:
    ttl = float(ttl_seconds)
    return random.uniform(ttl * 0.5, ttl * 0.75)


class ConcordParticipantLease:
    """Own one exact participant token without ever reattaching it."""

    def __init__(
        self,
        store: ConcordKvStore,
        view: ConcordView,
        *,
        contract: ContractHandle,
        participant: str | EndpointAddress,
        session_id: str,
        on_change: ConcordParticipantLeaseCallback | None = None,
    ) -> None:
        self._store = store
        self._view = view
        self.contract = contract
        self.participant = parse_endpoint_address(participant)
        self.session_id = session_id
        self._token_id = str(uuid.uuid4())
        self._owned_token: ParticipantHandle | None = None
        self._authority_token: ParticipantHandle | None = None
        self._refresh_delay: float | None = None
        self._last_refresh_at: float | None = None
        self._on_change = on_change
        self._operation_lock = anyio.Lock()
        self._version = 0
        self._state = ConcordParticipantLeaseState(
            version=0,
            phase=ConcordParticipantLeasePhase.STARTING,
            local_authority=False,
        )
        self._closed = False

    @property
    def state(self) -> ConcordParticipantLeaseState:
        return self._state

    @property
    def token(self) -> ParticipantHandle | None:
        """Return the token only while it is locally held authority."""

        return self._authority_token

    @property
    def owned_token(self) -> ParticipantHandle | None:
        """The exact handle retained for partial-attach cleanup."""

        return self._owned_token

    async def attach(self) -> ParticipantHandle:
        async with self._operation_lock:
            if self._closed:
                raise ConcordConflict(
                    ConcordConflictCode.LEASE_CLOSED,
                    "Concord participant lease is closed",
                    key=self.contract.key,
                )
            if self._authority_token is not None:
                return self._authority_token
            if self._owned_token is not None:
                raise ConcordConflict(
                    ConcordConflictCode.REVISION_CHANGED,
                    "Concord participant attachment is already incomplete",
                    key=self.contract.key,
                    expected_pointer=self.contract.pointer,
                )

            await self._publish(
                ConcordParticipantLeasePhase.ATTACHING,
                local_authority=False,
            )
            async with self._view.subscribe(
                contract_id=self.contract.contract_id,
            ) as subscription:
                token = await self._store.prepare_attach(
                    self.contract,
                    self.participant,
                    self.session_id,
                    token_id=self._token_id,
                )
                self._owned_token = token
                last_conflict: ConcordConflict | None = None
                for attempt in range(MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS):
                    observed = self._view.get_contract(self.contract.pointer)
                    observed_revision = (
                        observed.revision
                        if observed is not None
                        else self.contract.revision
                    )
                    try:
                        revision = await self._store.mark_participant_attached(
                            self.contract,
                            self.participant,
                        )
                    except ConcordConflict as exc:
                        if exc.code != ConcordConflictCode.REVISION_CHANGED:
                            await self._terminal_after_attach_failure(exc)
                            raise
                        last_conflict = exc
                        if attempt + 1 == MAX_ATTACHED_PARTICIPANT_CAS_ATTEMPTS:
                            break
                        # The subscription was armed before the CAS, so the
                        # revision change that caused the conflict cannot be
                        # missed. Continued contention waits for view progress.
                        while True:
                            await subscription.receive()
                            advanced = self._view.get_contract(self.contract.pointer)
                            if (
                                advanced is not None
                                and advanced.revision > observed_revision
                            ):
                                break
                        continue
                    await self._view.wait_contract_revision(
                        self.contract.key,
                        revision,
                    )
                    await self._view.wait_token_revision(token.key, token.revision)
                    self._authority_token = token
                    self._refresh_delay = concord_token_refresh_delay(
                        token.ttl_seconds
                    )
                    self._last_refresh_at = monotonic()
                    await self._publish(
                        ConcordParticipantLeasePhase.ACTIVE,
                        local_authority=True,
                    )
                    return token

                assert last_conflict is not None
                await self._terminal_after_attach_failure(last_conflict)
                raise ConcordConflict(
                    ConcordConflictCode.REVISION_CHANGED,
                    "Concord participant attachment exhausted the eight-attempt "
                    "contract CAS budget; its token was withdrawn",
                    key=self.contract.key,
                    expected_pointer=self.contract.pointer,
                ) from last_conflict

    async def refresh(self) -> ParticipantHandle:
        """Refresh only the exact already-held token."""

        async with self._operation_lock:
            if self._closed or self._authority_token is None:
                raise ConcordConflict(
                    ConcordConflictCode.LEASE_CLOSED,
                    "Concord participant lease has no live local authority",
                    key=self.contract.key,
                )
            try:
                refreshed = await self._store.refresh(self._authority_token)
            except ConcordUnavailable as exc:
                self._authority_token = None
                await self._publish(
                    ConcordParticipantLeasePhase.UNAVAILABLE,
                    local_authority=False,
                    reason=str(exc),
                )
                raise
            except ConcordConflict as exc:
                self._authority_token = None
                await self._publish(
                    ConcordParticipantLeasePhase.TERMINAL,
                    local_authority=False,
                    reason=str(exc),
                )
                raise
            self._owned_token = refreshed
            self._authority_token = refreshed
            self._refresh_delay = concord_token_refresh_delay(refreshed.ttl_seconds)
            self._last_refresh_at = monotonic()
            return refreshed

    async def fail_closed_unavailable(self, reason: str) -> None:
        """Stop using retained authority after its transport view goes stale."""

        async with self._operation_lock:
            if self._closed:
                return
            self._authority_token = None
            await self._publish(
                ConcordParticipantLeasePhase.UNAVAILABLE,
                local_authority=False,
                reason=reason,
            )

    async def heartbeat_loop(self) -> None:
        """Heartbeat an already-held token until authority is lost or closed."""

        while not self._closed:
            token = self._authority_token
            if token is None:
                return
            delay = self._next_heartbeat_delay()
            await anyio.sleep(delay)
            if self._closed or self._authority_token is None:
                return
            try:
                await self.refresh()
            except (ConcordConflict, ConcordUnavailable):
                return

    def _next_heartbeat_delay(self) -> float:
        delay = self._refresh_delay
        if delay is None:
            return 0.0
        if self._last_refresh_at is None:
            return delay
        return max(0.0, delay - (monotonic() - self._last_refresh_at))

    async def aclose(self, *, withdraw: bool = True) -> None:
        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            await self._publish(
                ConcordParticipantLeasePhase.CLOSING,
                local_authority=False,
            )
            self._authority_token = None
            token = self._owned_token
            if withdraw and token is not None:
                with anyio.move_on_after(
                    CONCORD_LIFECYCLE_CLEANUP_SECONDS,
                    shield=True,
                ):
                    try:
                        await self._store.withdraw(token)
                    except (ConcordConflict, ConcordUnavailable):
                        logger.debug(
                            "Concord participant token cleanup failed "
                            "contract=%s generation=%s participant=%s session=%s",
                            token.contract_id,
                            token.generation,
                            token.participant,
                            token.session_id,
                            exc_info=True,
                        )
            self._owned_token = None
            self._last_refresh_at = None
            await self._publish(
                ConcordParticipantLeasePhase.CLOSED,
                local_authority=False,
            )

    async def _terminal_after_attach_failure(self, error: BaseException) -> None:
        token = self._owned_token
        self._authority_token = None
        if token is not None:
            try:
                await self._store.withdraw(token)
            except ConcordUnavailable as exc:
                try:
                    await self._store.cancel(
                        self.contract,
                        self.participant,
                        reason="concord_partial_attach_cleanup_unavailable",
                    )
                except (ConcordConflict, ConcordUnavailable):
                    logger.debug(
                        "Could not cancel Concord contract after unavailable "
                        "partial-attach cleanup",
                        exc_info=True,
                    )
                await self._publish(
                    ConcordParticipantLeasePhase.UNAVAILABLE,
                    local_authority=False,
                    reason=str(exc),
                )
                raise
            except ConcordConflict:
                try:
                    await self._store.cancel(
                        self.contract,
                        self.participant,
                        reason="concord_partial_attach_cleanup_failed",
                    )
                except ConcordUnavailable as exc:
                    logger.debug(
                        "Could not cancel unavailable Concord contract after "
                        "partial attach",
                        exc_info=True,
                    )
                    await self._publish(
                        ConcordParticipantLeasePhase.UNAVAILABLE,
                        local_authority=False,
                        reason=str(exc),
                    )
                    raise
                except ConcordConflict:
                    logger.debug(
                        "Could not cancel Concord contract after partial attach",
                        exc_info=True,
                    )
                else:
                    self._owned_token = None
            else:
                self._owned_token = None
        await self._publish(
            ConcordParticipantLeasePhase.TERMINAL,
            local_authority=False,
            reason=str(error),
        )

    async def _publish(
        self,
        phase: ConcordParticipantLeasePhase,
        *,
        local_authority: bool,
        reason: str | None = None,
    ) -> None:
        self._version += 1
        state = ConcordParticipantLeaseState(
            version=self._version,
            phase=phase,
            local_authority=local_authority,
            reason=reason,
        )
        self._state = state
        if self._on_change is not None:
            result = self._on_change(state)
            if hasattr(result, "__await__"):
                await result


__all__ = [
    "CONCORD_LIFECYCLE_CLEANUP_SECONDS",
    "ConcordParticipantLease",
    "ConcordParticipantLeasePhase",
    "ConcordParticipantLeaseState",
    "concord_token_refresh_delay",
]
