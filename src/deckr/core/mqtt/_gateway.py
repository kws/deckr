"""Generic MQTT gateway: bridges EventBus and MQTT with configurable serialize/deserialize."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiomqtt
import anyio
from anyio import get_cancelled_exc_class

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

QOS = 2  # Exactly once; used for both publish and subscribe


@dataclass(frozen=True)
class MqttGatewayConfig:
    hostname: str
    port: int
    topic: str
    username: str | None
    password: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.hostname and self.topic)


class MqttGateway(BaseComponent):
    """Bidirectional bridge: EventBus <-> MQTT. Uses serialize/deserialize for message format."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        config: MqttGatewayConfig,
        serialize: Callable[[Any], dict],
        deserialize: Callable[[dict], Any],
        deserialize_from_mqtt: Callable[[dict], Any] | None = None,
        is_event: Callable[[Any], bool] | None = None,
        online_event: Any | None = None,
        offline_event: Any | None = None,
    ) -> None:
        super().__init__(name="mqtt_gateway")
        self._event_bus = event_bus
        self._config = config
        self._serialize = serialize
        self._deserialize = deserialize
        self._deserialize_from_mqtt = deserialize_from_mqtt or deserialize
        self._is_event = is_event if is_event is not None else lambda _: True
        self._gateway_id = str(uuid.uuid4())
        self._ready = anyio.Event()
        self._online_event = online_event
        self._offline_event = offline_event

    async def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait for the gateway's subscriptions/bridge loops to become active.

        Args:
            timeout: Max seconds to wait. If ``None``, wait indefinitely.

        Returns:
            ``True`` when the gateway became ready, ``False`` on timeout.
        """
        if timeout is None:
            await self._ready.wait()
            return True

        with anyio.move_on_after(timeout) as scope:
            await self._ready.wait()
        return not scope.cancel_called

    async def start(self, ctx: RunContext) -> None:
        ctx.tg.start_soon(
            self._run,
            self._config.hostname,
            self._config.port,
            self._config.topic,
            self._config.username,
            self._config.password,
            name="mqtt_gateway_run",
        )
        logger.info(
            "MqttGateway connected to %s:%s, topic %s",
            self._config.hostname,
            self._config.port,
            self._config.topic,
        )

    async def _run(
        self,
        hostname: str,
        port: int,
        topic: str,
        username: str | None,
        password: str | None,
    ) -> None:
        backoff = 1.0
        cancelled_exc = get_cancelled_exc_class()
        while True:
            try:
                will = None
                if self._offline_event is not None:
                    will = aiomqtt.Will(
                        topic,
                        json.dumps(
                            build_bridge_envelope(
                                self._gateway_id, self._serialize(self._offline_event)
                            )
                        ),
                        qos=QOS,
                    )
                async with aiomqtt.Client(
                    hostname,
                    port=port,
                    username=username,
                    password=password,
                    will=will,
                ) as client:
                    await client.subscribe(topic, qos=QOS)
                    if self._online_event is not None:
                        payload = json.dumps(
                            build_bridge_envelope(
                                self._gateway_id, self._serialize(self._online_event)
                            )
                        )
                        await client.publish(topic, payload, qos=QOS)
                    backoff = 1.0
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(self._bus_to_mqtt_loop, client, topic)
                        tg.start_soon(self._mqtt_to_bus_loop, client)
            except cancelled_exc:
                raise
            except Exception:
                logger.exception(
                    "MQTT gateway disconnected from %s:%s; retrying in %.1fs",
                    hostname,
                    port,
                    backoff,
                )
                await anyio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    async def _bus_to_mqtt_loop(self, client: aiomqtt.Client, topic: str) -> None:
        async with self._event_bus.subscribe() as stream:
            self._ready.set()
            async for event in stream:
                if event_originated_from_bridge(event, self._gateway_id):
                    continue
                if not self._is_event(event):
                    continue
                try:
                    msg_dict = self._serialize(event)
                    payload = json.dumps(build_bridge_envelope(self._gateway_id, msg_dict))
                    await client.publish(topic, payload, qos=QOS)
                except Exception:
                    logger.exception("Error publishing to MQTT")
        raise RuntimeError(
            "MQTT gateway event bus subscription closed unexpectedly; reconnecting"
        )

    async def _mqtt_to_bus_loop(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            try:
                raw = message.payload
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                gateway_id, payload = parse_bridge_envelope(json.loads(raw))
                if gateway_id == self._gateway_id:
                    continue
                msg = mark_event_from_bridge(
                    self._deserialize_from_mqtt(payload), self._gateway_id
                )
                await self._event_bus.send(msg)
            except Exception:
                logger.exception("Error forwarding MQTT message to bus")

    async def stop(self) -> None:
        pass  # Task cancellation handles disconnect
