from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from deckr.contracts.messages import DeckrMessage
from deckr.transports.bus import EventBus
from deckr.transports.routes import (
    RouteTable,
    mark_received_from_client,
    route_targets_client,
)

SendRemote = Callable[[DeckrMessage], Awaitable[None]]


class LaneHandler(Protocol):
    async def handle_local_message(
        self,
        message: DeckrMessage,
        *,
        send_remote: SendRemote,
    ) -> None: ...

    async def handle_remote_message(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
    ) -> None: ...

    async def route_targets_client(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
    ) -> bool: ...

    async def handle_transport_disconnect(self) -> None: ...


class DeckrLaneHandler:
    def __init__(
        self,
        *,
        lane: str,
        bus: EventBus,
        route_table: RouteTable,
        transport_kind: str,
        transport_id: str,
    ) -> None:
        self._lane = lane
        self._bus = bus
        self._route_table = route_table
        self._transport_kind = transport_kind
        self._transport_id = transport_id

    async def handle_local_message(
        self,
        message: DeckrMessage,
        *,
        send_remote: SendRemote,
    ) -> None:
        if message.lane != self._lane:
            return
        await send_remote(message)

    async def handle_remote_message(
        self,
        message: DeckrMessage,
        *,
        client_id: str,
    ) -> None:
        if message.lane != self._lane:
            raise ValueError(
                f"Remote message for lane {message.lane!r} cannot enter {self._lane!r}"
            )
        route = await self._route_table.claim_endpoint(
            endpoint=message.sender,
            client_id=client_id,
            client_kind="remote",
            transport_kind=self._transport_kind,
            transport_id=self._transport_id,
        )
        if route is None:
            return
        await self._bus.send(mark_received_from_client(message, client_id=client_id))

    async def route_targets_client(self, message: DeckrMessage, *, client_id: str) -> bool:
        if message.lane != self._lane:
            return False
        return await route_targets_client(
            self._route_table,
            message,
            client_id=client_id,
        )

    async def handle_transport_disconnect(self) -> None:
        return


def build_lane_handler(
    *,
    lane: str,
    transport_kind: str,
    transport_id: str,
    bus: EventBus,
) -> LaneHandler:
    return DeckrLaneHandler(
        lane=lane,
        bus=bus,
        route_table=bus.route_table,
        transport_kind=transport_kind,
        transport_id=transport_id,
    )
