from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import anyio
from anyio import get_cancelled_exc_class

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
    TransportDirection,
    _StrictConfigModel,
    lanes_for_bindings,
    transport_id_for,
)
from deckr.transports._lanes import build_lane_handler

logger = logging.getLogger(__name__)

QOS = 2
TRANSPORT_KIND = "mqtt"


class MqttTransportEnvelopeError(ValueError):
    """Raised when an MQTT transport envelope is invalid."""


def build_mqtt_envelope(
    transport_id: str,
    lane: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    return {
        "transportId": transport_id,
        "lane": lane,
        "message": message,
    }


def parse_mqtt_envelope(payload: Any) -> tuple[str | None, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise MqttTransportEnvelopeError("MQTT transport payload must be a JSON object")

    transport_id = payload.get("transportId")
    if transport_id is not None and not isinstance(transport_id, str):
        raise MqttTransportEnvelopeError(
            "MQTT transport payload 'transportId' must be a string"
        )

    lane = payload.get("lane")
    if not isinstance(lane, str) or not lane.strip():
        raise MqttTransportEnvelopeError(
            "MQTT transport payload 'lane' must be a non-empty string"
        )

    message = payload.get("message")
    if not isinstance(message, dict):
        raise MqttTransportEnvelopeError(
            "MQTT transport payload 'message' must be an object"
        )

    return transport_id, lane, message


class MqttTransportBindingConfig(TransportBindingConfigBase):
    topic: str


class MqttTransportConfig(_StrictConfigModel):
    enabled: bool = True
    transport_id: str | None = None
    hostname: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    bindings: dict[str, MqttTransportBindingConfig]


class _BindingRuntime:
    def __init__(
        self,
        *,
        binding_id: str,
        config: MqttTransportBindingConfig,
        bus: Any,
        transport_id: str,
    ) -> None:
        self.binding_id = binding_id
        self.config = config
        self.bus = bus
        self.transport_id = transport_id
        self.handler = build_lane_handler(
            lane=config.lane,
            transport_kind=TRANSPORT_KIND,
            transport_id=transport_id,
            bus=bus,
        )


class MqttTransportComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        transport_id: str,
        config: MqttTransportConfig,
        bindings: list[_BindingRuntime],
    ) -> None:
        super().__init__(name=runtime_name)
        self._transport_id = transport_id
        self._config = config
        self._bindings = bindings

    async def start(self, ctx: RunContext) -> None:
        for binding in self._bindings:
            ctx.tg.start_soon(
                self._run_binding,
                binding,
                name=f"mqtt_transport:{binding.binding_id}",
            )

    async def _run_binding(self, binding: _BindingRuntime) -> None:
        try:
            import aiomqtt
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "MQTT transport requires aiomqtt. Install deckr[mqtt]."
            ) from exc

        cancelled_exc = get_cancelled_exc_class()
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    self._config.hostname,
                    port=self._config.port,
                    username=self._config.username,
                    password=self._config.password,
                ) as client:
                    if binding.config.direction in {
                        TransportDirection.INGRESS,
                        TransportDirection.BIDIRECTIONAL,
                    }:
                        await client.subscribe(binding.config.topic, qos=QOS)
                    async with anyio.create_task_group() as tg:
                        if binding.config.direction in {
                            TransportDirection.EGRESS,
                            TransportDirection.BIDIRECTIONAL,
                        }:
                            tg.start_soon(self._bus_to_mqtt_loop, client, binding)
                        if binding.config.direction in {
                            TransportDirection.INGRESS,
                            TransportDirection.BIDIRECTIONAL,
                        }:
                            tg.start_soon(self._mqtt_to_bus_loop, client, binding)
            except cancelled_exc:
                raise
            except Exception:
                logger.exception(
                    "MQTT transport binding %s disconnected from %s:%s; retrying in %.1fs",
                    binding.binding_id,
                    self._config.hostname,
                    self._config.port,
                    backoff,
                )
                await anyio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
            else:
                backoff = 1.0

    async def _bus_to_mqtt_loop(self, client, binding: _BindingRuntime) -> None:
        async with binding.bus.subscribe() as stream:
            async for envelope in stream:
                await binding.handler.handle_local_event(
                    envelope,
                    send_remote=lambda payload, target_transport_id: self._publish(
                        client,
                        binding,
                        payload,
                        target_transport_id=target_transport_id,
                    ),
                )

    async def _mqtt_to_bus_loop(self, client, binding: _BindingRuntime) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            if topic != binding.config.topic:
                continue
            try:
                raw = message.payload
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                remote_transport_id, lane, payload = parse_mqtt_envelope(json.loads(raw))
                if remote_transport_id == self._transport_id or lane != binding.config.lane:
                    continue
                await binding.handler.handle_remote_payload(
                    payload,
                    send_remote=lambda response, target_transport_id: self._publish(
                        client,
                        binding,
                        response,
                        target_transport_id=target_transport_id,
                    ),
                    remote_transport_id=remote_transport_id,
                )
            except MqttTransportEnvelopeError:
                logger.warning("Dropped malformed MQTT transport envelope")
            except Exception:
                logger.exception("Error forwarding MQTT message to bus")

    async def _publish(
        self,
        client,
        binding: _BindingRuntime,
        payload: dict[str, Any],
        *,
        target_transport_id: str | None,
    ) -> None:
        del target_transport_id
        envelope = build_mqtt_envelope(
            self._transport_id,
            binding.config.lane,
            payload,
        )
        await client.publish(
            binding.config.topic,
            json.dumps(envelope),
            qos=QOS,
        )

    async def stop(self) -> None:
        return


def _config_from_mapping(source: Mapping[str, Any]) -> MqttTransportConfig:
    return MqttTransportConfig.model_validate(dict(source))


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
    return MqttTransportComponent(
        runtime_name=context.runtime_name,
        transport_id=transport_id,
        config=config,
        bindings=bindings,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.transports.mqtt",
        config_prefix="deckr.transports.mqtt",
        cardinality=ComponentCardinality.MULTI_INSTANCE,
    ),
    factory=component_factory,
    resolve_lanes=_resolve_lanes,
)
