"""Generic WebSocket gateways: bridge EventBus with WebSocket clients or servers."""

from __future__ import annotations

import http
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
from anyio import get_cancelled_exc_class
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from deckr.core._bridge import (
    build_bridge_envelope,
    event_originated_from_bridge,
    mark_event_from_bridge,
    parse_bridge_envelope,
)
from deckr.core.component import BaseComponent, RunContext

if TYPE_CHECKING:
    from deckr.core.messaging import EventBus

logger = logging.getLogger(__name__)

_SEND_TIMEOUT = 0.25
_UNSUPPORTED_DATA_CLOSE_CODE = 1003


@dataclass(frozen=True)
class WebSocketServerGatewayConfig:
    host: str
    port: int
    path: str = "/ws"
    allowed_origins: tuple[str, ...] = ()
    allow_no_origin: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.path)


@dataclass(frozen=True)
class WebSocketClientGatewayConfig:
    uri: str
    origin: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.uri)


class _BaseWebSocketGateway(BaseComponent):
    def __init__(
        self,
        event_bus: EventBus,
        *,
        config: WebSocketServerGatewayConfig | WebSocketClientGatewayConfig,
        serialize: Callable[[Any], dict],
        deserialize: Callable[[dict], Any],
        deserialize_from_websocket: Callable[[dict], Any] | None = None,
        is_event: Callable[[Any], bool] | None = None,
        name: str,
    ) -> None:
        super().__init__(name=name)
        self._event_bus = event_bus
        self._config = config
        self._serialize = serialize
        self._deserialize = deserialize
        self._deserialize_from_websocket = deserialize_from_websocket or deserialize
        self._is_event = is_event if is_event is not None else lambda _: True
        self._gateway_id = str(uuid.uuid4())
        self._ready = anyio.Event()

    async def wait_ready(self, timeout: float | None = None) -> bool:
        if timeout is None:
            await self._ready.wait()
            return True

        with anyio.move_on_after(timeout) as scope:
            await self._ready.wait()
        return not scope.cancel_called

    def _encode_event(self, event: Any) -> str:
        return json.dumps(
            build_bridge_envelope(self._gateway_id, self._serialize(event))
        )

    def _decode_event(self, raw_message: str) -> tuple[str | None, Any]:
        envelope = json.loads(raw_message)
        remote_gateway_id, message = parse_bridge_envelope(envelope)
        event = self._deserialize_from_websocket(message)
        return remote_gateway_id, mark_event_from_bridge(event, self._gateway_id)

    def _should_forward_event(self, event: Any) -> bool:
        return self._is_event(event) and not event_originated_from_bridge(
            event, self._gateway_id
        )


class WebSocketServerGateway(_BaseWebSocketGateway):
    """Bidirectional bridge: EventBus <-> WebSocket server."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        config: WebSocketServerGatewayConfig,
        serialize: Callable[[Any], dict],
        deserialize: Callable[[dict], Any],
        deserialize_from_websocket: Callable[[dict], Any] | None = None,
        is_event: Callable[[Any], bool] | None = None,
    ) -> None:
        super().__init__(
            event_bus,
            config=config,
            serialize=serialize,
            deserialize=deserialize,
            deserialize_from_websocket=deserialize_from_websocket,
            is_event=is_event,
            name="websocket_server_gateway",
        )
        self._connections_lock = anyio.Lock()
        self._connections: set[ServerConnection] = set()
        self._server: Server | None = None

    async def start(self, ctx: RunContext) -> None:
        ctx.tg.start_soon(self._run, name="websocket_server_gateway_run")
        logger.info(
            "WebSocketServerGateway listening on %s:%s%s",
            self._config.host,
            self._config.port,
            self._config.path,
        )

    async def _run(self) -> None:
        async with anyio.create_task_group() as tg:
            server = await serve(
                self._handle_connection,
                self._config.host,
                self._config.port,
                origins=self._allowed_origins(),
                process_request=self._process_request,
            )
            self._server = server
            self._ready.set()
            try:
                tg.start_soon(self._bus_to_clients_loop)
                await server.wait_closed()
            finally:
                tg.cancel_scope.cancel()
                server.close()
                await self._close_all_connections()
                await server.wait_closed()
                self._server = None

    def _allowed_origins(self) -> list[str | None]:
        origins: list[str | None] = list(self._config.allowed_origins)
        if self._config.allow_no_origin:
            origins.append(None)
        return origins

    async def _process_request(self, connection: ServerConnection, request):
        if urlsplit(request.path).path != self._config.path:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not Found\n")
        return None

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        async with self._connection_context(websocket):
            async for raw_message in websocket:
                if isinstance(raw_message, bytes):
                    await websocket.close(
                        code=_UNSUPPORTED_DATA_CLOSE_CODE,
                        reason="Binary frames are not supported",
                    )
                    return

                try:
                    remote_gateway_id, event = self._decode_event(raw_message)
                    if remote_gateway_id == self._gateway_id:
                        continue
                    await self._broadcast_text(raw_message, exclude={websocket})
                    await self._event_bus.send(event)
                except json.JSONDecodeError:
                    logger.warning("Dropped malformed WebSocket JSON message")
                except ValueError:
                    logger.warning("Dropped malformed WebSocket bridge envelope")
                except Exception:
                    logger.exception("Error forwarding WebSocket message to bus")

    async def _bus_to_clients_loop(self) -> None:
        async with self._event_bus.subscribe() as stream:
            async for event in stream:
                if not self._should_forward_event(event):
                    continue
                try:
                    payload = self._encode_event(event)
                    await self._broadcast_text(payload)
                except Exception:
                    logger.exception("Error publishing to WebSocket clients")
        raise RuntimeError(
            "WebSocket server event bus subscription closed unexpectedly"
        )

    async def _broadcast_text(
        self,
        payload: str,
        *,
        exclude: Iterable[ServerConnection] = (),
    ) -> None:
        excluded = set(exclude)
        async with self._connections_lock:
            connections = [ws for ws in self._connections if ws not in excluded]

        to_close: list[ServerConnection] = []
        for websocket in connections:
            try:
                with anyio.move_on_after(_SEND_TIMEOUT) as scope:
                    await websocket.send(payload)
                if scope.cancel_called:
                    to_close.append(websocket)
            except ConnectionClosed:
                to_close.append(websocket)
            except Exception:
                logger.exception("Error sending WebSocket message to client")
                to_close.append(websocket)

        for websocket in to_close:
            await self._drop_connection(websocket)

    async def _drop_connection(self, websocket: ServerConnection) -> None:
        async with self._connections_lock:
            self._connections.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _add_connection(self, websocket: ServerConnection) -> None:
        async with self._connections_lock:
            self._connections.add(websocket)

    async def _remove_connection(self, websocket: ServerConnection) -> None:
        async with self._connections_lock:
            self._connections.discard(websocket)

    async def _close_all_connections(self) -> None:
        async with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for websocket in connections:
            try:
                await websocket.close()
            except Exception:
                pass

    @asynccontextmanager
    async def _connection_context(self, websocket: ServerConnection):
        await self._add_connection(websocket)
        try:
            yield
        finally:
            await self._remove_connection(websocket)


class WebSocketClientGateway(_BaseWebSocketGateway):
    """Bidirectional bridge: EventBus <-> outbound WebSocket client."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        config: WebSocketClientGatewayConfig,
        serialize: Callable[[Any], dict],
        deserialize: Callable[[dict], Any],
        deserialize_from_websocket: Callable[[dict], Any] | None = None,
        is_event: Callable[[Any], bool] | None = None,
        online_event: Any | None = None,
        offline_event: Any | None = None,
    ) -> None:
        super().__init__(
            event_bus,
            config=config,
            serialize=serialize,
            deserialize=deserialize,
            deserialize_from_websocket=deserialize_from_websocket,
            is_event=is_event,
            name="websocket_client_gateway",
        )
        self._online_event = online_event
        self._offline_event = offline_event
        self._connection_lock = anyio.Lock()
        self._connection: ClientConnection | None = None

    async def start(self, ctx: RunContext) -> None:
        ctx.tg.start_soon(self._run, name="websocket_client_gateway_run")
        logger.info("WebSocketClientGateway connecting to %s", self._config.uri)

    async def _run(self) -> None:
        cancelled_exc = get_cancelled_exc_class()

        try:
            async for websocket in connect(
                self._config.uri,
                origin=self._config.origin,
            ):
                await self._set_connection(websocket)
                try:
                    await self._emit_gateway_event(self._online_event)
                    await self._connection_session(websocket)
                except cancelled_exc:
                    raise
                except ConnectionClosed:
                    logger.info(
                        "WebSocket client disconnected from %s; retrying",
                        self._config.uri,
                    )
                finally:
                    await self._set_connection(None)
                    await self._emit_gateway_event(self._offline_event)
        except cancelled_exc:
            raise

    async def _connection_session(self, websocket: ClientConnection) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._bus_to_websocket_loop, websocket)
            self._ready.set()
            try:
                await self._websocket_to_bus_loop(websocket)
            finally:
                tg.cancel_scope.cancel()

    async def _bus_to_websocket_loop(self, websocket: ClientConnection) -> None:
        async with self._event_bus.subscribe() as stream:
            async for event in stream:
                if not self._should_forward_event(event):
                    continue
                try:
                    payload = self._encode_event(event)
                    await websocket.send(payload)
                except Exception:
                    logger.exception("Error publishing to WebSocket server")
        raise RuntimeError(
            "WebSocket client event bus subscription closed unexpectedly"
        )

    async def _websocket_to_bus_loop(self, websocket: ClientConnection) -> None:
        async for raw_message in websocket:
            if isinstance(raw_message, bytes):
                await websocket.close(
                    code=_UNSUPPORTED_DATA_CLOSE_CODE,
                    reason="Binary frames are not supported",
                )
                return

            try:
                remote_gateway_id, event = self._decode_event(raw_message)
                if remote_gateway_id == self._gateway_id:
                    continue
                await self._event_bus.send(event)
            except json.JSONDecodeError:
                logger.warning("Dropped malformed WebSocket JSON message")
            except ValueError:
                logger.warning("Dropped malformed WebSocket bridge envelope")
            except Exception:
                logger.exception("Error forwarding WebSocket message to bus")

    async def _emit_gateway_event(self, event: Any | None) -> None:
        if event is None:
            return
        await self._event_bus.send(mark_event_from_bridge(event, self._gateway_id))

    async def stop(self) -> None:
        async with self._connection_lock:
            websocket = self._connection
        if websocket is None:
            return
        await websocket.close()

    async def _set_connection(self, websocket: ClientConnection | None) -> None:
        async with self._connection_lock:
            self._connection = websocket
