from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import anyio

from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    RouteMetadata,
    parse_endpoint_address,
)

RouteClientKind = Literal["local", "remote"]
RouteEventType = Literal[
    "clientConnected",
    "clientDisconnected",
    "endpointReachable",
    "endpointUnreachable",
    "endpointClaimRejected",
]

MAX_ROUTE_HISTORY = 16


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
    endpoint: EndpointAddress
    client_id: str
    client_kind: RouteClientKind
    direct: bool = True
    transport_kind: str | None = None
    transport_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouteEvent:
    event_type: RouteEventType
    client_id: str | None = None
    endpoint: EndpointAddress | None = None
    route: EndpointRoute | None = None
    rejected_route: EndpointRoute | None = None
    reason: str | None = None


class RouteTable:
    """In-process Deckr endpoint reachability and forwarding state."""

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._clients: dict[str, RouteClient] = {}
        self._routes: dict[EndpointAddress, EndpointRoute] = {}
        self._endpoints_by_client: dict[str, set[EndpointAddress]] = {}
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
                self._endpoints_by_client.setdefault(client_id, set())
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
            endpoints = self._endpoints_by_client.pop(client_id, set())
            self._clients.pop(client_id, None)
            for endpoint in endpoints:
                route = self._routes.get(endpoint)
                if route is None or route.client_id != client_id:
                    continue
                self._routes.pop(endpoint, None)
                removed.append(route)
                events.append(
                    RouteEvent(
                        event_type="endpointUnreachable",
                        client_id=client_id,
                        endpoint=endpoint,
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
        client_id: str,
        client_kind: RouteClientKind,
        transport_kind: str | None = None,
        transport_id: str | None = None,
        direct: bool = True,
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        await self.client_connected(
            client_id=client_id,
            client_kind=client_kind,
            transport_kind=transport_kind,
            transport_id=transport_id,
        )
        events: list[RouteEvent] = []
        accepted: EndpointRoute | None = None
        async with self._lock:
            existing = self._routes.get(parsed)
            candidate = EndpointRoute(
                endpoint=parsed,
                client_id=client_id,
                client_kind=client_kind,
                direct=direct,
                transport_kind=transport_kind,
                transport_id=transport_id,
            )
            if existing is not None:
                if existing == candidate:
                    return existing
                if client_kind == "local" and existing.client_kind == "remote":
                    endpoints = self._endpoints_by_client.get(existing.client_id)
                    if endpoints is not None:
                        endpoints.discard(parsed)
                    self._routes[parsed] = candidate
                    self._endpoints_by_client.setdefault(client_id, set()).add(parsed)
                    accepted = candidate
                    events.append(
                        RouteEvent(
                            event_type="endpointUnreachable",
                            client_id=existing.client_id,
                            endpoint=parsed,
                            route=existing,
                            reason="localClaimReplaced",
                        )
                    )
                    events.append(
                        RouteEvent(
                            event_type="endpointReachable",
                            client_id=client_id,
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
                            endpoint=parsed,
                            route=existing,
                            rejected_route=candidate,
                            reason=reason,
                        )
                    )
            else:
                self._routes[parsed] = candidate
                self._endpoints_by_client.setdefault(client_id, set()).add(parsed)
                accepted = candidate
                events.append(
                    RouteEvent(
                        event_type="endpointReachable",
                        client_id=client_id,
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
        client_id: str,
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        event: RouteEvent | None = None
        route: EndpointRoute | None = None
        async with self._lock:
            existing = self._routes.get(parsed)
            if existing is None or existing.client_id != client_id:
                return None
            route = self._routes.pop(parsed)
            endpoints = self._endpoints_by_client.get(client_id)
            if endpoints is not None:
                endpoints.discard(parsed)
            event = RouteEvent(
                event_type="endpointUnreachable",
                client_id=client_id,
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
    ) -> EndpointRoute | None:
        parsed = parse_endpoint_address(endpoint)
        async with self._lock:
            return self._routes.get(parsed)

    async def routes_for_client(self, client_id: str) -> tuple[EndpointRoute, ...]:
        async with self._lock:
            endpoints = self._endpoints_by_client.get(client_id, set())
            return tuple(
                route
                for endpoint in endpoints
                if (route := self._routes.get(endpoint)) is not None
            )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[anyio.abc.ObjectReceiveStream[RouteEvent]]:
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
    route = await route_table.route_for(recipient.endpoint)
    return route is not None and route.client_id == client_id
