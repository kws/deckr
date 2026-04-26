from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import anyio

from deckr.contracts.lanes import (
    DEFAULT_LANE_CONTRACT_REGISTRY,
    LaneContractRegistry,
)
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    RouteMetadata,
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
]

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
            rejection_reason = self._remote_claim_rejection_reason(
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

    def _route_key(self, *, lane: str, endpoint: EndpointAddress) -> RouteKey:
        return (lane, endpoint)

    def _remote_claim_rejection_reason(
        self,
        route: EndpointRoute,
    ) -> str | None:
        if route.client_kind != "remote":
            return None
        contract = self._lane_contracts.contract_for(route.lane)
        families = contract.route_policy.remote_claim_endpoint_families
        if not families:
            return f"remote endpoint claims are not allowed on lane {route.lane!r}"
        if route.endpoint.family not in families:
            return (
                f"endpoint family {route.endpoint.family!r} cannot be claimed "
                f"on lane {route.lane!r}"
            )
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
    if not should_forward_to_client(message, client_id=client_id):
        return False
    recipient = message.recipient
    if not isinstance(recipient, EndpointTarget):
        return True
    route = await route_table.route_for(recipient.endpoint, lane=message.lane)
    return route is not None and route.client_id == client_id
