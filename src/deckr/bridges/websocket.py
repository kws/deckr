from __future__ import annotations

import http
import json
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import anyio
from anyio import get_cancelled_exc_class

from deckr.bridges._common import (
    BridgeBindingConfigBase,
    StrictBridgeModel,
    bridge_id_for,
    lanes_for_bindings,
)
from deckr.bridges._lanes import build_lane_handler
from deckr.core._bridge import (
    BridgeEnvelopeError,
    build_bridge_envelope,
    parse_bridge_envelope,
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

logger = logging.getLogger(__name__)

_SEND_TIMEOUT = 0.25
_UNSUPPORTED_DATA_CLOSE_CODE = 1003


class WebSocketBridgeBindingConfig(BridgeBindingConfigBase):
    path: str | None = None
    uri: str | None = None


class WebSocketBridgeConfig(StrictBridgeModel):
    enabled: bool = True
    bridge_id: str | None = None
    mode: str = "server"
    host: str = "127.0.0.1"
    port: int = 0
    origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allow_no_origin: bool = True
    bindings: dict[str, WebSocketBridgeBindingConfig]


class _BindingRuntime:
    def __init__(
        self,
        *,
        binding_id: str,
        config: WebSocketBridgeBindingConfig,
        bus: Any,
        bridge_id: str,
    ) -> None:
        self.binding_id = binding_id
        self.config = config
        self.bus = bus
        self.bridge_id = bridge_id
        self.handler = build_lane_handler(
            lane=config.lane,
            bridge_id=bridge_id,
            bus=bus,
        )


class WebSocketBridgeComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        bridge_id: str,
        config: WebSocketBridgeConfig,
        bindings: list[_BindingRuntime],
    ) -> None:
        super().__init__(name=runtime_name)
        self._bridge_id = bridge_id
        self._config = config
        self._bindings = bindings
        self._server = None
        self._connections_lock = anyio.Lock()
        self._connections: set[Any] = set()
        self._connection_paths: dict[Any, str] = {}
        self._connection_remote_ids: dict[Any, str | None] = {}

    async def start(self, ctx: RunContext) -> None:
        if self._config.mode == "server":
            ctx.tg.start_soon(self._run_server, name="websocket_bridge_server")
            return
        for binding in self._bindings:
            ctx.tg.start_soon(
                self._run_client_binding,
                binding,
                name=f"websocket_bridge:{binding.binding_id}",
            )

    async def _run_server(self) -> None:
        try:
            from websockets.asyncio.server import serve
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket bridge requires websockets. Install deckr[websocket]."
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
                "WebSocket bridge requires websockets. Install deckr[websocket]."
            ) from exc

        path = self._server_connection_path(websocket)
        if path is None:
            await websocket.close(code=1008, reason="missing request path")
            return

        remote_bridge_id: str | None = None
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
                        remote_bridge_id, lane, payload = parse_bridge_envelope(
                            json.loads(raw)
                        )
                        if remote_bridge_id == self._bridge_id:
                            continue
                        binding = self._binding_for_server_path(path, lane=lane)
                        if binding is None:
                            logger.warning(
                                "Dropped WebSocket message for unknown binding path=%s lane=%s",
                                path,
                                lane,
                            )
                            continue
                        await self._set_connection_remote_id(websocket, remote_bridge_id)
                        await binding.handler.handle_remote_payload(
                            payload,
                            send_remote=lambda response, target_bridge_id, binding=binding: self._send_to_server_connections(
                                binding,
                                response,
                                target_bridge_id=target_bridge_id,
                            ),
                            remote_bridge_id=remote_bridge_id,
                        )
                    except json.JSONDecodeError:
                        logger.warning("Dropped malformed WebSocket JSON message")
                    except BridgeEnvelopeError:
                        logger.warning("Dropped malformed WebSocket bridge envelope")
                    except Exception:
                        logger.exception("Error forwarding WebSocket message to bus")
            except ConnectionClosed:
                return
            finally:
                await self._notify_transport_disconnect(
                    bindings=self._bindings_for_server_path(path),
                    remote_bridge_id=remote_bridge_id,
                )

    async def _run_client_binding(self, binding: _BindingRuntime) -> None:
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket bridge requires websockets. Install deckr[websocket]."
            ) from exc

        uri = binding.config.uri or ""
        cancelled_exc = get_cancelled_exc_class()
        try:
            async for websocket in connect(uri, origin=self._config.origin):
                try:
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(self._client_bus_to_websocket_loop, websocket, binding)
                        await self._client_websocket_to_bus_loop(websocket, binding)
                        tg.cancel_scope.cancel()
                except cancelled_exc:
                    raise
                except ConnectionClosed:
                    logger.info(
                        "WebSocket bridge client disconnected from %s; retrying",
                        uri,
                    )
        except cancelled_exc:
            raise

    async def _client_bus_to_websocket_loop(self, websocket, binding: _BindingRuntime) -> None:
        async with binding.bus.subscribe() as stream:
            async for event in stream:
                await binding.handler.handle_local_event(
                    event,
                    send_remote=lambda payload, target_bridge_id: self._send_to_client(
                        websocket,
                        binding,
                        payload,
                        target_bridge_id=target_bridge_id,
                    ),
                )

    async def _client_websocket_to_bus_loop(self, websocket, binding: _BindingRuntime) -> None:
        remote_bridge_id: str | None = None
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    await websocket.close(
                        code=_UNSUPPORTED_DATA_CLOSE_CODE,
                        reason="Binary frames are not supported",
                    )
                    return

                try:
                    remote_bridge_id, lane, payload = parse_bridge_envelope(
                        json.loads(raw)
                    )
                    if remote_bridge_id == self._bridge_id or lane != binding.config.lane:
                        continue
                    await binding.handler.handle_remote_payload(
                        payload,
                        send_remote=lambda response, target_bridge_id: self._send_to_client(
                            websocket,
                            binding,
                            response,
                            target_bridge_id=target_bridge_id,
                        ),
                        remote_bridge_id=remote_bridge_id,
                    )
                except json.JSONDecodeError:
                    logger.warning("Dropped malformed WebSocket JSON message")
                except BridgeEnvelopeError:
                    logger.warning("Dropped malformed WebSocket bridge envelope")
                except Exception:
                    logger.exception("Error forwarding WebSocket message to bus")
        finally:
            await binding.handler.handle_transport_disconnect(
                remote_bridge_id=remote_bridge_id,
            )

    async def _send_to_client(
        self,
        websocket,
        binding: _BindingRuntime,
        payload: dict[str, Any],
        *,
        target_bridge_id: str | None,
    ) -> None:
        del target_bridge_id
        envelope = build_bridge_envelope(
            self._bridge_id,
            binding.config.lane,
            payload,
        )
        await websocket.send(json.dumps(envelope))

    async def _send_to_server_connections(
        self,
        binding: _BindingRuntime,
        payload: dict[str, Any],
        *,
        target_bridge_id: str | None,
    ) -> None:
        try:
            from websockets.exceptions import ConnectionClosed
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "WebSocket bridge requires websockets. Install deckr[websocket]."
            ) from exc

        envelope = build_bridge_envelope(
            self._bridge_id,
            binding.config.lane,
            payload,
        )
        text = json.dumps(envelope)
        path = binding.config.path or "/ws"
        async with self._connections_lock:
            connections = [
                websocket
                for websocket in self._connections
                if self._connection_paths.get(websocket) == path
                and (
                    target_bridge_id is None
                    or self._connection_remote_ids.get(websocket) == target_bridge_id
                )
            ]

        to_close: list[Any] = []
        for websocket in connections:
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
        return [
            binding for binding in self._bindings if binding.config.path == path
        ]

    def _server_connection_path(self, websocket) -> str | None:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", None)
        if not isinstance(path, str):
            return None
        return urlsplit(path).path

    async def _add_connection(self, websocket, path: str) -> None:
        async with self._connections_lock:
            self._connections.add(websocket)
            self._connection_paths[websocket] = path
            self._connection_remote_ids[websocket] = None

    async def _remove_connection(self, websocket) -> None:
        async with self._connections_lock:
            self._connections.discard(websocket)
            self._connection_paths.pop(websocket, None)
            self._connection_remote_ids.pop(websocket, None)

    async def _set_connection_remote_id(self, websocket, remote_bridge_id: str | None) -> None:
        async with self._connections_lock:
            if websocket in self._connections:
                self._connection_remote_ids[websocket] = remote_bridge_id

    async def _drop_connection(self, websocket) -> None:
        await self._remove_connection(websocket)
        try:
            await websocket.close()
        except Exception:
            pass

    async def _close_all_connections(self) -> None:
        async with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
            self._connection_paths.clear()
            self._connection_remote_ids.clear()
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
        remote_bridge_id: str | None,
    ) -> None:
        for binding in bindings:
            await binding.handler.handle_transport_disconnect(
                remote_bridge_id=remote_bridge_id,
            )


def _validate_config(config: WebSocketBridgeConfig) -> WebSocketBridgeConfig:
    mode = config.mode.strip().lower()
    if mode not in {"client", "server"}:
        raise ValueError("WebSocket bridge mode must be 'client' or 'server'")
    config.mode = mode

    if not config.bindings:
        raise ValueError("WebSocket bridge requires at least one binding")

    for binding_id, binding in config.bindings.items():
        if not binding.enabled:
            continue
        if mode == "server":
            path = (binding.path or "").strip()
            if not path:
                raise ValueError(
                    f"WebSocket bridge binding {binding_id!r} requires path in server mode"
                )
            binding.path = path if path.startswith("/") else f"/{path}"
            binding.uri = None
        else:
            uri = (binding.uri or "").strip()
            if not uri:
                raise ValueError(
                    f"WebSocket bridge binding {binding_id!r} requires uri in client mode"
                )
            binding.uri = uri
            binding.path = None
    return config


def _config_from_mapping(source: Mapping[str, Any]) -> WebSocketBridgeConfig:
    config = WebSocketBridgeConfig.model_validate(dict(source))
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

    bridge_id = bridge_id_for(
        configured=config.bridge_id,
        runtime_name=context.runtime_name,
    )
    bindings = [
        _BindingRuntime(
            binding_id=binding_id,
            config=binding,
            bus=context.require_lane(binding.lane),
            bridge_id=bridge_id,
        )
        for binding_id, binding in sorted(config.bindings.items())
        if binding.enabled
    ]
    if not bindings:
        return InactiveComponent(name=context.runtime_name)
    return WebSocketBridgeComponent(
        runtime_name=context.runtime_name,
        bridge_id=bridge_id,
        config=config,
        bindings=bindings,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.bridges.websocket",
        config_prefix="deckr.bridges.websocket",
        cardinality=ComponentCardinality.MULTI_INSTANCE,
    ),
    factory=component_factory,
    resolve_lanes=_resolve_lanes,
)
