from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import anyio
from anyio import get_cancelled_exc_class

from deckr.bridges._common import (
    BridgeBindingConfigBase,
    BridgeDirection,
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

QOS = 2


class MqttBridgeBindingConfig(BridgeBindingConfigBase):
    topic: str


class MqttBridgeConfig(StrictBridgeModel):
    enabled: bool = True
    bridge_id: str | None = None
    hostname: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    bindings: dict[str, MqttBridgeBindingConfig]


class _BindingRuntime:
    def __init__(
        self,
        *,
        binding_id: str,
        config: MqttBridgeBindingConfig,
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


class MqttBridgeComponent(BaseComponent):
    def __init__(
        self,
        *,
        runtime_name: str,
        bridge_id: str,
        config: MqttBridgeConfig,
        bindings: list[_BindingRuntime],
    ) -> None:
        super().__init__(name=runtime_name)
        self._bridge_id = bridge_id
        self._config = config
        self._bindings = bindings

    async def start(self, ctx: RunContext) -> None:
        for binding in self._bindings:
            ctx.tg.start_soon(
                self._run_binding,
                binding,
                name=f"mqtt_bridge:{binding.binding_id}",
            )

    async def _run_binding(self, binding: _BindingRuntime) -> None:
        try:
            import aiomqtt
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "MQTT bridge requires aiomqtt. Install deckr[mqtt]."
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
                        BridgeDirection.INGRESS,
                        BridgeDirection.BIDIRECTIONAL,
                    }:
                        await client.subscribe(binding.config.topic, qos=QOS)
                    async with anyio.create_task_group() as tg:
                        if binding.config.direction in {
                            BridgeDirection.EGRESS,
                            BridgeDirection.BIDIRECTIONAL,
                        }:
                            tg.start_soon(self._bus_to_mqtt_loop, client, binding)
                        if binding.config.direction in {
                            BridgeDirection.INGRESS,
                            BridgeDirection.BIDIRECTIONAL,
                        }:
                            tg.start_soon(self._mqtt_to_bus_loop, client, binding)
            except cancelled_exc:
                raise
            except Exception:
                logger.exception(
                    "MQTT bridge binding %s disconnected from %s:%s; retrying in %.1fs",
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
            async for event in stream:
                await binding.handler.handle_local_event(
                    event,
                    send_remote=lambda payload, target_bridge_id: self._publish(
                        client,
                        binding,
                        payload,
                        target_bridge_id=target_bridge_id,
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
                remote_bridge_id, lane, payload = parse_bridge_envelope(json.loads(raw))
                if remote_bridge_id == self._bridge_id or lane != binding.config.lane:
                    continue
                await binding.handler.handle_remote_payload(
                    payload,
                    send_remote=lambda response, target_bridge_id: self._publish(
                        client,
                        binding,
                        response,
                        target_bridge_id=target_bridge_id,
                    ),
                    remote_bridge_id=remote_bridge_id,
                )
            except BridgeEnvelopeError:
                logger.warning("Dropped malformed MQTT bridge envelope")
            except Exception:
                logger.exception("Error forwarding MQTT message to bus")

    async def _publish(
        self,
        client,
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
        await client.publish(
            binding.config.topic,
            json.dumps(envelope),
            qos=QOS,
        )

    async def stop(self) -> None:
        return


def _config_from_mapping(source: Mapping[str, Any]) -> MqttBridgeConfig:
    return MqttBridgeConfig.model_validate(dict(source))


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
    return MqttBridgeComponent(
        runtime_name=context.runtime_name,
        bridge_id=bridge_id,
        config=config,
        bindings=bindings,
    )


component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="deckr.bridges.mqtt",
        config_prefix="deckr.bridges.mqtt",
        cardinality=ComponentCardinality.MULTI_INSTANCE,
    ),
    factory=component_factory,
    resolve_lanes=_resolve_lanes,
)
