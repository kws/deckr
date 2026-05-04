from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any, Protocol

import anyio
from pydantic import ValidationError

from deckr.contracts.lanes import LaneContract, LaneContractRegistry
from deckr.contracts.messages import (
    ACTIONS_LANE,
    CORE_LANE_NAMES,
    HARDWARE_MESSAGES_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    EntitySubject,
    MessageTarget,
    endpoint_target,
    message_is_expired,
    message_targets_endpoint,
    parse_endpoint_address,
)
from deckr.state import (
    DEFAULT_LEASE_STATE_STORE_NAME,
    DEFAULT_STATE_LEASE_TTL_SECONDS,
    DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS,
    EndpointPresence,
    StateConflict,
    StateEntry,
    StateStore,
    StateUnavailable,
    presence_endpoint_key,
)

ReplyPredicate = Callable[[DeckrMessage], bool | Awaitable[bool]]
logger = logging.getLogger(__name__)


class EndpointRegistrationConflict(RuntimeError):
    """Raised when an endpoint address is already registered on a lane."""


class EndpointSessionLost(RuntimeError):
    """Raised when a registered endpoint lease is no longer authoritative."""


class LaneSubstrate(Protocol):
    async def publish(self, message: DeckrMessage) -> None: ...

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None: ...

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage: ...

    def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
        *,
        endpoint_session_id: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]: ...

    def state(self, name: str) -> StateStore: ...


class Lane:
    def __init__(
        self,
        *,
        name: str,
        contract: LaneContract,
        substrate: LaneSubstrate,
    ) -> None:
        self.name = name
        self.contract = contract
        self._substrate = substrate
        self._registration_lock = anyio.Lock()
        self._registered_endpoints: set[EndpointAddress] = set()

    @asynccontextmanager
    async def register_endpoint(
        self,
        endpoint: str | EndpointAddress,
        *,
        metadata: Mapping[str, str] | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
    ) -> AsyncIterator[RegisteredEndpointLane]:
        parsed = parse_endpoint_address(endpoint)
        registered = RegisteredEndpointLane(
            lane=self,
            endpoint=parsed,
            session_id=str(uuid.uuid4()),
            state=self._substrate.state(
                getattr(
                    self._substrate,
                    "default_state_name",
                    DEFAULT_LEASE_STATE_STORE_NAME,
                )
            ),
            metadata=metadata or {},
        )
        async with self._registration_lock:
            if parsed in self._registered_endpoints:
                raise EndpointRegistrationConflict(
                    f"Endpoint {parsed} is already registered on lane {self.name!r}"
                )
            self._registered_endpoints.add(parsed)
        try:
            await registered._claim()
            renewal_task: asyncio.Task[None] | None = None
            if task_group is None:
                renewal_task = asyncio.create_task(
                    _log_endpoint_renewal_failures(registered),
                    name=f"deckr.endpoint-renewal:{self.name}:{parsed}",
                )
            else:
                task_group.start_soon(
                    registered._renew_until_closed,
                    name=f"deckr.endpoint-renewal:{self.name}:{parsed}",
                )
            try:
                yield registered
            finally:
                registered._closing = True
                registered._closing_event.set()
                if renewal_task is not None:
                    renewal_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await renewal_task
                registered._closed = True
                with anyio.move_on_after(2.0, shield=True):
                    await registered._withdraw()
        finally:
            async with self._registration_lock:
                self._registered_endpoints.discard(parsed)


class RegisteredEndpointLane:
    def __init__(
        self,
        *,
        lane: Lane,
        endpoint: EndpointAddress,
        session_id: str,
        state: StateStore,
        metadata: Mapping[str, str],
        ttl_seconds: int = DEFAULT_STATE_LEASE_TTL_SECONDS,
        renewal_interval_seconds: float = DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if renewal_interval_seconds <= 0:
            raise ValueError("renewal_interval_seconds must be greater than zero")
        self.lane = lane
        self.endpoint = endpoint
        self.session_id = session_id
        self._state = state
        self._metadata = dict(metadata)
        self._ttl_seconds = ttl_seconds
        self._renewal_interval_seconds = renewal_interval_seconds
        self._revision: int | None = None
        self._closing = False
        self._closing_event = anyio.Event()
        self._closed = False
        self._lost_reason: str | None = None
        self._lease_lock = anyio.Lock()

    async def send(
        self,
        *,
        recipient: str | EndpointAddress | MessageTarget,
        recipient_session_id: str | None = None,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        ttl_ms: int | None = None,
        causation_id: str | None = None,
    ) -> DeckrMessage:
        message = DeckrMessage(
            lane=self.lane.name,
            messageType=message_type,
            sender=self.endpoint,
            senderSessionId=self.session_id,
            recipient=_coerce_target(recipient),
            recipientSessionId=recipient_session_id,
            subject=subject,
            ttlMs=ttl_ms,
            causationId=causation_id,
            body=body,
        )
        await self._publish_current_message(message)
        return message

    async def publish(self, message: DeckrMessage) -> DeckrMessage:
        """Publish a prebuilt envelope through this endpoint-bound lane."""
        if message.sender != self.endpoint:
            raise ValueError(
                f"Message sender {message.sender} does not match bound endpoint "
                f"{self.endpoint}"
            )
        if message.sender_session_id != self.session_id:
            raise ValueError(
                f"Message senderSessionId {message.sender_session_id!r} does not "
                f"match bound endpoint session {self.session_id!r}"
            )
        await self._publish_current_message(message)
        return message

    async def request(
        self,
        *,
        recipient: str | EndpointAddress | MessageTarget,
        recipient_session_id: str | None = None,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
    ) -> DeckrMessage:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        message = DeckrMessage(
            lane=self.lane.name,
            messageType=message_type,
            sender=self.endpoint,
            senderSessionId=self.session_id,
            recipient=_coerce_target(recipient),
            recipientSessionId=recipient_session_id,
            subject=subject,
            ttlMs=ttl_ms,
            causationId=causation_id,
            body=body,
        )
        validate_message_for_contract(message, self.lane.contract)
        await self._assert_current_sender_session(message)
        return await self.lane._substrate.request(
            message,
            timeout=timeout,
            accept=accept,
        )

    async def reply_to(
        self,
        request: DeckrMessage,
        *,
        message_type: str,
        body: Mapping[str, Any],
        subject: EntitySubject | None = None,
        causation_id: str | None = None,
    ) -> DeckrMessage:
        reply = DeckrMessage(
            lane=request.lane,
            messageType=message_type,
            sender=self.endpoint,
            senderSessionId=self.session_id,
            recipient=endpoint_target(request.sender),
            recipientSessionId=request.sender_session_id,
            subject=subject or request.subject,
            inReplyTo=request.message_id,
            causationId=causation_id,
            body=body,
        )
        validate_message_for_contract(reply, self.lane.contract)
        await self._assert_current_sender_session(reply)
        await self.lane._substrate.publish_reply(reply, request=request)
        return reply

    def subscribe(
        self,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        self._ensure_active()
        return self.lane._substrate.subscribe(
            self.lane.name,
            self.endpoint,
            endpoint_session_id=self.session_id,
        )

    async def renew(self) -> None:
        async with self._lease_lock:
            self._ensure_active()
            if self._revision is None:
                raise EndpointSessionLost("Endpoint session is not registered")
            try:
                entry = await self._state.update(
                    self._presence_key,
                    self._presence(),
                    revision=self._revision,
                    ttl=self._ttl_seconds,
                )
            except (StateConflict, StateUnavailable) as exc:
                self._mark_lost("endpoint presence renewal failed")
                raise EndpointSessionLost(
                    f"Endpoint {self.endpoint} lost session {self.session_id}"
                ) from exc
            self._revision = entry.revision

    @property
    def _presence_key(self) -> str:
        return presence_endpoint_key(lane=self.lane.name, endpoint=self.endpoint)

    async def _claim(self) -> None:
        try:
            entry = await self._state.create(
                self._presence_key,
                self._presence(),
                ttl=self._ttl_seconds,
            )
        except StateConflict as exc:
            raise EndpointRegistrationConflict(
                f"Endpoint {self.endpoint} is already present on lane "
                f"{self.lane.name!r}"
            ) from exc
        self._revision = entry.revision

    async def _renew_until_closed(self) -> None:
        while not self._closing:
            with anyio.move_on_after(self._renewal_interval_seconds):
                await self._closing_event.wait()
            if self._closing:
                return
            await self.renew()

    async def _withdraw(self) -> None:
        if self._revision is None:
            return
        try:
            current = await self._state.get(self._presence_key)
        except StateUnavailable:
            return
        if current is None:
            self._mark_lost("endpoint presence disappeared before withdrawal")
            return
        if not _presence_entry_matches(
            current,
            lane=self.lane.name,
            endpoint=self.endpoint,
            session_id=self.session_id,
        ):
            self._mark_lost("endpoint presence changed before withdrawal")
            return
        try:
            await self._state.delete(self._presence_key, revision=current.revision)
        except (StateConflict, StateUnavailable):
            return
        self._revision = None

    def _presence(self) -> EndpointPresence:
        return EndpointPresence(
            endpoint=self.endpoint,
            lane=self.lane.name,
            sessionId=self.session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=self._ttl_seconds,
            metadata=self._metadata,
        )

    async def _publish_current_message(self, message: DeckrMessage) -> None:
        validate_message_for_contract(message, self.lane.contract)
        await self._assert_current_sender_session(message)
        await self.lane._substrate.publish(message)

    async def _assert_current_sender_session(self, message: DeckrMessage) -> None:
        self._ensure_active()
        if not await message_sender_session_is_current(message, state=self._state):
            self._mark_lost("endpoint presence no longer matches sender session")
            raise EndpointSessionLost(
                f"Endpoint {self.endpoint} lost session {self.session_id}"
            )

    def _ensure_active(self) -> None:
        if self._lost_reason is not None:
            raise EndpointSessionLost(self._lost_reason)
        if self._closed:
            raise RuntimeError(
                f"Endpoint {self.endpoint} on lane {self.lane.name!r} is closed"
            )

    def _mark_lost(self, reason: str) -> None:
        self._lost_reason = reason


async def _log_endpoint_renewal_failures(endpoint: RegisteredEndpointLane) -> None:
    try:
        await endpoint._renew_until_closed()
    except anyio.get_cancelled_exc_class():
        raise
    except Exception:
        if not endpoint._closing:
            logger.warning(
                "Endpoint lease renewal failed for %s on lane %r",
                endpoint.endpoint,
                endpoint.lane.name,
                exc_info=True,
            )


class LaneRegistry:
    def __init__(self, lanes: Mapping[str, Lane]) -> None:
        self._lanes = dict(lanes)

    @classmethod
    def from_names(
        cls,
        lane_names: Sequence[str],
        *,
        lane_contracts: LaneContractRegistry,
        substrate: LaneSubstrate,
    ) -> LaneRegistry:
        names = set(CORE_LANE_NAMES)
        names.update(lane_names)
        return cls(
            {
                name: Lane(
                    name=name,
                    contract=lane_contracts.contract_for(name),
                    substrate=substrate,
                )
                for name in sorted(names)
            }
        )

    def get(self, name: str) -> Lane | None:
        return self._lanes.get(name)

    def require(self, name: str) -> Lane:
        lane = self.get(name)
        if lane is None:
            raise LookupError(f"Required lane {name!r} is not available")
        return lane

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._lanes))


def validate_message_for_contract(
    message: DeckrMessage,
    contract: LaneContract,
) -> None:
    if message.lane != contract.lane:
        raise ValueError(
            f"Message lane {message.lane!r} does not match contract {contract.lane!r}"
        )
    if contract.message_types and message.message_type not in contract.message_types:
        raise ValueError(
            f"Message type {message.message_type!r} is not supported on "
            f"lane {message.lane!r}"
        )
    _validate_core_lane_body(message)
    if (
        contract.allowed_sender_families is not None
        and message.sender.family not in contract.allowed_sender_families
    ):
        raise ValueError(
            f"Sender family {message.sender.family!r} is not allowed on "
            f"lane {message.lane!r}"
        )
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        _validate_recipient_family(
            recipient.endpoint.family,
            contract=contract,
            lane=message.lane,
        )
        return
    expected_family = contract.broadcast_targets.get(recipient.scope)
    if expected_family != recipient.endpoint_family:
        raise ValueError(
            f"Broadcast target {recipient.scope!r} for family "
            f"{recipient.endpoint_family!r} is not allowed on lane {message.lane!r}"
        )
    _validate_recipient_family(
        recipient.endpoint_family,
        contract=contract,
        lane=message.lane,
    )


def message_is_deliverable(
    message: DeckrMessage,
    *,
    endpoint: EndpointAddress,
    endpoint_session_id: str,
    contract: LaneContract,
) -> bool:
    if message_is_expired(message):
        return False
    validate_message_for_contract(message, contract)
    if (
        message.recipient_session_id is not None
        and message.recipient_session_id != endpoint_session_id
    ):
        return False
    return message_targets_endpoint(message, endpoint)


async def message_sender_session_is_current(
    message: DeckrMessage,
    *,
    state: StateStore,
) -> bool:
    try:
        entry = await state.get(
            presence_endpoint_key(lane=message.lane, endpoint=message.sender)
        )
    except StateUnavailable:
        return False
    if entry is None:
        return False
    return _presence_entry_matches(
        entry,
        lane=message.lane,
        endpoint=message.sender,
        session_id=message.sender_session_id,
    )


async def reply_is_accepted(
    reply: DeckrMessage,
    *,
    request: DeckrMessage,
    accept: ReplyPredicate | None,
) -> bool:
    if reply.message_id == request.message_id:
        return False
    if reply.in_reply_to != request.message_id:
        return False
    if (
        reply.recipient_session_id is not None
        and reply.recipient_session_id != request.sender_session_id
    ):
        return False
    if accept is None:
        return True
    accepted = accept(reply)
    if isawaitable(accepted):
        accepted = await accepted
    return bool(accepted)


def _validate_recipient_family(
    family: str,
    *,
    contract: LaneContract,
    lane: str,
) -> None:
    if contract.allowed_recipient_families is None:
        return
    if family not in contract.allowed_recipient_families:
        raise ValueError(f"Recipient family {family!r} is not allowed on lane {lane!r}")


def _validate_core_lane_body(message: DeckrMessage) -> None:
    if message.lane == ACTIONS_LANE:
        from deckr.actions.messages import action_body

        action_body(message)
        return
    if message.lane == HARDWARE_MESSAGES_LANE:
        from deckr.hardware.messages import hardware_body_from_message

        hardware_body_from_message(message)


def _coerce_target(target: str | EndpointAddress | MessageTarget) -> MessageTarget:
    if isinstance(target, EndpointTarget | BroadcastTarget):
        return target
    return endpoint_target(target)


def _presence_entry_matches(
    entry: StateEntry,
    *,
    lane: str,
    endpoint: EndpointAddress,
    session_id: str,
) -> bool:
    try:
        presence = EndpointPresence.model_validate(entry.value)
    except ValidationError:
        return False
    return (
        presence.lane == lane
        and presence.endpoint == endpoint
        and presence.session_id == session_id
    )
