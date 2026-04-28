from __future__ import annotations

import anyio
import pytest

from deckr.contracts.messages import (
    controller_address,
    endpoint_address,
    entity_subject,
    host_address,
    plugin_hosts_broadcast,
)
from deckr.pluginhost.messages import plugin_message
from deckr.runtime import Deckr
from deckr.state import (
    StateConflict,
    decode_key_token,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)
from deckr.substrates.nats import NatsStateStore, _headers_for, _subject_for


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


@pytest.mark.asyncio
async def test_endpoint_send_stamps_sender_and_filters_direct_recipient() -> None:
    async with Deckr() as deckr:
        host = deckr.lane("plugin_messages").endpoint(host_address("python"))
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        other = deckr.lane("plugin_messages").endpoint(controller_address("other"))
        async with controller.subscribe() as controller_stream, other.subscribe() as other_stream:
            sent = await host.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )
            received = await _receive(controller_stream)
            with anyio.move_on_after(0.05) as scope:
                await other_stream.receive()

    assert received == sent
    assert received.sender == host_address("python")
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with Deckr() as deckr:
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        host_a = deckr.lane("plugin_messages").endpoint(host_address("a"))
        host_b = deckr.lane("plugin_messages").endpoint(host_address("b"))
        controller_listener = deckr.lane("plugin_messages").endpoint(
            controller_address("other")
        )
        async with (
            host_a.subscribe() as stream_a,
            host_b.subscribe() as stream_b,
            controller_listener.subscribe() as controller_stream,
        ):
            sent = await controller.send(
                recipient=plugin_hosts_broadcast(),
                subject=entity_subject("page", contextId="ctx"),
                message_type="setTitle",
                body={"title": "Ready"},
            )
            received_a = await _receive(stream_a)
            received_b = await _receive(stream_b)
            with anyio.move_on_after(0.05) as scope:
                await controller_stream.receive()

    assert received_a == sent
    assert received_b == sent
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_lane_validation_rejects_wrong_sender_family() -> None:
    async with Deckr() as deckr:
        worker = deckr.lane("plugin_messages").endpoint(endpoint_address("worker", "x"))
        with pytest.raises(ValueError, match="Sender family"):
            await worker.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )


@pytest.mark.asyncio
async def test_endpoint_request_uses_deckr_correlation() -> None:
    async with Deckr() as deckr:
        host = deckr.lane("plugin_messages").endpoint(host_address("python"))
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        ready = anyio.Event()

        async def responder() -> None:
            async with controller.subscribe() as stream:
                ready.set()
                request = await _receive(stream)
                await controller.reply_to(
                    request,
                    message_type="hereAreSettings",
                    body={"settings": {"theme": "dark"}},
                )

        async with anyio.create_task_group() as tg:
            tg.start_soon(responder)
            await ready.wait()
            reply = await host.request(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="requestSettings",
                body={},
            )
            tg.cancel_scope.cancel()

    assert reply.message_type == "hereAreSettings"
    assert reply.in_reply_to is not None


@pytest.mark.asyncio
async def test_endpoint_publish_accepts_prebuilt_message_from_bound_sender() -> None:
    async with Deckr() as deckr:
        host = deckr.lane("plugin_messages").endpoint(host_address("python"))
        controller = deckr.lane("plugin_messages").endpoint(controller_address("main"))
        message = plugin_message(
            sender=host.endpoint,
            recipient=controller.endpoint,
            subject=entity_subject("settings", contextId="ctx"),
            message_type="requestSettings",
            body={},
        )
        async with controller.subscribe() as stream:
            await host.publish(message)
            received = await _receive(stream)

        with pytest.raises(ValueError, match="does not match bound endpoint"):
            await controller.publish(message)

    assert received == message


@pytest.mark.asyncio
async def test_nats_state_creates_bucket_with_broker_lease_ttl() -> None:
    fake_js = _FakeJs(existing=False)
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )

    await store.put("claim.device.main.deck", {"owner": "controller"})

    assert fake_js.created_config is not None
    assert fake_js.created_config.ttl == 15.0
    assert fake_js.created_config.history == 1


@pytest.mark.asyncio
async def test_nats_state_updates_existing_bucket_to_broker_lease_ttl() -> None:
    fake_js = _FakeJs(existing=True, max_age=None)
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )

    await store.items("claim.")

    assert fake_js.updated_config is not None
    assert fake_js.updated_config.max_age == 15.0
    assert fake_js.updated_config.max_msgs_per_subject == 1
    assert fake_js.updated_config.allow_msg_ttl is True


@pytest.mark.asyncio
async def test_nats_state_create_reclaims_broker_expired_claim() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    key = "claim.device.main.stale"
    stale = await store.create(key, {"owner": "dead-controller"})

    await fake_js.kv.expire(key)
    created = await store.create(key, {"owner": "new-controller"})

    assert created.value["owner"] == "new-controller"
    assert created.revision > stale.revision


@pytest.mark.asyncio
async def test_nats_state_update_rejects_broker_expired_claim_refresh() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    key = "claim.device.main.stale"
    stale = await store.create(key, {"owner": "dead-controller"})

    await fake_js.kv.expire(key)
    with pytest.raises(StateConflict, match="revision changed"):
        await store.update(key, {"owner": "dead-controller"}, revision=stale.revision)
    assert await store.get(key) is None


@pytest.mark.asyncio
async def test_nats_state_watch_maps_delete_and_max_age_marker() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    key = "claim.device.main.deck"

    async with store.watch("claim.") as changes:
        put = await store.create(key, {"owner": "controller"})
        put_change = await _receive(changes)
        await store.delete(key, revision=put.revision)
        delete_change = await _receive(changes)
        await store.create(key, {"owner": "controller"})
        await _receive(changes)
        await fake_js.kv.expire(key)
        expire_change = await _receive(changes)

    assert put_change.operation == "put"
    assert delete_change.operation == "delete"
    assert expire_change.operation == "expire"
    assert expire_change.key == key
    assert expire_change.entry is None


def test_key_token_encoding_round_trips_nats_safe_and_fallback_tokens() -> None:
    assert encode_key_token("deck_1") == "deck_1"
    assert decode_key_token("deck_1") == "deck_1"
    encoded = encode_key_token("b64_native")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "b64_native"
    encoded = encode_key_token("deck:one")
    assert encoded.startswith("b64_")
    assert decode_key_token(encoded) == "deck:one"


def test_state_key_helpers_round_trip_encoded_tokens() -> None:
    presence_key = presence_endpoint_key(
        lane="hardware_messages",
        endpoint="hardware_manager:room/a",
    )
    inventory_key = hardware_inventory_key("room/a")
    claim_key = device_claim_key(manager_id="room/a", device_id="deck:one")

    assert parse_presence_endpoint_key(presence_key) == (
        "hardware_messages",
        endpoint_address("hardware_manager", "room/a"),
    )
    assert parse_hardware_inventory_key(inventory_key) == "room/a"
    assert parse_device_claim_key(claim_key) == ("room/a", "deck:one")


def test_nats_subject_and_headers_are_delivery_hints_for_canonical_envelope() -> None:
    # Build through the public lane API so sender stamping and validation stay covered.
    async def build():
        async with Deckr() as deckr:
            host = deckr.lane("plugin_messages").endpoint(host_address("python"))
            controller = deckr.lane("plugin_messages").endpoint(
                controller_address("main")
            )
            async with controller.subscribe():
                return await host.send(
                    recipient=controller_address("main"),
                    subject=entity_subject("settings", contextId="ctx"),
                    message_type="requestSettings",
                    body={},
                )

    message = anyio.run(build)
    assert _subject_for(message) == "deckr.lane.plugin_messages.host.python"
    assert _headers_for(message)["Deckr-Message-Id"] == message.message_id
    assert _headers_for(message)["Deckr-Sender"] == "host:python"
    assert _headers_for(message)["Deckr-Recipient"] == "controller:main"


class _FakeKvEntry:
    def __init__(
        self,
        *,
        key: str,
        value: bytes,
        revision: int,
        operation: str = "PUT",
    ) -> None:
        self.key = key
        self.value = value
        self.revision = revision
        self.operation = operation


class _FakeKv:
    def __init__(self, js: _FakeJs) -> None:
        self._js = js
        self._stream = f"KV_{js.bucket}"
        self._pre = f"$KV.{js.bucket}."
        self._revision = 0
        self._entries: dict[str, _FakeKvEntry] = {}

    async def get(self, key: str) -> _FakeKvEntry:
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("missing")
        return entry

    async def keys(self, filters=None):
        del filters
        return tuple(sorted(self._entries))

    async def put(self, key: str, value: bytes) -> int:
        self._revision += 1
        self._entries[key] = _FakeKvEntry(
            key=key,
            value=value,
            revision=self._revision,
        )
        await self._js.publish_state(key, value, revision=self._revision, headers={})
        return self._revision

    async def create(self, key: str, value: bytes, **kwargs) -> int:
        del kwargs
        if key in self._entries:
            raise RuntimeError("exists")
        return await self.put(key, value)

    async def update(self, key: str, value: bytes, *, last: int, **kwargs) -> int:
        del kwargs
        entry = self._entries.get(key)
        if entry is None or entry.revision != last:
            raise RuntimeError("revision changed")
        return await self.put(key, value)

    async def delete(self, key: str, *, last: int | None = None, **kwargs) -> None:
        del kwargs
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("missing")
        if last is not None and entry.revision != last:
            raise RuntimeError("revision changed")
        self._entries.pop(key, None)
        await self._js.publish_state(
            key,
            b"",
            revision=self._revision,
            headers={"KV-Operation": "DEL"},
        )

    async def expire(self, key: str) -> None:
        self._revision += 1
        self._entries.pop(key, None)
        await self._js.publish_state(
            key,
            b"",
            revision=self._revision,
            headers={"Nats-Marker-Reason": "MaxAge"},
        )


class _FakeSubscription:
    def __init__(self, js: _FakeJs, subject: str, callback) -> None:
        self._js = js
        self.subject = subject
        self.callback = callback

    async def unsubscribe(self) -> None:
        self._js.subscriptions.remove(self)


class _FakeMetadataSequence:
    def __init__(self, stream: int) -> None:
        self.stream = stream


class _FakeMetadata:
    def __init__(self, revision: int) -> None:
        self.sequence = _FakeMetadataSequence(revision)


class _FakeMsg:
    def __init__(
        self,
        *,
        subject: str,
        data: bytes,
        headers: dict[str, str],
        revision: int,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers
        self.metadata = _FakeMetadata(revision)


class _FakeStreamConfig:
    def __init__(
        self,
        *,
        name: str,
        max_age: float | None,
        max_msgs_per_subject: int | None = 1,
        allow_msg_ttl: bool | None = True,
    ) -> None:
        self.name = name
        self.max_age = max_age
        self.max_msgs_per_subject = max_msgs_per_subject
        self.allow_msg_ttl = allow_msg_ttl


class _FakeStreamInfo:
    def __init__(self, config: _FakeStreamConfig) -> None:
        self.config = config


class _FakeJs:
    def __init__(self, *, existing: bool = True, max_age: float | None = 15.0) -> None:
        self.bucket = "test_state"
        self.kv = _FakeKv(self) if existing else None
        self.config = _FakeStreamConfig(
            name=f"KV_{self.bucket}",
            max_age=max_age,
        )
        self.created_config = None
        self.updated_config = None
        self.subscriptions: list[_FakeSubscription] = []

    async def key_value(self, name: str) -> _FakeKv:
        self.bucket = name
        if self.kv is None:
            raise RuntimeError("missing")
        return self.kv

    async def create_key_value(self, config=None, **params) -> _FakeKv:
        if config is not None:
            self.created_config = config
            self.bucket = config.bucket
            self.config = _FakeStreamConfig(
                name=f"KV_{config.bucket}",
                max_age=config.ttl,
                max_msgs_per_subject=config.history,
                allow_msg_ttl=True,
            )
        else:
            self.created_config = params
            self.bucket = params["bucket"]
            self.config = _FakeStreamConfig(
                name=f"KV_{self.bucket}",
                max_age=params.get("ttl"),
                max_msgs_per_subject=params.get("history"),
                allow_msg_ttl=True,
            )
        self.kv = _FakeKv(self)
        return self.kv

    async def stream_info(self, name: str) -> _FakeStreamInfo:
        assert name == self.config.name
        return _FakeStreamInfo(self.config)

    async def update_stream(self, config) -> None:
        self.updated_config = config
        self.config = config

    async def subscribe(self, subject: str, *, cb, **kwargs) -> _FakeSubscription:
        del kwargs
        subscription = _FakeSubscription(self, subject, cb)
        self.subscriptions.append(subscription)
        return subscription

    async def publish_state(
        self,
        key: str,
        data: bytes,
        *,
        revision: int,
        headers: dict[str, str],
    ) -> None:
        subject = f"$KV.{self.bucket}.{key}"
        message = _FakeMsg(
            subject=subject,
            data=data,
            headers=headers,
            revision=revision,
        )
        for subscription in tuple(self.subscriptions):
            if _subject_matches(subscription.subject, subject):
                await subscription.callback(message)


def _subject_matches(pattern: str, subject: str) -> bool:
    if pattern.endswith(">"):
        return subject.startswith(pattern[:-1])
    return pattern == subject
