from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from inspect import isawaitable
from typing import Any, Protocol

import anyio

from deckr.contracts.lanes import LaneContract, LaneContractRegistry
from deckr.contracts.messages import (
    ACTIONS_LANE,
    CORE_LANE_NAMES,
    HARDWARE_MESSAGES_LANE,
    SERVICES_LANE,
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

ReplyPredicate = Callable[[DeckrMessage], bool | Awaitable[bool]]


class EndpointRegistrationConflict(RuntimeError):
    """Raised when an endpoint address is already registered on a lane."""


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
            metadata=metadata or {},
        )
        async with self._registration_lock:
            if parsed in self._registered_endpoints:
                raise EndpointRegistrationConflict(
                    f"Endpoint {parsed} is already registered on lane {self.name!r}"
                )
            self._registered_endpoints.add(parsed)
        try:
            del task_group
            yield registered
        finally:
            registered._closed = True
            async with self._registration_lock:
                self._registered_endpoints.discard(parsed)


class RegisteredEndpointLane:
    def __init__(
        self,
        *,
        lane: Lane,
        endpoint: EndpointAddress,
        session_id: str,
        metadata: Mapping[str, str],
    ) -> None:
        self.lane = lane
        self.endpoint = endpoint
        self.session_id = session_id
        self._metadata = dict(metadata)
        self._closed = False

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

    async def _publish_current_message(self, message: DeckrMessage) -> None:
        validate_message_for_contract(message, self.lane.contract)
        self._ensure_active()
        await self.lane._substrate.publish(message)

    def _ensure_active(self) -> None:
        if self._closed:
            raise RuntimeError(
                f"Endpoint {self.endpoint} on lane {self.lane.name!r} is closed"
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
        return
    if message.lane == SERVICES_LANE:
        from deckr.services.messages import service_body

        service_body(message)


def _coerce_target(target: str | EndpointAddress | MessageTarget) -> MessageTarget:
    if isinstance(target, EndpointTarget | BroadcastTarget):
        return target
    return endpoint_target(target)
