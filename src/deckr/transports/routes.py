from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import anyio

from deckr.contracts.lanes import (
    DEFAULT_LANE_CONTRACT_REGISTRY,
    DeliverySemantics,
    ExpiryHandling,
    LaneContractRegistry,
)
from deckr.contracts.messages import (
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    RouteMetadata,
    message_is_expired,
    parse_endpoint_address,
)

RouteClientKind = Literal["local", "remote"]
RouteScope = Literal["lane"]
RouteClaimSource = Literal["local", "message_sender", "transport_route"]
RouteTrustStatus = Literal["local", "trusted", "untrusted"]
RouteEventType = Literal[
    "clientConnected",
    "clientDisconnected",
    "endpointReachable",
    "endpointUnreachable",
    "endpointClaimRejected",
    "messageRejected",
    "messageDropped",
]
MessagePolicyBoundary = Literal["remote_ingress", "transport_egress"]

MAX_ROUTE_HISTORY = 16
RouteKey = tuple[str, EndpointAddress]

_TRUST_PRECEDENCE: dict[RouteTrustStatus, int] = {
    "untrusted": 10,
    "trusted": 50,
    "local": 100,
}


def route_client_id(prefix: str, *parts: str) -> str:
    tokens = [prefix, *parts, uuid.uuid4().hex]
    return ":".join(token for token in tokens if token)


@dataclass(frozen=True, slots=True)
class RouteClient:
    client_id: str
    client_kind: RouteClientKind
    transport_kind: str | None = None
    transport_id: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class EndpointRoute:
    lane: str
    endpoint: EndpointAddress
    client_id: str
    client_kind: RouteClientKind
    scope: RouteScope = "lane"
    direct: bool = True
    transport_kind: str | None = None
    transport_id: str | None = None
    claim_source: RouteClaimSource = "local"
    trust_status: RouteTrustStatus = "local"
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteEvent:
    event_type: RouteEventType
    client_id: str | None = None
    lane: str | None = None
    endpoint: EndpointAddress | None = None
    message_id: str | None = None
    message_type: str | None = None
    sender: EndpointAddress | None = None
    route: EndpointRoute | None = None
    rejected_route: EndpointRoute | None = None
    reason: str | None = None


class RouteTable:
    """In-process Deckr endpoint reachability and forwarding state."""

    def __init__(
        self,
        *,
        lane_contracts: LaneContractRegistry | None = None,
    ) -> None:
        self._lane_contracts = lane_contracts or DEFAULT_LANE_CONTRACT_REGISTRY
        self._lock = anyio.Lock()
        self._clients: dict[str, RouteClient] = {}
        self._routes: dict[RouteKey, EndpointRoute] = {}
        self._routes_by_client: dict[str, set[RouteKey]] = {}
        self._subscribers: set[anyio.abc.ObjectSendStream[RouteEvent]] = set()

    async def client_connected(
        self,
        *,
        client_id: str,
        client_kind: RouteClientKind,
        transport_kind: str | None = None,
        transport_id: str | None = None,
        description: str | None = None,
    ) -> RouteClient:
        event: RouteEvent | None = None
        async with self._lock:
            client = RouteClient(
                client_id=client_id,
                client_kind=client_kind,
                transport_kind=transport_kind,
                transport_id=transport_id,
                description=description,
            )
            if self._clients.get(client_id) != client:
                self._clients[client_id] = client
                self._routes_by_client.setdefault(client_id, set())
                event = RouteEvent(
                    event_type="clientConnected",
                    client_id=client_id,
                )
        if event is not None:
            await self._publish(event)
        return client

    async def client_disconnected(self, client_id: str) -> tuple[EndpointRoute, ...]:
        events: list[RouteEvent] = []
        removed: list[EndpointRoute] = []
        async with self._lock:
            if client_id not in self._clients:
                return ()
            route_keys = self._routes_by_client.pop(client_id, set())
            self._clients.pop(client_id, None)
            for route_key in route_keys:
                route = self._routes.get(route_key)
                if route is None or route.client_id != client_id:
                    continue
                self._routes.pop(route_key, None)
                removed.append(route)
                events.append(
                    RouteEvent(
                        event_type="endpointUnreachable",
                        client_id=client_id,
                        lane=route.lane,
                        endpoint=route.endpoint,
                        route=route,
                        reason="clientDisconnected",
                    )
                )
            events.append(
                RouteEvent(event_type="clientDisconnected", client_id=client_id)
            )
        for event in events:
            await self._publish(event)
        return tuple(removed)

    async def claim_endpoint(
        self,
        *,
        endpoint: str | EndpointAddress,
        lane: str,
        client_id: str,
        client_kind: RouteClientKind,
        transport_kind: str | None = None,
        transport_id: str | None = None,
        direct: bool = True,
        claim_source: RouteClaimSource | None = None,
        trust_status: RouteTrustStatus | None = None,
        capabilities: tuple[str, ...] | list[str] = (),
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        normalized_capabilities = tuple(dict.fromkeys(capabilities))
        if client_kind == "local":
            claim_source = "local"
            trust_status = "local"
        else:
            if claim_source is None:
                raise ValueError(
                    "Remote endpoint claims require an explicit claim_source"
                )
            if claim_source == "local":
                raise ValueError("Remote endpoint claims cannot use local claim_source")
            if trust_status is None:
                trust_status = "untrusted"
            if trust_status == "local":
                raise ValueError("Remote endpoint claims cannot use local trust_status")
        await self.client_connected(
            client_id=client_id,
            client_kind=client_kind,
            transport_kind=transport_kind,
            transport_id=transport_id,
        )
        events: list[RouteEvent] = []
        accepted: EndpointRoute | None = None
        async with self._lock:
            candidate = EndpointRoute(
                lane=lane,
                endpoint=parsed,
                client_id=client_id,
                client_kind=client_kind,
                direct=direct,
                transport_kind=transport_kind,
                transport_id=transport_id,
                claim_source=claim_source,
                trust_status=trust_status,
                capabilities=normalized_capabilities,
            )
            rejection_reason = self._claim_rejection_reason(
                candidate,
            )
            if rejection_reason is not None:
                events.append(
                    RouteEvent(
                        event_type="endpointClaimRejected",
                        client_id=client_id,
                        lane=lane,
                        endpoint=parsed,
                        rejected_route=candidate,
                        reason=rejection_reason,
                    )
                )
                existing = None
            else:
                route_key = self._route_key(lane=lane, endpoint=parsed)
                existing = self._routes.get(route_key)
            if existing is not None:
                if existing == candidate:
                    return existing
                if self._claim_precedence(candidate) > self._claim_precedence(existing):
                    routes = self._routes_by_client.get(existing.client_id)
                    if routes is not None:
                        routes.discard(route_key)
                    self._routes[route_key] = candidate
                    self._routes_by_client.setdefault(client_id, set()).add(route_key)
                    accepted = candidate
                    events.append(
                        RouteEvent(
                            event_type="endpointUnreachable",
                            client_id=existing.client_id,
                            lane=lane,
                            endpoint=parsed,
                            route=existing,
                            reason=self._replacement_reason(existing, candidate),
                        )
                    )
                    events.append(
                        RouteEvent(
                            event_type="endpointReachable",
                            client_id=client_id,
                            lane=lane,
                            endpoint=parsed,
                            route=candidate,
                        )
                    )
                else:
                    reason = "endpoint already claimed"
                    if existing.client_kind == "local" and client_kind == "remote":
                        reason = "local endpoint claim owns endpoint"
                    events.append(
                        RouteEvent(
                            event_type="endpointClaimRejected",
                            client_id=client_id,
                            lane=lane,
                            endpoint=parsed,
                            route=existing,
                            rejected_route=candidate,
                            reason=reason,
                        )
                    )
            elif rejection_reason is None:
                route_key = self._route_key(lane=lane, endpoint=parsed)
                self._routes[route_key] = candidate
                self._routes_by_client.setdefault(client_id, set()).add(route_key)
                accepted = candidate
                events.append(
                    RouteEvent(
                        event_type="endpointReachable",
                        client_id=client_id,
                        lane=lane,
                        endpoint=parsed,
                        route=candidate,
                    )
                )
        for event in events:
            await self._publish(event)
        return accepted

    async def withdraw_endpoint(
        self,
        *,
        endpoint: str | EndpointAddress,
        lane: str,
        client_id: str,
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        event: RouteEvent | None = None
        route: EndpointRoute | None = None
        async with self._lock:
            route_key = self._route_key(lane=lane, endpoint=parsed)
            existing = self._routes.get(route_key)
            if existing is None or existing.client_id != client_id:
                return None
            route = self._routes.pop(route_key)
            routes = self._routes_by_client.get(client_id)
            if routes is not None:
                routes.discard(route_key)
            event = RouteEvent(
                event_type="endpointUnreachable",
                client_id=client_id,
                lane=lane,
                endpoint=parsed,
                route=route,
                reason="withdrawn",
            )
        if event is not None:
            await self._publish(event)
        return route

    async def route_for(
        self,
        endpoint: str | EndpointAddress,
        *,
        lane: str,
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        async with self._lock:
            return self._routes.get(self._route_key(lane=lane, endpoint=parsed))

    async def routes_for_client(self, client_id: str) -> tuple[EndpointRoute, ...]:
        async with self._lock:
            route_keys = self._routes_by_client.get(client_id, set())
            return tuple(
                route
                for route_key in route_keys
                if (route := self._routes.get(route_key)) is not None
            )

    async def claim_remote_sender(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
        transport_kind: str | None = None,
        transport_id: str | None = None,
        direct: bool = True,
        trust_status: RouteTrustStatus = "untrusted",
        capabilities: tuple[str, ...] | list[str] = (),
    ) -> EndpointRoute | None:
        return await self.claim_endpoint(
            endpoint=message.sender,
            lane=message.lane,
            client_id=client_id,
            client_kind="remote",
            transport_kind=transport_kind,
            transport_id=transport_id,
            direct=direct,
            claim_source="message_sender",
            trust_status=trust_status,
            capabilities=capabilities,
        )

    async def remote_message_rejection_reason(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
    ) -> str | None:
        reason = self._message_policy_rejection_reason(
            message,
            boundary="remote_ingress",
        )
        if reason is not None:
            await self._publish_message_rejected(
                message,
                client_id=client_id,
                reason=reason,
            )
        return reason

    async def local_message_rejection_reason(
        self,
        message: DeckrMessage,
    ) -> str | None:
        reason = self._message_policy_rejection_reason(
            message,
            boundary="local",
        )
        if reason is not None:
            await self._publish_message_rejected(
                message,
                client_id=None,
                reason=reason,
            )
        return reason

    async def message_dropped(
        self,
        message: DeckrMessage,
        *,
        client_id: str | None,
        reason: str,
    ) -> None:
        await self._publish(
            RouteEvent(
                event_type="messageDropped",
                client_id=client_id,
                lane=message.lane,
                message_id=message.message_id,
                message_type=message.message_type,
                sender=message.sender,
                reason=reason,
            )
        )

    def delivery_for_lane(self, lane: str) -> DeliverySemantics | None:
        return self._lane_contracts.contract_for(lane).delivery

    async def route_targets_client(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
    ) -> bool:
        if not should_forward_to_client(message, client_id=client_id):
            return False

        reason = self._message_policy_rejection_reason(
            message,
            boundary="transport_egress",
        )
        if reason is not None:
            await self._publish_message_rejected(
                message,
                client_id=client_id,
                reason=reason,
            )
            return False

        bridge_rejection: str | None = None
        targets_client = False
        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return False
            contract = self._lane_contracts.contract_for(message.lane)
            if (
                client.client_kind == "remote"
                and message.route is not None
                and message.route.origin_client_id is not None
                and message.route.origin_client_id != client_id
                and contract.route_policy.bridgeable is not True
            ):
                bridge_rejection = (
                    f"lane {message.lane!r} does not allow remote-to-remote forwarding"
                )
            else:
                recipient = message.recipient
                if isinstance(recipient, EndpointTarget):
                    route = self._routes.get(
                        self._route_key(lane=message.lane, endpoint=recipient.endpoint)
                    )
                    targets_client = route is not None and route.client_id == client_id
                elif isinstance(recipient, BroadcastTarget):
                    route_keys = self._routes_by_client.get(client_id, set())
                    targets_client = any(
                        route.lane == message.lane
                        and route.endpoint.family == recipient.endpoint_family
                        for route_key in route_keys
                        if (route := self._routes.get(route_key)) is not None
                    )

        if bridge_rejection is not None:
            await self._publish_message_rejected(
                message,
                client_id=client_id,
                reason=bridge_rejection,
            )
            return False
        return targets_client

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[RouteEvent]]:
        send, receive = anyio.create_memory_object_stream[RouteEvent](
            max_buffer_size=100
        )
        async with self._lock:
            self._subscribers.add(send)
        try:
            yield receive
        finally:
            async with self._lock:
                self._subscribers.discard(send)
            await send.aclose()
            await receive.aclose()

    async def _publish(self, event: RouteEvent) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)
        for send in subscribers:
            try:
                send.send_nowait(event)
            except anyio.WouldBlock:
                pass
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                async with self._lock:
                    self._subscribers.discard(send)

    async def _publish_message_rejected(
        self,
        message: DeckrMessage,
        *,
        client_id: str | None,
        reason: str,
    ) -> None:
        await self._publish(
            RouteEvent(
                event_type="messageRejected",
                client_id=client_id,
                lane=message.lane,
                message_id=message.message_id,
                message_type=message.message_type,
                sender=message.sender,
                reason=reason,
            )
        )

    def _route_key(self, *, lane: str, endpoint: EndpointAddress) -> RouteKey:
        return (lane, endpoint)

    def _message_policy_rejection_reason(
        self,
        message: DeckrMessage,
        *,
        boundary: MessagePolicyBoundary | Literal["local"],
    ) -> str | None:
        contract = self._lane_contracts.contract_for(message.lane)
        policy = contract.route_policy

        if message_is_expired(message):
            delivery = contract.delivery
            if delivery is None or delivery.expiry == ExpiryHandling.DROP_AND_REPORT:
                return f"message {message.message_id!r} expired"
            return f"message {message.message_id!r} expired"

        if (
            contract.message_types
            and message.message_type not in contract.message_types
        ):
            return (
                f"message type {message.message_type!r} is not supported "
                f"on lane {message.lane!r}"
            )

        if (
            boundary != "local"
            and message.message_type in policy.local_only_message_types
        ):
            return (
                f"message type {message.message_type!r} on lane {message.lane!r} "
                "is local-only"
            )

        sender_families = policy.allowed_sender_families
        if sender_families is not None and message.sender.family not in sender_families:
            return (
                f"sender family {message.sender.family!r} is not allowed "
                f"on lane {message.lane!r}"
            )

        reserved_sender = self._reserved_endpoint_rejection_reason(
            message.sender,
            lane=message.lane,
            role="sender",
        )
        if reserved_sender is not None:
            return reserved_sender

        recipient = message.recipient
        if isinstance(recipient, EndpointTarget):
            recipient_families = policy.allowed_recipient_families
            if (
                recipient_families is not None
                and recipient.endpoint.family not in recipient_families
            ):
                return (
                    f"recipient family {recipient.endpoint.family!r} is not allowed "
                    f"on lane {message.lane!r}"
                )
            return self._reserved_endpoint_rejection_reason(
                recipient.endpoint,
                lane=message.lane,
                role="recipient",
            )

        if isinstance(recipient, BroadcastTarget):
            return self._broadcast_rejection_reason(
                recipient,
                lane=message.lane,
                boundary=boundary,
            )
        return "message recipient target is not supported"

    def _reserved_endpoint_rejection_reason(
        self,
        endpoint: EndpointAddress,
        *,
        lane: str,
        role: str,
    ) -> str | None:
        contract = self._lane_contracts.contract_for(lane)
        reserved_families = contract.route_policy.reserved_endpoint_ids.get(
            endpoint.endpoint_id,
            frozenset(),
        )
        if endpoint.family not in reserved_families:
            return None
        return (
            f"reserved endpoint id {endpoint.endpoint_id!r} cannot be used as "
            f"{role} for family {endpoint.family!r} on lane {lane!r}"
        )

    def _broadcast_rejection_reason(
        self,
        target: BroadcastTarget,
        *,
        lane: str,
        boundary: MessagePolicyBoundary,
    ) -> str | None:
        contract = self._lane_contracts.contract_for(lane)
        policy = contract.route_policy
        if not policy.broadcast_targets:
            return f"broadcast messages are not allowed on lane {lane!r}"
        expected_family = policy.broadcast_targets.get(target.scope)
        if expected_family is None:
            return f"broadcast scope {target.scope!r} is not allowed on lane {lane!r}"
        if target.endpoint_family != expected_family:
            return (
                f"broadcast scope {target.scope!r} on lane {lane!r} requires "
                f"endpoint family {expected_family!r}, got {target.endpoint_family!r}"
            )
        if target.domain is not None:
            return (
                f"broadcast domain {target.domain!r} is not supported on lane {lane!r}"
            )
        hop_limit = (
            target.hop_limit
            if target.hop_limit is not None
            else policy.default_broadcast_hop_limit
        )
        if hop_limit is None:
            return None
        if hop_limit < 0:
            return "broadcast hop_limit must be non-negative"
        if hop_limit > 1:
            return "multi-hop broadcast requires bridge policy"
        if hop_limit == 0 and boundary in {"remote_ingress", "transport_egress"}:
            return "broadcast hop_limit=0 cannot cross a transport boundary"
        return None

    def _claim_rejection_reason(
        self,
        route: EndpointRoute,
    ) -> str | None:
        contract = self._lane_contracts.contract_for(route.lane)
        reserved_families = contract.route_policy.reserved_endpoint_ids.get(
            route.endpoint.endpoint_id,
            frozenset(),
        )
        if route.endpoint.family in reserved_families:
            return (
                f"reserved endpoint id {route.endpoint.endpoint_id!r} cannot "
                f"be claimed for family {route.endpoint.family!r} "
                f"on lane {route.lane!r}"
            )
        if route.client_kind != "remote":
            return None
        families = contract.route_policy.remote_claim_endpoint_families
        if not families:
            return f"remote endpoint claims are not allowed on lane {route.lane!r}"
        if route.endpoint.family not in families:
            return (
                f"endpoint family {route.endpoint.family!r} cannot be claimed "
                f"on lane {route.lane!r}"
            )
        if not route.direct and route.trust_status != "trusted":
            return "bridged endpoint claims require trusted route authority"
        return None

    def _claim_precedence(self, route: EndpointRoute) -> int:
        if route.client_kind == "local":
            return _TRUST_PRECEDENCE["local"]
        return _TRUST_PRECEDENCE[route.trust_status]

    def _replacement_reason(
        self,
        existing: EndpointRoute,
        candidate: EndpointRoute,
    ) -> str:
        if candidate.client_kind == "local" and existing.client_kind == "remote":
            return "localClaimReplaced"
        return "higherAuthorityClaimReplaced"


def mark_received_from_client(
    message: DeckrMessage,
    *,
    client_id: str,
) -> DeckrMessage:
    route = message.route
    origin_client_id = route.origin_client_id if route else None
    route_history = tuple(route.route_history if route else ())
    if client_id not in route_history:
        route_history = (*route_history, client_id)[-MAX_ROUTE_HISTORY:]
    return message.model_copy(
        update={
            "route": RouteMetadata(
                origin_client_id=origin_client_id or client_id,
                current_client_id=client_id,
                hop_count=(route.hop_count + 1) if route else 1,
                route_history=route_history,
            )
        }
    )


def should_forward_to_client(message: DeckrMessage, *, client_id: str) -> bool:
    route = message.route
    if route is None:
        return True
    if route.current_client_id == client_id:
        return False
    return client_id not in route.route_history


def mark_forwarded_to_client(
    message: DeckrMessage,
    *,
    client_id: str,
) -> DeckrMessage:
    route = message.route
    route_history = tuple(route.route_history if route else ())
    if client_id not in route_history:
        route_history = (*route_history, client_id)[-MAX_ROUTE_HISTORY:]
    return message.model_copy(
        update={
            "route": RouteMetadata(
                origin_client_id=route.origin_client_id if route else None,
                current_client_id=client_id,
                hop_count=(route.hop_count + 1) if route else 1,
                route_history=route_history,
            )
        }
    )


async def route_targets_client(
    route_table: RouteTable,
    message: DeckrMessage,
    *,
    client_id: str,
) -> bool:
    return await route_table.route_targets_client(message, client_id=client_id)
