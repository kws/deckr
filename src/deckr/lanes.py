from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from inspect import isawaitable
from types import MappingProxyType, TracebackType
from typing import Any, Protocol

import anyio

from deckr.contracts.lanes import MessageContract, MessageContractRegistry
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
    TraceContext,
    endpoint_target,
    message_is_expired,
    message_targets_endpoint,
    parse_endpoint_address,
)

ReplyPredicate = Callable[[DeckrMessage], bool | Awaitable[bool]]


class MessageBus(Protocol):
    def contract_for(self, lane: str) -> MessageContract: ...

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


@dataclass(frozen=True, slots=True)
class EndpointSessionInfo:
    address: EndpointAddress
    session_id: str
    metadata: Mapping[str, str]


class EndpointSession:
    def __init__(
        self,
        *,
        address: EndpointAddress,
        session_id: str,
        metadata: Mapping[str, str],
        message_bus: MessageBus,
    ) -> None:
        self._info = EndpointSessionInfo(
            address=address,
            session_id=session_id,
            metadata=MappingProxyType(dict(metadata)),
        )
        self._message_bus = message_bus
        self._subscriptions: set[_EndpointSubscription] = set()
        self._closed = False

    @property
    def info(self) -> EndpointSessionInfo:
        return self._info

    @property
    def address(self) -> EndpointAddress:
        return self._info.address

    @property
    def session_id(self) -> str:
        return self._info.session_id

    @property
    def metadata(self) -> Mapping[str, str]:
        return dict(self._info.metadata)

    async def send(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        self._ensure_active()
        message = self._message(
            lane=lane,
            recipient=recipient,
            recipient_session_id=recipient_session_id,
            subject=subject,
            message_type=message_type,
            body=body,
            ttl_ms=ttl_ms,
            causation_id=causation_id,
            trace=trace,
        )
        validate_message_for_contract(message, self._contract_for(lane))
        await self._message_bus.publish(message)
        return message

    async def request(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        self._ensure_active()
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        message = self._message(
            lane=lane,
            recipient=recipient,
            recipient_session_id=recipient_session_id,
            subject=subject,
            message_type=message_type,
            body=body,
            ttl_ms=ttl_ms,
            causation_id=causation_id,
            trace=trace,
        )
        validate_message_for_contract(message, self._contract_for(lane))
        return await self._message_bus.request(
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
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        self._ensure_active()
        reply = DeckrMessage(
            lane=request.lane,
            messageType=message_type,
            sender=self.address,
            senderSessionId=self.session_id,
            recipient=endpoint_target(request.sender),
            recipientSessionId=request.sender_session_id,
            subject=subject or request.subject,
            inReplyTo=request.message_id,
            causationId=causation_id,
            trace=trace,
            body=body,
        )
        validate_message_for_contract(reply, self._contract_for(request.lane))
        await self._message_bus.publish_reply(reply, request=request)
        return reply

    def subscribe(
        self,
        lane: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        self._ensure_active()
        self._contract_for(lane)
        return _EndpointSubscription(
            self,
            self._message_bus.subscribe(
                lane,
                self.address,
                endpoint_session_id=self.session_id,
            ),
        )

    def close(self) -> None:
        self._closed = True

    async def aclose(self) -> None:
        self._closed = True
        for subscription in tuple(self._subscriptions):
            await subscription.aclose()

    def _message(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress | MessageTarget,
        recipient_session_id: str | None,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        ttl_ms: int | None,
        causation_id: str | None,
        trace: TraceContext | None,
    ) -> DeckrMessage:
        return DeckrMessage(
            lane=lane,
            messageType=message_type,
            sender=self.address,
            senderSessionId=self.session_id,
            recipient=_coerce_target(recipient),
            recipientSessionId=recipient_session_id,
            subject=subject,
            ttlMs=ttl_ms,
            causationId=causation_id,
            trace=trace,
            body=body,
        )

    def _contract_for(self, lane: str) -> MessageContract:
        return self._message_bus.contract_for(lane)

    def _ensure_active(self) -> None:
        if self._closed:
            raise RuntimeError(f"Endpoint session {self.address} is closed")

    def _track_subscription(self, subscription: _EndpointSubscription) -> None:
        self._ensure_active()
        self._subscriptions.add(subscription)

    def _untrack_subscription(self, subscription: _EndpointSubscription) -> None:
        self._subscriptions.discard(subscription)


class _EndpointSubscription:
    def __init__(
        self,
        session: EndpointSession,
        context_manager: AbstractAsyncContextManager[
            anyio.abc.ObjectReceiveStream[DeckrMessage]
        ],
    ) -> None:
        self._session = session
        self._context_manager = context_manager
        self._entered = False
        self._closed = False

    async def __aenter__(self) -> anyio.abc.ObjectReceiveStream[DeckrMessage]:
        self._session._ensure_active()  # noqa: SLF001
        stream = await self._context_manager.__aenter__()
        try:
            self._session._track_subscription(self)  # noqa: SLF001
        except BaseException:
            await self._context_manager.__aexit__(None, None, None)
            raise
        self._entered = True
        return stream

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if not self._entered or self._closed:
            return None
        self._closed = True
        try:
            return await self._context_manager.__aexit__(exc_type, exc, traceback)
        finally:
            self._session._untrack_subscription(self)  # noqa: SLF001

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)


class Lane:
    """A stable DeckrMessage namespace and message contract selector."""

    def __init__(
        self,
        *,
        name: str,
        contract: MessageContract,
    ) -> None:
        self.name = name
        self.contract = contract


class LaneRegistry:
    def __init__(self, lanes: Mapping[str, Lane]) -> None:
        self._lanes = dict(lanes)

    @classmethod
    def from_names(
        cls,
        lane_names: Sequence[str],
        *,
        message_contracts: MessageContractRegistry,
    ) -> LaneRegistry:
        names = set(CORE_LANE_NAMES)
        names.update(lane_names)
        return cls(
            {
                name: Lane(
                    name=name,
                    contract=message_contracts.contract_for(name),
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


def new_endpoint_session_id() -> str:
    return str(uuid.uuid4())


def endpoint_session(
    *,
    address: str | EndpointAddress,
    session_id: str | None,
    metadata: Mapping[str, str] | None,
    message_bus: MessageBus,
) -> EndpointSession:
    return EndpointSession(
        address=parse_endpoint_address(address),
        session_id=session_id or new_endpoint_session_id(),
        metadata=metadata or {},
        message_bus=message_bus,
    )


def validate_message_for_contract(
    message: DeckrMessage,
    contract: MessageContract,
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
    if message.recipient_session_id is not None:
        raise ValueError("recipientSessionId is only valid for endpoint recipients")
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
    contract: MessageContract,
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
    contract: MessageContract,
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
