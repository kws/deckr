from __future__ import annotations

import logging
from datetime import UTC, datetime

import anyio
import pytest
from deckr.actions.endpoints import (
    action_provider_address,
    action_providers_broadcast,
)
from deckr.actions.messages import action_message
from deckr.actions.state import (
    action_provider_catalog_key,
    parse_action_provider_catalog_key,
)
from deckr.contracts.lanes import DEFAULT_LANE_CONTRACT_REGISTRY
from deckr.contracts.messages import (
    ACTIONS_LANE,
    DeckrMessage,
    controller_address,
    endpoint_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
)
from deckr.contracts.nats import lane_message_headers, lane_message_subject
from deckr.state import (
    DEFAULT_DISCOVERY_STATE_STORE_NAME,
    EndpointPresence,
    HardwareInventory,
    decode_key_token,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)
from memory_lane_substrate import MemoryLaneSubstrate, memory_deckr

from deckr_python_runtime.lanes import EndpointRegistrationConflict, EndpointSessionLost
from deckr_python_runtime.runtime import Deckr
from deckr_python_runtime.state import (
    PERSISTENT_STATE_STORE_POLICY,
    StateConflict,
    StateEntry,
    StateUnavailable,
    observe_prefix_current,
)
from deckr_python_runtime.substrates.nats import (
    NatsStateStore,
    NatsSubstrate,
)


def _settings_target() -> dict[str, str]:
    return {
        "scope": "action_instance",
        "controllerId": "main",
        "configId": "device-config",
        "providerInstanceId": "demo-provider",
        "providerId": "demo.provider",
        "actionId": "demo.action",
        "actionInstanceId": "instance-a",
    }


async def _receive(stream):
    with anyio.fail_after(1):
        return await stream.receive()


@pytest.mark.asyncio
async def test_endpoint_send_stamps_sender_and_filters_direct_recipient() -> None:
    async with (
        memory_deckr() as deckr, deckr.lane("actions").register_endpoint(
            action_provider_address("python")
        ) as provider,
        deckr.lane("actions").register_endpoint(
            controller_address("main")
        ) as controller,
        deckr.lane("actions").register_endpoint(
            controller_address("other")
        ) as other,
        controller.subscribe() as controller_stream,
        other.subscribe() as other_stream,
    ):
        sent = await provider.send(
            recipient=controller_address("main"),
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        received = await _receive(controller_stream)
        with anyio.move_on_after(0.05) as scope:
            await other_stream.receive()

    assert received == sent
    assert received.sender == action_provider_address("python")
    assert received.sender_session_id == provider.session_id
    assert scope.cancel_called


@pytest.mark.asyncio
async def test_broadcast_delivery_is_filtered_by_target_family() -> None:
    async with (
        memory_deckr() as deckr, deckr.lane("actions").register_endpoint(
            controller_address("main")
        ) as controller,
        deckr.lane("actions").register_endpoint(
            action_provider_address("a")
        ) as provider_a,
        deckr.lane("actions").register_endpoint(
            action_provider_address("b")
        ) as provider_b,
        deckr.lane("actions").register_endpoint(
            controller_address("other")
        ) as controller_listener,
        provider_a.subscribe() as stream_a,
        provider_b.subscribe() as stream_b,
        controller_listener.subscribe() as controller_stream,
    ):
        sent = await controller.send(
            recipient=action_providers_broadcast(),
            subject=entity_subject("page", contextId="ctx"),
            message_type="actionExtension",
            body={
                "extensionType": "test.broadcast",
                "extensionSchemaId": "test.broadcast.v1",
                "data": {"title": "Ready"},
            },
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
    async with (
        memory_deckr() as deckr,
        deckr.lane("actions").register_endpoint(
            hardware_manager_address("x")
        ) as worker,
    ):
        with pytest.raises(ValueError, match="Sender family"):
            await worker.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )


@pytest.mark.asyncio
async def test_endpoint_request_uses_deckr_correlation() -> None:
    async with memory_deckr() as deckr:
        ready = anyio.Event()

        async def responder(controller) -> None:
            async with controller.subscribe() as stream:
                ready.set()
                request = await _receive(stream)
                await controller.reply_to(
                    request,
                    message_type="settingsSnapshot",
                    body={"target": _settings_target(), "settings": {"theme": "dark"}},
                )

        async with (
            deckr.lane("actions").register_endpoint(
                action_provider_address("python")
            ) as provider,
            deckr.lane("actions").register_endpoint(
                controller_address("main")
            ) as controller,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(responder, controller)
            await ready.wait()
            reply = await provider.request(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )
            tg.cancel_scope.cancel()

    assert reply.message_type == "settingsSnapshot"
    assert reply.in_reply_to is not None
    assert reply.recipient_session_id == provider.session_id


@pytest.mark.asyncio
async def test_endpoint_publish_accepts_prebuilt_message_from_bound_sender() -> None:
    async with (
        memory_deckr() as deckr, deckr.lane("actions").register_endpoint(
            action_provider_address("python")
        ) as provider,
        deckr.lane("actions").register_endpoint(
            controller_address("main")
        ) as controller,
    ):
        message = action_message(
            sender=provider.endpoint,
            sender_session_id=provider.session_id,
            recipient=controller.endpoint,
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        async with controller.subscribe() as stream:
            await provider.publish(message)
            received = await _receive(stream)

        with pytest.raises(ValueError, match="does not match bound endpoint"):
            await controller.publish(message)

    assert received == message


@pytest.mark.asyncio
async def test_register_endpoint_creates_presence_and_withdraws_on_exit() -> None:
    async with memory_deckr() as deckr:
        state = deckr.state()
        key = presence_endpoint_key(
            lane=ACTIONS_LANE,
            endpoint=action_provider_address("python"),
        )

        async with deckr.lane(ACTIONS_LANE).register_endpoint(
            action_provider_address("python"),
            metadata={"runtime": "test-provider"},
        ) as provider:
            entry = await state.get(key)
            assert entry is not None
            presence = EndpointPresence.model_validate(entry.value)
            assert presence.endpoint == provider.endpoint
            assert presence.lane == ACTIONS_LANE
            assert presence.session_id == provider.session_id
            assert presence.metadata["runtime"] == "test-provider"

        assert await state.get(key) is None


@pytest.mark.asyncio
async def test_register_endpoint_rejects_local_duplicate() -> None:
    async with memory_deckr() as deckr:
        lane = deckr.lane(ACTIONS_LANE)
        async with lane.register_endpoint(action_provider_address("python")):
            with pytest.raises(EndpointRegistrationConflict):
                async with lane.register_endpoint(action_provider_address("python")):
                    pass


@pytest.mark.asyncio
async def test_register_endpoint_closes_inside_later_cancel_scope() -> None:
    async with memory_deckr() as deckr:
        endpoint_cm = deckr.lane(ACTIONS_LANE).register_endpoint(
            controller_address("main")
        )
        await endpoint_cm.__aenter__()

        with anyio.CancelScope(shield=True):
            await endpoint_cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_register_endpoint_rejects_existing_distributed_presence() -> None:
    substrate = MemoryLaneSubstrate(lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY)
    async with (
        Deckr(substrate=substrate) as deckr_a,
        Deckr(substrate=substrate) as deckr_b,
        deckr_a.lane(ACTIONS_LANE).register_endpoint(action_provider_address("python")),
    ):
        with pytest.raises(EndpointRegistrationConflict):
            async with deckr_b.lane(ACTIONS_LANE).register_endpoint(
                action_provider_address("python")
            ):
                pass


@pytest.mark.asyncio
async def test_endpoint_renewal_refreshes_same_session_with_revision_guard() -> None:
    async with memory_deckr() as deckr:
        state = deckr.state()
        key = presence_endpoint_key(
            lane=ACTIONS_LANE,
            endpoint=action_provider_address("python"),
        )
        async with deckr.lane(ACTIONS_LANE).register_endpoint(
            action_provider_address("python")
        ) as provider:
            before = await state.get(key)
            assert before is not None
            await provider.renew()
            after = await state.get(key)

        assert after is not None
        assert after.revision > before.revision
        presence = EndpointPresence.model_validate(after.value)
        assert presence.session_id == provider.session_id


@pytest.mark.asyncio
async def test_endpoint_session_loss_is_terminal_after_stale_presence() -> None:
    async with memory_deckr() as deckr:
        state = deckr.state()
        key = presence_endpoint_key(
            lane=ACTIONS_LANE,
            endpoint=action_provider_address("python"),
        )
        async with deckr.lane(ACTIONS_LANE).register_endpoint(
            action_provider_address("python")
        ) as provider:
            entry = await state.get(key)
            assert entry is not None
            await state.delete(key, revision=entry.revision)

            with pytest.raises(EndpointSessionLost):
                await provider.renew()
            with pytest.raises(EndpointSessionLost):
                await provider.send(
                    recipient=controller_address("main"),
                    subject=entity_subject("settings", contextId="ctx"),
                    message_type="settingsRequest",
                    body={"target": _settings_target()},
                )


@pytest.mark.asyncio
async def test_stale_sender_session_is_not_delivered() -> None:
    async with (
        memory_deckr() as deckr, deckr.lane(ACTIONS_LANE).register_endpoint(
            action_provider_address("python")
        ) as provider,
        deckr.lane(ACTIONS_LANE).register_endpoint(
            controller_address("main")
        ) as controller,
        controller.subscribe() as stream,
    ):
        message = DeckrMessage(
            lane=ACTIONS_LANE,
            messageType="settingsRequest",
            sender=provider.endpoint,
            senderSessionId="stale-session",
            recipient=endpoint_target(controller.endpoint),
            subject=entity_subject("settings", contextId="ctx"),
            body={"target": _settings_target()},
        )
        await provider.lane._substrate.publish(message)
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called


@pytest.mark.asyncio
async def test_recipient_session_mismatch_is_not_delivered() -> None:
    async with (
        memory_deckr() as deckr, deckr.lane(ACTIONS_LANE).register_endpoint(
            action_provider_address("python")
        ) as provider,
        deckr.lane(ACTIONS_LANE).register_endpoint(
            controller_address("main")
        ) as controller,
        controller.subscribe() as stream,
    ):
        await provider.send(
            recipient=controller.endpoint,
            recipient_session_id="wrong-session",
            subject=entity_subject("settings", contextId="ctx"),
            message_type="settingsRequest",
            body={"target": _settings_target()},
        )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called


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
    assert fake_js.created_config.ttl == 30.0
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

    assert fake_js.kv is not None
    assert fake_js.kv.keys_filters is None
    assert fake_js.updated_config is not None
    assert fake_js.updated_config.max_age == 30.0
    assert fake_js.updated_config.max_msgs_per_subject == 1
    assert fake_js.updated_config.allow_msg_ttl is True


@pytest.mark.asyncio
async def test_nats_state_creates_discovery_bucket_without_broker_ttl() -> None:
    fake_js = _FakeJs(existing=False)
    store = NatsStateStore(
        name=DEFAULT_DISCOVERY_STATE_STORE_NAME,
        js=fake_js,
        buffer_size=10,
        lease_ttl_seconds=None,
    )

    await store.put("catalog.actions.providers.python", {"provider": "python"})

    assert fake_js.created_config is not None
    assert fake_js.created_config.ttl is None
    assert fake_js.created_config.history == 1
    assert fake_js.updated_config is None


@pytest.mark.asyncio
async def test_nats_state_discovery_bucket_rejects_ttl_writes() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name=DEFAULT_DISCOVERY_STATE_STORE_NAME,
        js=fake_js,
        buffer_size=10,
        lease_ttl_seconds=None,
    )

    with pytest.raises(ValueError, match="persistent current state"):
        await store.put(
            "catalog.actions.providers.python",
            {"provider": "python"},
            ttl=30,
        )


@pytest.mark.asyncio
async def test_nats_substrate_uses_configured_discovery_store_without_ttl() -> None:
    substrate = NatsSubstrate(
        lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY,
        discovery_state_name="deckr_discovery_custom",
    )
    substrate._js = _FakeJs()

    discovery_state = substrate.state("deckr_discovery_custom")

    with pytest.raises(ValueError, match="persistent current state"):
        await discovery_state.put(
            "catalog.actions.providers.python",
            {"provider": "python"},
            ttl=30,
        )


@pytest.mark.asyncio
async def test_nats_substrate_can_open_explicit_persistent_state_bucket() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY)
    substrate._js = _FakeJs(existing=False)

    state = substrate.state(
        "dev_deckr_controller_config_v1",
        policy=PERSISTENT_STATE_STORE_POLICY,
    )
    await state.put(
        "config.controllers.controller-main.materialized",
        {"schema": "test"},
    )

    assert substrate._js.created_config is not None
    assert substrate._js.created_config.ttl is None
    assert substrate._js.updated_config is None


def test_nats_substrate_rejects_same_bucket_with_conflicting_policy() -> None:
    substrate = NatsSubstrate(lane_contracts=DEFAULT_LANE_CONTRACT_REGISTRY)
    substrate._js = _FakeJs()

    substrate.state(
        "dev_deckr_controller_config_v1",
        policy=PERSISTENT_STATE_STORE_POLICY,
    )

    with pytest.raises(ValueError, match="already opened"):
        substrate.state("dev_deckr_controller_config_v1")


@pytest.mark.asyncio
async def test_nats_state_items_reads_prefix_snapshot_without_global_keys() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )

    await store.put("inventory.hardware.elgato-main", {"manager": "elgato"})
    claim = await store.put(
        "claim.device.mirabox-main.deck",
        {"owner": "controller"},
    )

    entries = await store.items("claim.device.")

    assert entries == (claim,)
    assert fake_js.kv.keys_filters is None


@pytest.mark.asyncio
async def test_observe_prefix_current_exact_confirms_omitted_known_keys() -> None:
    state = _PartialState(
        entries={
            "claim.device.main.a": StateEntry(
                key="claim.device.main.a",
                value={"owner": "controller"},
                revision=1,
            ),
            "claim.device.main.b": StateEntry(
                key="claim.device.main.b",
                value={"owner": "controller"},
                revision=2,
            ),
        },
        observed_keys={"claim.device.main.a"},
    )

    observation = await observe_prefix_current(
        state,
        "claim.device.",
        known_keys=("claim.device.main.a", "claim.device.main.b", "claim.device.main.c"),
    )

    assert tuple(entry.key for entry in observation.entries) == (
        "claim.device.main.a",
        "claim.device.main.b",
    )
    assert observation.confirmed_missing == frozenset({"claim.device.main.c"})


@pytest.mark.asyncio
async def test_observe_prefix_current_does_not_guess_when_exact_get_fails() -> None:
    state = _PartialState(
        entries={},
        observed_keys=set(),
        fail_get=StateUnavailable("broker unavailable"),
    )

    with pytest.raises(StateUnavailable):
        await observe_prefix_current(
            state,
            "claim.device.",
            known_keys=("claim.device.main.a",),
        )


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
async def test_nats_state_watch_maps_delete_and_max_age_marker(caplog) -> None:
    caplog.set_level(logging.INFO, logger="deckr_python_runtime.substrates.nats")
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
    assert (
        "NATS Deckr state expire marker bucket=test_state "
        "key=claim.device.main.deck marker_reason=MaxAge"
    ) in caplog.text


@pytest.mark.asyncio
async def test_nats_state_watch_ignores_stale_max_age_marker(caplog) -> None:
    caplog.set_level(logging.INFO, logger="deckr_python_runtime.substrates.nats")
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    key = "claim.device.main.deck"

    async with store.watch("claim.") as changes:
        first = await store.create(key, {"owner": "controller"})
        await _receive(changes)
        await store.update(
            key,
            {"owner": "controller", "refresh": 1},
            revision=first.revision,
        )
        refreshed_change = await _receive(changes)
        await fake_js.kv.publish_stale_max_age_marker(key)
        with anyio.move_on_after(0.05) as scope:
            await changes.receive()

    assert refreshed_change.operation == "put"
    assert refreshed_change.entry is not None
    assert refreshed_change.entry.value["refresh"] == 1
    assert scope.cancel_called
    assert (
        "Ignoring stale NATS Deckr state expire marker bucket=test_state "
        "key=claim.device.main.deck marker_reason=MaxAge"
    ) in caplog.text
    assert "current_revision=2" in caplog.text


@pytest.mark.asyncio
async def test_nats_state_reports_substrate_failures_as_unavailable() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    fake_js.kv.fail_get = RuntimeError("broker unavailable")

    with pytest.raises(StateUnavailable):
        await store.get("claim.device.main.deck")


@pytest.mark.asyncio
async def test_nats_state_create_reports_non_conflict_failures_as_unavailable() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )
    fake_js.kv.fail_create = RuntimeError("broker unavailable")

    with pytest.raises(StateUnavailable):
        await store.create("claim.device.main.deck", {"owner": "controller"})


@pytest.mark.asyncio
async def test_nats_state_delete_missing_key_is_idempotent() -> None:
    fake_js = _FakeJs()
    store = NatsStateStore(
        name="test_state",
        js=fake_js,
        buffer_size=10,
    )

    await store.delete("claim.device.main.missing")


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
    catalog_key = action_provider_catalog_key("provider.main")

    assert parse_presence_endpoint_key(presence_key) == (
        "hardware_messages",
        endpoint_address("hardware_manager", "room/a"),
    )
    assert parse_hardware_inventory_key(inventory_key) == "room/a"
    assert parse_device_claim_key(claim_key) == ("room/a", "deck:one")
    assert parse_action_provider_catalog_key(catalog_key) == "provider.main"


def test_hardware_inventory_labels_round_trip_and_default_empty() -> None:
    inventory = HardwareInventory(
        managerId="mqtt-main",
        managerEndpoint=hardware_manager_address("mqtt-main"),
        sessionId="session-1",
        timestamp=datetime.now(UTC),
        labels={"mqtt-host": "openhabian"},
    )

    dumped = inventory.model_dump(by_alias=True, mode="json")

    assert dumped["labels"] == {"mqtt-host": "openhabian"}
    assert HardwareInventory.model_validate(dumped).labels == {
        "mqtt-host": "openhabian"
    }
    assert HardwareInventory.model_validate(
        {
            "managerId": "mqtt-main",
            "managerEndpoint": "hardware_manager:mqtt-main",
            "sessionId": "session-1",
            "timestamp": "2026-05-06T12:00:00Z",
            "devices": {},
        }
    ).labels == {}


def test_nats_subject_and_headers_are_delivery_hints_for_canonical_envelope() -> None:
    # Build through the public lane API so sender stamping and validation stay covered.
    async def build():
        async with (
            memory_deckr() as deckr, deckr.lane(ACTIONS_LANE).register_endpoint(
                action_provider_address("python")
            ) as provider,
            deckr.lane(ACTIONS_LANE).register_endpoint(
                controller_address("main")
            ) as controller,
            controller.subscribe(),
        ):
            return await provider.send(
                recipient=controller_address("main"),
                subject=entity_subject("settings", contextId="ctx"),
                message_type="settingsRequest",
                body={"target": _settings_target()},
            )

    message = anyio.run(build)
    assert lane_message_subject(message) == "deckr.lane.actions.action_provider.python"
    assert lane_message_headers(message)["Deckr-Message-Id"] == message.message_id
    assert lane_message_headers(message)["Deckr-Sender"] == "action_provider:python"
    assert lane_message_headers(message)["Deckr-Sender-Session"] == message.sender_session_id
    assert lane_message_headers(message)["Deckr-Recipient"] == "controller:main"


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


class _PartialState:
    def __init__(
        self,
        *,
        entries: dict[str, StateEntry],
        observed_keys: set[str],
        fail_get: Exception | None = None,
    ) -> None:
        self._entries = entries
        self._observed_keys = observed_keys
        self._fail_get = fail_get

    async def get(self, key: str) -> StateEntry | None:
        if self._fail_get is not None:
            raise self._fail_get
        return self._entries.get(key)

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]:
        return tuple(
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(prefix) and key in self._observed_keys
        )


class _FakeKv:
    def __init__(self, js: _FakeJs) -> None:
        self._js = js
        self._stream = f"KV_{js.bucket}"
        self._pre = f"$KV.{js.bucket}."
        self._revision = 0
        self._entries: dict[str, _FakeKvEntry] = {}
        self.fail_get: Exception | None = None
        self.fail_create: Exception | None = None
        self.keys_filters: object = None

    async def get(self, key: str) -> _FakeKvEntry:
        if self.fail_get is not None:
            raise self.fail_get
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("missing")
        return entry

    async def keys(self, filters=None):
        self.keys_filters = filters
        return tuple(sorted(self._entries))

    async def watch(self, keys, **kwargs):
        del kwargs
        entries = [
            entry
            for key, entry in sorted(self._entries.items())
            if _subject_matches(keys, key)
        ]
        return _FakeKvWatcher(entries)

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
        if self.fail_create is not None:
            raise self.fail_create
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

    async def publish_stale_max_age_marker(self, key: str) -> None:
        self._revision += 1
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
        self.delivered = 0

    async def consumer_info(self):
        return _FakeConsumerInfo(num_pending=0)

    async def deliver(self, message: _FakeMsg) -> None:
        self.delivered += 1
        await self.callback(message)

    async def unsubscribe(self) -> None:
        self._js.subscriptions.remove(self)


class _FakeKvWatcher:
    def __init__(self, entries: list[_FakeKvEntry]) -> None:
        self._entries = [*entries, None]
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._entries):
            raise StopAsyncIteration
        entry = self._entries[self._index]
        self._index += 1
        return entry

    async def stop(self) -> None:
        self._index = len(self._entries)


class _FakeConsumerInfo:
    def __init__(self, *, num_pending: int) -> None:
        self.num_pending = num_pending


class _FakeMetadataSequence:
    def __init__(self, stream: int) -> None:
        self.stream = stream


class _FakeMetadata:
    def __init__(self, revision: int, *, num_pending: int = 0) -> None:
        self.sequence = _FakeMetadataSequence(revision)
        self.num_pending = num_pending


class _FakeMsg:
    def __init__(
        self,
        *,
        subject: str,
        data: bytes,
        headers: dict[str, str],
        revision: int,
        num_pending: int = 0,
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers
        self.metadata = _FakeMetadata(revision, num_pending=num_pending)


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
    def __init__(self, *, existing: bool = True, max_age: float | None = 30.0) -> None:
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
        subscription = _FakeSubscription(self, subject, cb)
        self.subscriptions.append(subscription)
        deliver_policy = kwargs.get("deliver_policy")
        if getattr(deliver_policy, "value", deliver_policy) == "last_per_subject":
            await self._deliver_last_per_subject(subscription)
        return subscription

    async def _deliver_last_per_subject(self, subscription: _FakeSubscription) -> None:
        if self.kv is None:
            return
        entries = [
            entry
            for key, entry in sorted(self.kv._entries.items())
            if _subject_matches(subscription.subject, f"$KV.{self.bucket}.{key}")
        ]
        total = len(entries)
        for index, entry in enumerate(entries):
            await subscription.deliver(
                _FakeMsg(
                    subject=f"$KV.{self.bucket}.{entry.key}",
                    data=entry.value,
                    headers={},
                    revision=entry.revision,
                    num_pending=total - index - 1,
                )
            )

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
                await subscription.deliver(message)


def _subject_matches(pattern: str, subject: str) -> bool:
    if pattern.endswith(">"):
        return subject.startswith(pattern[:-1])
    return pattern == subject
