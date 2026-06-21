from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from memory_kv_bucket import MemoryJsonKvBucket

from deckr.actions.endpoints import action_provider_address
from deckr.beacon import Beacon, BeaconAdvertisementSpec
from deckr.contracts.messages import service_address
from deckr.services import (
    ServiceAdvertisementPayload,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceProtocol,
    ServiceUseTerms,
    ServiceViewEntry,
    ServiceViewFamily,
    ServiceViewRef,
    ServiceViewStore,
    UnsupportedServiceScope,
    newest_service_descriptor,
    parse_service_descriptor,
    service_use_terms,
    service_view_key,
    service_view_prefix,
)
from deckr.substrates.nats_kv import KvChange, KvConflict, KvEntry, kv_value


def _protocol(
    service_id: str = "openhab-home",
    *,
    operations: tuple[str, ...] = ("ensureItems", "refreshItem", "sendCommand"),
) -> ServiceProtocol:
    return ServiceProtocol(
        namespace="dev.deckr.openhab.service",
        feature_id="dev.deckr.openhab.feature",
        advertisement_profile="dev.deckr.openhab.service.advertisement.v1",
        use_profile="dev.deckr.openhab.service_use.v1",
        operations=operations,
        view_families={
            "items": ServiceViewFamily(
                storeName="deckr_openhab_service_view_v1",
                keyPrefix=service_view_prefix(service_id, "items"),
            )
        },
    )


def _memory_beacon() -> Beacon:
    return Beacon(MemoryJsonKvBucket(bucket="beacon"))


async def _publish_service_advertisement(
    beacon: Beacon,
    protocol: ServiceProtocol,
    *,
    service_id: str = "openhab-home",
    session_id: str = "service-session",
    advertisement_id: str = "ad-1",
    payload: Mapping[str, Any] | None = None,
    feature_id: str | None = None,
):
    return await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=feature_id or protocol.feature_id,
            endpoint=service_address(service_id),
            session_id=session_id,
            advertisement_id=advertisement_id,
            payload=(
                dict(payload)
                if payload is not None
                else protocol.advertisement_payload(
                    service_id=service_id,
                    session_id=session_id,
                    backend_status=ServiceBackendStatus.AVAILABLE,
                ).to_dict()
            ),
        )
    )


async def _descriptor(
    beacon: Beacon,
    protocol: ServiceProtocol,
) -> ServiceDescriptor:
    candidates = beacon.candidates(protocol.feature_id)
    descriptors = [
        descriptor
        for candidate in candidates
        if (descriptor := parse_service_descriptor(candidate, protocol)) is not None
    ]
    descriptor = newest_service_descriptor(descriptors)
    assert descriptor is not None
    return descriptor


async def _service_view_context():
    beacon = _memory_beacon()
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    descriptor = await _descriptor(beacon, protocol)
    terms = service_use_terms(
        descriptor,
        action_provider_address("provider-main"),
        operations={"ensureItems"},
        views={"items"},
    )
    lease = _FakeServiceUseLease(descriptor=descriptor, terms=terms)
    view_ref = ServiceViewRef(
        "deckr_openhab_service_view_v1",
        service_view_key("openhab-home", "items", "Kitchen Light"),
    )
    return protocol, lease, view_ref


async def _receive_service_change(stream):
    with anyio.fail_after(1):
        return await stream.receive()


async def _assert_no_service_change(stream) -> None:
    received = None
    with anyio.move_on_after(0.05) as scope:
        received = await stream.receive()
    assert scope.cancel_called, f"unexpected service view change: {received!r}"


def test_service_protocol_payload_terms_and_view_keys() -> None:
    protocol = _protocol()
    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={"backend": "ok"},
    )

    assert protocol.feature_id != protocol.namespace
    assert service_view_key("openhab-home", "items", "Kitchen Light") == (
        "views.openhab-home.items.b64_S2l0Y2hlbiBMaWdodA"
    )
    assert payload.to_dict()["backendStatus"] == "available"
    assert payload.to_dict()["serviceUseProfile"] == protocol.use_profile
    assert payload.to_dict()["views"]["items"] == {
        "storeName": "deckr_openhab_service_view_v1",
        "keyPrefix": "views.openhab-home.items.",
    }
    assert ServiceAdvertisementPayload.model_validate(payload.to_dict()) == payload

    terms = ServiceUseTerms(
        profile=protocol.use_profile,
        serviceUseId="service-use:test",
        serviceId="openhab-home",
        serviceEndpoint=service_address("openhab-home"),
        serviceNamespace=protocol.namespace,
        serviceSessionId="service-session",
        clientEndpoint=action_provider_address("provider-main"),
        allowedOperations=("ensureItems",),
        allowedViews={"items": ("views.openhab-home.items.",)},
    )
    assert terms.to_dict()["allowedOperations"] == ["ensureItems"]
    assert terms.to_dict()["allowedViews"] == {
        "items": ["views.openhab-home.items."]
    }


@pytest.mark.asyncio
async def test_parse_service_descriptor_validates_profile_identity() -> None:
    beacon = _memory_beacon()
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    candidate = beacon.candidates(protocol.feature_id)[0]

    descriptor = parse_service_descriptor(candidate, protocol)
    assert descriptor is not None
    assert descriptor.namespace == protocol.namespace
    assert descriptor.endpoint == service_address("openhab-home")

    wrong_feature_protocol = _protocol()
    object.__setattr__(wrong_feature_protocol, "feature_id", protocol.namespace)
    assert parse_service_descriptor(candidate, wrong_feature_protocol) is None

    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
    ).to_dict()
    payload["profile"] = "wrong-profile"
    await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-2",
        payload=payload,
    )
    wrong_profile = [
        item
        for item in beacon.candidates(protocol.feature_id)
        if item.advertisement.advertisement_id == "ad-2"
    ][0]
    assert parse_service_descriptor(wrong_profile, protocol) is None

    payload = protocol.advertisement_payload(
        service_id="openhab-home",
        session_id="service-session",
        backend_status=ServiceBackendStatus.AVAILABLE,
    ).to_dict()
    payload["serviceNamespace"] = "wrong-namespace"
    await _publish_service_advertisement(
        beacon,
        protocol,
        advertisement_id="ad-3",
        payload=payload,
    )
    wrong_namespace = [
        item
        for item in beacon.candidates(protocol.feature_id)
        if item.advertisement.advertisement_id == "ad-3"
    ][0]
    assert parse_service_descriptor(wrong_namespace, protocol) is None


@pytest.mark.asyncio
async def test_service_use_terms_grant_only_requested_scope() -> None:
    beacon = _memory_beacon()
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    descriptor = await _descriptor(beacon, protocol)

    terms = service_use_terms(
        descriptor,
        action_provider_address("provider-main"),
        operations={"ensureItems"},
        views={"items"},
    )

    assert terms.allowed_operations == ("ensureItems",)
    assert terms.allowed_views == {"items": ("views.openhab-home.items.",)}
    assert "sendCommand" not in terms.allowed_operations

    with pytest.raises(UnsupportedServiceScope):
        service_use_terms(
            descriptor,
            action_provider_address("provider-main"),
            operations={"missingOperation"},
        )
    with pytest.raises(UnsupportedServiceScope):
        service_use_terms(
            descriptor,
            action_provider_address("provider-main"),
            views={"missingView"},
        )


@pytest.mark.asyncio
async def test_service_view_store_uses_explicit_lease_scope() -> None:
    beacon = _memory_beacon()
    protocol = _protocol()
    await _publish_service_advertisement(beacon, protocol)
    descriptor = await _descriptor(beacon, protocol)
    terms = service_use_terms(
        descriptor,
        action_provider_address("provider-main"),
        operations={"ensureItems"},
        views={"items"},
    )
    lease = _FakeServiceUseLease(descriptor=descriptor, terms=terms)
    view_store = ServiceViewStore(
        bucket=MemoryJsonKvBucket(bucket="deckr_openhab_service_view_v1")
    )
    view_ref = ServiceViewRef(
        "deckr_openhab_service_view_v1",
        service_view_key("openhab-home", "items", "Kitchen Light"),
    )

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            service_id="openhab-home",
            service_namespace=protocol.namespace,
            session_id="service-session",
        )

        current = await view_store.get(lease, view_ref)
        assert current is not None
        assert isinstance(current, ServiceViewEntry)
        assert current.value["state"] == "ON"

        async with view_store.watch(lease, view_ref) as changes:
            updated = await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                service_id="openhab-home",
                service_namespace=protocol.namespace,
                session_id="service-session",
                revision=created.revision,
            )
            change = await changes.receive()
            assert change.operation == "put"
            assert change.entry == updated

        with pytest.raises(KvConflict):
            await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "STALE"},
                service_id="openhab-home",
                service_namespace=protocol.namespace,
                session_id="service-session",
                revision=created.revision,
            )

        unauthorized = ServiceViewRef(
            "deckr_openhab_service_view_v1",
            "views.openhab-home.items-unrelated.Kitchen",
        )
        with pytest.raises(UnsupportedServiceScope):
            await view_store.get(lease, unauthorized)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_hides_puts_for_different_service_fence() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        async with view_store.watch(lease, view_ref) as changes:
            await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                service_id="other-service",
                service_namespace=protocol.namespace,
                session_id="other-session",
            )
            await _assert_no_service_change(changes)

        assert await view_store.get(lease, view_ref) is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_replacement_with_other_fence_removes_visible_entry() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            service_id="openhab-home",
            service_namespace=protocol.namespace,
            session_id="service-session",
        )

        async with view_store.watch(lease, view_ref) as changes:
            replacement = await view_store.update(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                service_id="other-service",
                service_namespace=protocol.namespace,
                session_id="other-session",
                revision=created.revision,
            )
            change = await _receive_service_change(changes)

            assert change.operation == "delete"
            assert change.key == view_ref.key
            assert change.revision == replacement.revision
            assert change.entry is None
            await _assert_no_service_change(changes)

        assert await view_store.get(lease, view_ref) is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_hides_removals_for_never_visible_fenced_entry() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        async with view_store.watch(lease, view_ref) as changes:
            first_hidden = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                service_id="other-service",
                service_namespace=protocol.namespace,
                session_id="other-session",
            )
            await _assert_no_service_change(changes)

            await view_store.delete(view=view_ref, revision=first_hidden.revision)
            await _assert_no_service_change(changes)

            second_hidden = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                service_id="other-service",
                service_namespace=protocol.namespace,
                session_id="other-session",
            )
            await _assert_no_service_change(changes)

            await raw.expire(view_ref.key)
            with anyio.fail_after(1):
                while view_store._revision_by_key[view_ref.key] <= second_hidden.revision:
                    await anyio.sleep(0)
            await _assert_no_service_change(changes)

        assert await view_store.get(lease, view_ref) is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_recovers_absent_key_after_watch_restart() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = _RecoveringServiceViewBucket(bucket=view_ref.store_name)
    raw.add(
        view_ref.key,
        {
            "item": "Kitchen Light",
            "state": "ON",
            "serviceId": "openhab-home",
            "serviceNamespace": protocol.namespace,
            "sessionId": "service-session",
        },
    )
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        assert await view_store.get(lease, view_ref) is not None

        raw.remove_without_publish(view_ref.key)
        raw.close_current_watch()

        with anyio.fail_after(1):
            while await view_store.get(lease, view_ref) is not None:
                await anyio.sleep(0)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_get_waits_while_materialized_view_stale() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = _RecoveringServiceViewBucket(bucket=view_ref.store_name)
    raw.add(
        view_ref.key,
        {
            "item": "Kitchen Light",
            "state": "ON",
            "serviceId": "openhab-home",
            "serviceNamespace": protocol.namespace,
            "sessionId": "service-session",
        },
    )
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        assert await view_store.get(lease, view_ref) is not None

        raw.pause_next_watch()
        raw.close_current_watch()
        with anyio.fail_after(1):
            await raw.wait_next_watch_paused()

        with anyio.move_on_after(0.05) as scope:
            await view_store.get(lease, view_ref)
        assert scope.cancelled_caught

        raw.remove_without_publish(view_ref.key)
        raw.resume_next_watch()
        with anyio.fail_after(1):
            while await view_store.get(lease, view_ref) is not None:
                await anyio.sleep(0)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_get_rebuilds_generation_stale_cache() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_current()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            service_id="openhab-home",
            service_namespace=protocol.namespace,
            session_id="service-session",
        )
        assert await view_store.get(lease, view_ref) == created

        async with view_store._lock:  # noqa: SLF001
            view_store._entries.clear()  # noqa: SLF001
            view_store._revision_by_key.clear()  # noqa: SLF001
            view_store._bucket_generation = 0  # noqa: SLF001

        assert not view_store.is_current()
        assert await view_store.get(lease, view_ref) == created
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_delete_updates_cache_immediately() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()
        created = await view_store.put(
            view=view_ref,
            payload={"item": "Kitchen Light", "state": "ON"},
            service_id="openhab-home",
            service_namespace=protocol.namespace,
            session_id="service-session",
        )

        async with view_store.watch(lease, view_ref) as changes:
            await view_store.delete(view=view_ref, revision=created.revision)
            change = await _receive_service_change(changes)

        assert change.operation == "delete"
        assert await view_store.get(lease, view_ref) is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_store_forwards_expire_and_delete_events() -> None:
    protocol, lease, view_ref = await _service_view_context()
    raw = MemoryJsonKvBucket(bucket=view_ref.store_name)
    view_store = ServiceViewStore(bucket=raw)

    async with anyio.create_task_group() as tg:
        view_store.start(tg)
        await view_store.wait_ready()

        async with view_store.watch(lease, view_ref) as changes:
            await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "ON"},
                service_id="openhab-home",
                service_namespace=protocol.namespace,
                session_id="service-session",
            )
            await _receive_service_change(changes)
            await raw.expire(view_ref.key)
            expired = await _receive_service_change(changes)

            recreated = await view_store.put(
                view=view_ref,
                payload={"item": "Kitchen Light", "state": "OFF"},
                service_id="openhab-home",
                service_namespace=protocol.namespace,
                session_id="service-session",
            )
            await _receive_service_change(changes)
            await raw.delete(view_ref.key, revision=recreated.revision)
            deleted = await _receive_service_change(changes)

        assert expired.operation == "expire"
        assert deleted.operation == "delete"
        tg.cancel_scope.cancel()


class _FakeServiceUseLease:
    def __init__(self, *, descriptor: ServiceDescriptor, terms: ServiceUseTerms) -> None:
        self.descriptor = descriptor
        self.terms = terms

    async def refresh(self) -> None:
        return None


class _RecoveringServiceViewBucket:
    def __init__(self, *, bucket: str) -> None:
        self.bucket = bucket
        self._revision = 0
        self._entries: dict[str, KvEntry] = {}
        self._close_events: list[anyio.Event] = []
        self._pause_next_watch = False
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

    def add(self, key: str, value: Mapping[str, Any]) -> KvEntry:
        self._revision += 1
        entry = KvEntry(self.bucket, key, kv_value(value), self._revision)
        self._entries[key] = entry
        return entry

    def remove_without_publish(self, key: str) -> None:
        if key in self._entries:
            self._revision += 1
            self._entries.pop(key)

    def close_current_watch(self) -> None:
        self._close_events[-1].set()

    def pause_next_watch(self) -> None:
        self._pause_next_watch = True
        self._watch_paused = anyio.Event()
        self._resume_watch = anyio.Event()

    async def wait_next_watch_paused(self) -> None:
        await self._watch_paused.wait()

    def resume_next_watch(self) -> None:
        self._resume_watch.set()

    async def get(self, key: str) -> KvEntry | None:
        return self._entries.get(key)

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[KvChange | None]]:
        if self._pause_next_watch:
            self._pause_next_watch = False
            self._watch_paused.set()
            await self._resume_watch.wait()
        close_event = anyio.Event()
        self._close_events.append(close_event)
        send, receive = anyio.create_memory_object_stream[KvChange | None](100)
        snapshot = tuple(
            entry for key, entry in sorted(self._entries.items()) if key.startswith(prefix)
        )

        async def run() -> None:
            for entry in snapshot:
                await send.send(
                    KvChange(self.bucket, entry.key, entry.revision, "put", entry)
                )
            await send.send(None)
            await close_event.wait()
            await send.aclose()

        async with send, receive, anyio.create_task_group() as task_group:
            task_group.start_soon(run)
            yield receive
            task_group.cancel_scope.cancel()
