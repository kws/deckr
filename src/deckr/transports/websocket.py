from __future__ import annotations

import http
import json
import logging
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import anyio
from anyio import get_cancelled_exc_class
from pydantic import ValidationError

from deckr.contracts.messages import (
    DeckrMessage,
    TransportFrame,
)
from deckr.core.component import BaseComponent, RunContext
from deckr.core.components import (
    ComponentCardinality,
    ComponentContext,
    ComponentDefinition,
    ComponentManifest,
    InactiveComponent,
    ResolvedLaneSet,
)
from deckr.transports._common import (
    TransportBindingConfigBase,
    _StrictConfigModel,
    lanes_for_bindings,
    transport_id_for,
    validate_binding_schema_ids,
)
from deckr.transports._lanes import build_lane_handler
from deckr.transports.routes import mark_forwarded_to_client

logger = logging.getLogger(__name__)

_SEND_TIMEOUT = 0.25
_UNSUPPORTED_DATA_CLOSE_CODE = 1003
TRANSPORT_KIND = "websocket"


class WebSocketTransportFrameError(ValueError):
    """Raised when a WebSocket transport frame is invalid."""


def build_websocket_frame(
    transport_id: str,
    message: DeckrMessage,
    *,
    client_id: str | None = None,
) -> dict[str, Any]:
    return TransportFrame(
        transportId=transport_id,
        clientId=client_id,
        message=message,
    ).to_dict()


def parse_websocket_frame(payload: Any) -> TransportFrame:
    if not isinstance(payload, dict):
        raise WebSocketTransportFrameError(
            "WebSocket transport frame must be a JSON object"
        )
    try:
        return TransportFrame.from_dict(payload)
    except ValidationError as exc:
        raise WebSocketTransportFrameError(
            "WebSocket transport frame is invalid"
        ) from exc


class WebSocketTransportBindingConfig(TransportBindingConfigBase):
    path: str | None = None
    uri: str | None = None


class WebSocketTransportConfig(_StrictConfigModel):
    enabled: bool = True
    transport_id: str | None = None
    mode: str = "server"
    host: str = "127.0.0.1"
    port: int = 0
    origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allow_no_origin: bool = True
    bindings: dict[str, WebSocketTransportBindingConfig]


class _BindingRuntime:
    def __init__(
        self,
        *,
        binding_id: str,
        config: WebSocketTransportBindingConfig,
        bus: Any,
        transport_id: str,
    ) -> None:
        self.binding_id = binding_id
        self.config = config
        self.bus = bus
        self.transport_id = transport_id
        self.client_id = f"websocket:{transport_id}:{binding_id}"
        self.handler = build_lane_handler(
            lane=config.lane,
            transport_kind=TRANSPORT_KIND,
            transport_id=transport_id,
            bus=bus,
        )


class WebSocketTransportComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        transport_id: str,
        config: WebSocketTransportConfig,
        bindings: list[_BindingRuntime],
    ) -> None:
        super().__init__(name=runtime_name)
        self._transport_id = transport_id
        self._config = config
        self._bindings = bindings
        self._server = None
        self._connections_lock = anyio.Lock()
        self._connections: set[Any] = set()
        self._connection_paths: dict[Any, str] = {}
        self._connection_client_ids: dict[Any, str] = {}

    async def start(self, ctx: RunContext) -> None:
        if self._config.mode == "server":
            ctx.tg.start_soon(self._run_server, name="websocket_transport_server")
            return
        for binding in self._bindings:
            ctx.tg.start_soon(
                self._run_client_binding,
                binding,
                name=f"websocket_transport:{binding.binding_id}",
            )

    async def _run_server(self) -> None:
        try:
            from websockets.asyncio.server import serve
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket transport requires websockets. Install deckr[websocket]."
            ) from exc

        async with anyio.create_task_group() as tg:
            server = await serve(
                self._handle_server_connection,
                self._config.host,
                self._config.port,
                origins=self._allowed_origins(),
                process_request=self._process_request,
            )
            self._server = server
            for binding in self._bindings:
                if binding.config.allows_egress():
                    tg.start_soon(
                        self._server_bus_to_websocket_loop,
                        binding,
                        name=f"websocket_transport_server:{binding.binding_id}",
                    )
            try:
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

    async def _process_request(self, connection, request):
        if self._binding_for_server_path(urlsplit(request.path).path) is None:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not Found\n")
        return None

    async def _handle_server_connection(self, websocket) -> None:
        try:
            from websockets.exceptions import ConnectionClosed
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket transport requires websockets. Install deckr[websocket]."
            ) from exc

        path = self._server_connection_path(websocket)
        if path is None:
            await websocket.close(code=1008, reason="missing request path")
            return

        async with self._server_connection_context(websocket, path):
            try:
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        await websocket.close(
                            code=_UNSUPPORTED_DATA_CLOSE_CODE,
                            reason="Binary frames are not supported",
                        )
                        return

                    try:
                        frame = parse_websocket_frame(json.loads(raw))
                        if frame.transport_id == self._transport_id:
                            continue
                        binding = self._binding_for_server_path(
                            path,
                            lane=frame.message.lane,
                        )
                        if binding is None:
                            logger.warning(
                                "Dropped WebSocket message for unknown binding path=%s lane=%s",
                                path,
                                frame.message.lane,
                            )
                            continue
                        if not binding.config.allows_ingress():
                            logger.warning(
                                "Dropped WebSocket ingress on egress-only binding path=%s lane=%s",
                                path,
                                frame.message.lane,
                            )
                            continue
                        client_id = self._connection_client_ids.get(websocket)
                        if client_id is None:
                            continue
                        await binding.handler.handle_remote_message(
                            frame.message,
                            client_id=client_id,
                        )
                    except json.JSONDecodeError:
                        logger.warning("Dropped malformed WebSocket JSON message")
                    except WebSocketTransportFrameError:
                        logger.warning("Dropped malformed WebSocket transport frame")
                    except Exception:
                        logger.exception("Error forwarding WebSocket message to bus")
            except ConnectionClosed:
                return
            finally:
                await self._notify_transport_disconnect(
                    bindings=self._bindings_for_server_path(path),
                )

    async def _server_bus_to_websocket_loop(self, binding: _BindingRuntime) -> None:
        if not binding.config.allows_egress():
            return
        async with binding.bus.subscribe() as stream:
            async for message in stream:
                await binding.handler.handle_local_message(
                    message,
                    send_remote=lambda message: self._send_to_server_connections(
                        binding,
                        message,
                    ),
                )

    async def _run_client_binding(self, binding: _BindingRuntime) -> None:
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket transport requires websockets. Install deckr[websocket]."
            ) from exc

        uri = binding.config.uri or ""
        cancelled_exc = get_cancelled_exc_class()
        try:
            async for websocket in connect(uri, origin=self._config.origin):
                try:
                    async with anyio.create_task_group() as tg:
                        await binding.bus.route_table.client_connected(
                            client_id=binding.client_id,
                            client_kind="remote",
                            transport_kind=TRANSPORT_KIND,
                            transport_id=self._transport_id,
                            description=binding.binding_id,
                        )
                        if binding.config.allows_egress():
                            tg.start_soon(
                                self._client_bus_to_websocket_loop,
                                websocket,
                                binding,
                            )
                        if binding.config.allows_ingress():
                            await self._client_websocket_to_bus_loop(
                                websocket,
                                binding,
                            )
                        else:
                            await self._client_websocket_drain_loop(
                                websocket,
                                binding,
                            )
                        tg.cancel_scope.cancel()
                except cancelled_exc:
                    await binding.bus.route_table.client_disconnected(binding.client_id)
                    raise
                except ConnectionClosed:
                    await binding.bus.route_table.client_disconnected(binding.client_id)
                    logger.info(
                        "WebSocket transport client disconnected from %s; retrying",
                        uri,
                    )
        except cancelled_exc:
            raise

    async def _client_bus_to_websocket_loop(
        self, websocket, binding: _BindingRuntime
    ) -> None:
        if not binding.config.allows_egress():
            return
        async with binding.bus.subscribe() as stream:
            async for message in stream:
                await binding.handler.handle_local_message(
                    message,
                    send_remote=lambda message: self._send_to_client(
                        websocket,
                        binding,
                        message,
                    ),
                )

    async def _client_websocket_to_bus_loop(
        self, websocket, binding: _BindingRuntime
    ) -> None:
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    await websocket.close(
                        code=_UNSUPPORTED_DATA_CLOSE_CODE,
                        reason="Binary frames are not supported",
                    )
                    return

                try:
                    frame = parse_websocket_frame(json.loads(raw))
                    if (
                        frame.transport_id == self._transport_id
                        or frame.message.lane != binding.config.lane
                    ):
                        continue
                    await binding.handler.handle_remote_message(
                        frame.message,
                        client_id=binding.client_id,
                    )
                except json.JSONDecodeError:
                    logger.warning("Dropped malformed WebSocket JSON message")
                except WebSocketTransportFrameError:
                    logger.warning("Dropped malformed WebSocket transport frame")
                except Exception:
                    logger.exception("Error forwarding WebSocket message to bus")
        finally:
            await binding.bus.route_table.client_disconnected(binding.client_id)
            await binding.handler.handle_transport_disconnect()

    async def _client_websocket_drain_loop(
        self, websocket, binding: _BindingRuntime
    ) -> None:
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    await websocket.close(
                        code=_UNSUPPORTED_DATA_CLOSE_CODE,
                        reason="Binary frames are not supported",
                    )
                    return
        finally:
            await binding.bus.route_table.client_disconnected(binding.client_id)
            await binding.handler.handle_transport_disconnect()

    async def _send_to_client(
        self,
        websocket,
        binding: _BindingRuntime,
        message: DeckrMessage,
    ) -> None:
        if not await binding.handler.route_targets_client(
            message,
            client_id=binding.client_id,
        ):
            return
        frame = build_websocket_frame(
            self._transport_id,
            mark_forwarded_to_client(message, client_id=binding.client_id),
            client_id=binding.client_id,
        )
        await websocket.send(json.dumps(frame))

    async def _send_to_server_connections(
        self,
        binding: _BindingRuntime,
        message: DeckrMessage,
    ) -> None:
        try:
            from websockets.exceptions import ConnectionClosed
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket transport requires websockets. Install deckr[websocket]."
            ) from exc

        path = binding.config.path or "/ws"
        async with self._connections_lock:
            connections = [
                websocket
                for websocket in self._connections
                if self._connection_paths.get(websocket) == path
            ]

        to_close: list[Any] = []
        for websocket in connections:
            client_id = self._connection_client_ids.get(websocket)
            if client_id is None:
                continue
            if not await binding.handler.route_targets_client(
                message,
                client_id=client_id,
            ):
                continue
            frame = build_websocket_frame(
                self._transport_id,
                mark_forwarded_to_client(message, client_id=client_id),
                client_id=client_id,
            )
            text = json.dumps(frame)
            try:
                with anyio.move_on_after(_SEND_TIMEOUT) as scope:
                    await websocket.send(text)
                if scope.cancel_called:
                    to_close.append(websocket)
            except ConnectionClosed:
                to_close.append(websocket)
            except Exception:
                logger.exception("Error sending WebSocket message to client")
                to_close.append(websocket)

        for websocket in to_close:
            await self._drop_connection(websocket)

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        await server.wait_closed()

    def _binding_for_server_path(
        self,
        path: str,
        *,
        lane: str | None = None,
    ) -> _BindingRuntime | None:
        for binding in self._bindings:
            if binding.config.path != path:
                continue
            if lane is not None and binding.config.lane != lane:
                continue
            return binding
        return None

    def _bindings_for_server_path(self, path: str) -> list[_BindingRuntime]:
        return [binding for binding in self._bindings if binding.config.path == path]

    def _server_connection_path(self, websocket) -> str | None:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", None)
        if not isinstance(path, str):
            return None
        return urlsplit(path).path

    async def _add_connection(self, websocket, path: str) -> None:
        client_id = f"websocket:{self._transport_id}:{uuid.uuid4().hex}"
        async with self._connections_lock:
            self._connections.add(websocket)
            self._connection_paths[websocket] = path
            self._connection_client_ids[websocket] = client_id
        bindings = self._bindings_for_server_path(path)
        if bindings:
            await bindings[0].bus.route_table.client_connected(
                client_id=client_id,
                client_kind="remote",
                transport_kind=TRANSPORT_KIND,
                transport_id=self._transport_id,
                description=path,
            )

    async def _remove_connection(self, websocket) -> None:
        client_id: str | None = None
        async with self._connections_lock:
            self._connections.discard(websocket)
            self._connection_paths.pop(websocket, None)
            client_id = self._connection_client_ids.pop(websocket, None)
        if client_id is not None and self._bindings:
            await self._bindings[0].bus.route_table.client_disconnected(client_id)

    async def _drop_connection(self, websocket) -> None:
        await self._remove_connection(websocket)
        try:
            await websocket.close()
        except Exception:
            pass

    async def _close_all_connections(self) -> None:
        async with self._connections_lock:
            connections = list(self._connections)
            client_ids = list(self._connection_client_ids.values())
            self._connections.clear()
            self._connection_paths.clear()
            self._connection_client_ids.clear()
        for client_id in client_ids:
            if self._bindings:
                await self._bindings[0].bus.route_table.client_disconnected(client_id)
        for websocket in connections:
            try:
                await websocket.close()
            except Exception:
                pass

    @asynccontextmanager
    async def _server_connection_context(self, websocket, path: str):
        await self._add_connection(websocket, path)
        try:
            yield
        finally:
            await self._remove_connection(websocket)

    async def _notify_transport_disconnect(
        self,
        *,
        bindings: list[_BindingRuntime],
    ) -> None:
        for binding in bindings:
            await binding.handler.handle_transport_disconnect()


def _validate_config(config: WebSocketTransportConfig) -> WebSocketTransportConfig:
    mode = config.mode.strip().lower()
    if mode not in {"client", "server"}:
        raise ValueError("WebSocket transport mode must be 'client' or 'server'")
    config.mode = mode

    if not config.bindings:
        raise ValueError("WebSocket transport requires at least one binding")

    validate_binding_schema_ids(config.bindings)

    for binding_id, binding in config.bindings.items():
        if not binding.enabled:
            continue
        if mode == "server":
            path = (binding.path or "").strip()
            if not path:
                raise ValueError(
                    f"WebSocket transport binding {binding_id!r} requires path in server mode"
                )
            binding.path = path if path.startswith("/") else f"/{path}"
            binding.uri = None
        else:
            uri = (binding.uri or "").strip()
            if not uri:
                raise ValueError(
                    f"WebSocket transport binding {binding_id!r} requires uri in client mode"
                )
            binding.uri = uri
            binding.path = None
    return config


def _config_from_mapping(source: Mapping[str, Any]) -> WebSocketTransportConfig:
    config = WebSocketTransportConfig.model_validate(dict(source))
    return _validate_config(config)


def _resolve_lanes(
    *,
    manifest: ComponentManifest,
    raw_config: Mapping[str, Any],
    instance_id: str,
) -> ResolvedLaneSet:
    del manifest, instance_id
    source = dict(raw_config)
    if not source:
        return ResolvedLaneSet()
    config = _config_from_mapping(source)
    if not config.enabled:
        return ResolvedLaneSet()
    return lanes_for_bindings(config.bindings)


def component_factory(context: ComponentContext):
    source = dict(context.raw_config)
    if not source:
        return InactiveComponent(name=context.runtime_name)

    config = _config_from_mapping(source)
    if not config.enabled:
        return InactiveComponent(name=context.runtime_name)

    transport_id = transport_id_for(
        configured=config.transport_id,
        runtime_name=context.runtime_name,
    )
    bindings = [
        _BindingRuntime(
            binding_id=binding_id,
            config=binding,
            bus=context.require_lane(binding.lane),
            transport_id=transport_id,
        )
        for binding_id, binding in sorted(config.bindings.items())
        if binding.enabled
    ]
    if not bindings:
        return InactiveComponent(name=context.runtime_name)
    return WebSocketTransportComponent(
        runtime_name=context.runtime_name,
        transport_id=transport_id,
        config=config,
        bindings=bindings,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.transports.websocket",
        config_prefix="deckr.transports.websocket",
        cardinality=ComponentCardinality.MULTI_INSTANCE,
    ),
    factory=component_factory,
    resolve_lanes=_resolve_lanes,
)
