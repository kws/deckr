from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from deckr.contracts.messages import DeckrMessage
from deckr.transports.bus import EventBus

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
    ) -> None: ...

    async def handle_transport_disconnect(self) -> None: ...


class DeckrLaneHandler:
    def __init__(self, *, lane: str, bus: EventBus) -> None:
        self._lane = lane
        self._bus = bus
        self._remote_message_ids: set[str] = set()

    async def handle_local_message(
        self,
        message: DeckrMessage,
        *,
        send_remote: SendRemote,
    ) -> None:
        if message.lane != self._lane:
            return
        if message.message_id in self._remote_message_ids:
            self._remote_message_ids.discard(message.message_id)
            return
        await send_remote(message)

    async def handle_remote_message(
        self,
        message: DeckrMessage,
    ) -> None:
        if message.lane != self._lane:
            raise ValueError(
                f"Remote message for lane {message.lane!r} cannot enter {self._lane!r}"
            )
        self._remote_message_ids.add(message.message_id)
        await self._bus.send(message)

    async def handle_transport_disconnect(self) -> None:
        return


def build_lane_handler(
    *,
    lane: str,
    transport_kind: str,
    transport_id: str,
    bus: EventBus,
) -> LaneHandler:
    del transport_kind, transport_id
    return DeckrLaneHandler(lane=lane, bus=bus)
