from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from deckr.contracts.messages import service_address
from deckr.services import (
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceReplyBody,
    ServiceReplyStatus,
    ServiceSubscriptionMessage,
    ServiceSubscriptionState,
    ServiceUnavailable,
    ServiceViewFamily,
    ServiceViewRef,
    SharedResourceSubscriptionManager,
)


@pytest.mark.asyncio
async def test_shared_resource_subscription_does_not_replay_stale_latest_after_drop() -> None:
    async with anyio.create_task_group() as tg:
        use_calls: list[dict[str, Any]] = []
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        ensure_resources = AsyncMock()
        release_resources = AsyncMock()
        request = AsyncMock(return_value=_ok_reply("play"))

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            use_calls.append({"operations": frozenset(operations), "views": views})
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, _view):
            yield {"volume": 12}
            await anyio.sleep_forever()

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=request,
            ensure_resources=ensure_resources,
            release_resources=release_resources,
        )
        manager = _manager(services)

        first = await manager.open_session({"Kitchen"})
        await _next_state(first, ServiceSubscriptionState.READY)
        await first.drop({"Kitchen"})
        await _wait_until(lambda: release_resources.await_count == 1)

        second = await manager.open_session({"Kitchen"})
        message = await second.messages.receive()

        assert message.state is ServiceSubscriptionState.PENDING
        assert message.payload is None
        assert release_resources.await_args.args[1] == frozenset({"Kitchen"})

        await first.aclose()
        await second.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_replacement_sets_retained_union() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        set_calls: list[frozenset[str]] = []
        set_lease_closed: list[bool] = []
        descriptor = AsyncMock(return_value=_descriptor())

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, view: ServiceViewRef):
            zone = view.key.rsplit("/", 1)[-1]
            yield {"volume": 20 if zone == "Bedroom" else 12}
            await anyio.sleep_forever()

        async def record_set_resources(lease, resources: frozenset[str]) -> None:
            set_calls.append(resources)
            set_lease_closed.append(lease.closed)

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=AsyncMock(return_value=_ok_reply("play")),
            set_resources=AsyncMock(side_effect=record_set_resources),
        )
        manager = _manager(services, replacement=True)

        first = await manager.open_session({"Kitchen"})
        await _next_state(first, ServiceSubscriptionState.READY)
        second = await manager.open_session({"Bedroom"})
        await _next_state(second, ServiceSubscriptionState.READY)

        await _wait_until(
            lambda: set_calls
            == [
                frozenset({"Kitchen"}),
                frozenset({"Bedroom", "Kitchen"}),
            ]
        )

        await first.set(set())
        await _wait_until(lambda: set_calls[-1:] == [frozenset({"Bedroom"})])

        await second.set(set())
        await _wait_until(lambda: set_calls[-1:] == [frozenset()])

        assert set_calls == [
            frozenset({"Kitchen"}),
            frozenset({"Bedroom", "Kitchen"}),
            frozenset({"Bedroom"}),
            frozenset(),
        ]
        assert set_lease_closed == [False, False, False, False]

        await first.aclose()
        await second.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_replacement_reapplies_after_reconnect() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        set_calls: list[frozenset[str]] = []
        descriptor = AsyncMock(return_value=_descriptor())

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(lease, _view):
            if lease.contract.generation == 1:
                yield {"volume": 12}
                raise ServiceUnavailable("contract_cancelled", "cancelled")
            yield {"volume": 13}
            await anyio.sleep_forever()

        async def record_set_resources(_lease, resources: frozenset[str]) -> None:
            set_calls.append(resources)

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=AsyncMock(return_value=_ok_reply("play")),
            set_resources=AsyncMock(side_effect=record_set_resources),
        )
        manager = _manager(services, replacement=True, reconnect_delay_seconds=0)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)
        await _next_state(session, ServiceSubscriptionState.RECONNECTING)
        await _next_state(session, ServiceSubscriptionState.READY)

        assert set_calls == [
            frozenset({"Kitchen"}),
            frozenset({"Kitchen"}),
        ]

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_lease_monitor_reconnects_without_view_change() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        first_refresh = AsyncMock(
            side_effect=ServiceUnavailable("contract_cancelled", "cancelled")
        )

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            if not leases:
                lease.refresh = first_refresh
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(lease, _view):
            yield {"volume": 12 if lease.contract.generation == 1 else 13}
            await anyio.sleep_forever()

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=AsyncMock(return_value=_ok_reply("play")),
            set_resources=AsyncMock(),
        )
        manager = _manager(
            services,
            replacement=True,
            reconnect_delay_seconds=0,
            lease_monitor_interval_seconds=0.01,
        )

        session = await manager.open_session({"Kitchen"})
        first_ready = await _next_state(session, ServiceSubscriptionState.READY)
        reconnecting = await _next_state(session, ServiceSubscriptionState.RECONNECTING)
        second_ready = await _next_state(session, ServiceSubscriptionState.READY)

        assert first_ready.payload == {"volume": 12}
        assert reconnecting.error is not None
        assert reconnecting.error.code == "contract_cancelled"
        assert second_ready.payload == {"volume": 13}
        assert [lease.contract.generation for lease in leases] == [1, 2]

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_active_request_requires_retained_resource() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        request = AsyncMock(return_value=_ok_reply("play"))

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, _view):
            yield {"volume": 12}
            await anyio.sleep_forever()

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=request,
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
        )
        manager = _manager(services)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)

        watched = await manager.request_on_active_lease(
            "play",
            {"zone": "Kitchen"},
            required_resource="Kitchen",
        )
        unwatched = await manager.request_on_active_lease(
            "play",
            {"zone": "Bedroom"},
            required_resource="Bedroom",
        )

        assert watched is not None
        assert watched.status == ServiceReplyStatus.OK
        assert unwatched is None
        request.assert_awaited_once()
        assert request.await_args.args[1] == "play"

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_required_resource_must_be_retained() -> None:
    async with anyio.create_task_group() as tg:
        use_calls: list[dict[str, Any]] = []
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        request = AsyncMock(return_value=_ok_reply("play"))

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            use_calls.append({"operations": frozenset(operations), "views": views})
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, _view):
            yield {"volume": 12}
            await anyio.sleep_forever()

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=request,
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
        )
        manager = _manager(services)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)

        with pytest.raises(ServiceUnavailable) as exc_info:
            await manager.request(
                "play",
                {"zone": "Bedroom"},
                required_resource="Bedroom",
            )

        assert exc_info.value.code == "service_subscription_request_unavailable"
        request.assert_not_awaited()
        assert len(use_calls) == 1

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


def test_shared_resource_subscription_constructor_validation() -> None:
    services = SimpleNamespace()
    kwargs = {
        "services": services,
        "name": "demo-zones",
        "descriptor": AsyncMock(return_value=_descriptor()),
        "ensure_resources": AsyncMock(),
        "release_resources": AsyncMock(),
        "view_for_resource": lambda descriptor, zone: ServiceViewRef(
            "demo_views",
            f"service/{descriptor.service_id}/zones/{zone}",
        ),
        "message_from_view": _message_from_view,
    }

    with pytest.raises(ValueError, match="reconnect_delay_seconds"):
        SharedResourceSubscriptionManager(**kwargs, reconnect_delay_seconds=-0.01)
    with pytest.raises(ValueError, match="lease_monitor_interval_seconds"):
        SharedResourceSubscriptionManager(
            **kwargs,
            lease_monitor_interval_seconds=0,
        )
    with pytest.raises(ValueError, match="subscriber_buffer_size"):
        SharedResourceSubscriptionManager(**kwargs, subscriber_buffer_size=0)
    with pytest.raises(ValueError, match="ensure_resources or set_resources"):
        SharedResourceSubscriptionManager(
            **{
                **kwargs,
                "ensure_resources": None,
                "release_resources": None,
            }
        )
    with pytest.raises(ValueError, match="replacement mode"):
        SharedResourceSubscriptionManager(
            **kwargs,
            set_resources=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_shared_resource_subscription_unknown_and_closed_session_noops() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, _view):
            yield {"volume": 12}
            await anyio.sleep_forever()

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=AsyncMock(return_value=_descriptor()),
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
            watch_view=watch_view,
            use=use,
            request=AsyncMock(return_value=_ok_reply("play")),
        )
        manager = _manager(services)

        await manager.ensure("missing", {"Kitchen"})
        await manager.drop("missing", {"Kitchen"})
        await manager.set("missing", {"Kitchen"})
        await manager.close_session("missing")

        session = await manager.open_session({"Kitchen"})
        session_id = session._session_id  # noqa: SLF001
        await _next_state(session, ServiceSubscriptionState.READY)
        await session.aclose()
        await _wait_until(lambda: services.release_resources.await_count == 1)
        ensure_count = services.ensure_resources.await_count
        release_count = services.release_resources.await_count

        await manager.ensure(session_id, {"Kitchen"})
        await manager.drop(session_id, {"Kitchen"})
        await manager.set(session_id, {"Kitchen"})

        assert services.ensure_resources.await_count == ensure_count
        assert services.release_resources.await_count == release_count
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_nonterminal_unavailable_does_not_reconnect() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())

        @asynccontextmanager
        async def use(
            service_descriptor: ServiceDescriptor,
            *,
            operations=(),
            views=(),
            timeout_seconds=None,
        ):
            del operations, views, timeout_seconds
            lease = _lease(service_descriptor, generation=len(leases) + 1)
            leases.append(lease)
            try:
                yield lease
            finally:
                lease.closed = True

        async def watch_view(_lease, _view):
            yield {"volume": 12}
            raise ServiceUnavailable("service_backend_unavailable", "backend down")

        services = SimpleNamespace(
            _task_group=tg,
            descriptor=descriptor,
            use=use,
            watch_view=watch_view,
            request=AsyncMock(return_value=_ok_reply("play")),
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
        )
        manager = _manager(services, reconnect_delay_seconds=0)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)
        unavailable = await _next_state(session, ServiceSubscriptionState.UNAVAILABLE)
        await anyio.sleep(0)

        assert unavailable.error is not None
        assert unavailable.error.code == "service_backend_unavailable"
        assert descriptor.await_count == 1

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()
@pytest.mark.asyncio
async def test_shared_resource_subscription_prunes_closed_but_not_full_subscribers() -> None:
    services = SimpleNamespace(
        _task_group=SimpleNamespace(start_soon=lambda *args, **kwargs: None),
        descriptor=AsyncMock(return_value=_descriptor()),
        ensure_resources=AsyncMock(),
        release_resources=AsyncMock(),
        watch_view=lambda _lease, _view: None,
        use=lambda *args, **kwargs: None,
    )
    manager = _manager(services, subscriber_buffer_size=1)

    full = await manager.open_session({"Kitchen"})
    await manager._emit(  # noqa: SLF001
        "Kitchen",
        ServiceSubscriptionMessage(
            resource="Kitchen",
            state=ServiceSubscriptionState.READY,
            payload={"volume": 12},
        ),
    )

    assert (await full.messages.receive()).state is ServiceSubscriptionState.PENDING
    assert full._session_id in manager._subscribers  # noqa: SLF001

    closed = await manager.open_session({"Bedroom"})
    await closed.messages.aclose()
    await manager._emit(  # noqa: SLF001
        "Bedroom",
        ServiceSubscriptionMessage(
            resource="Bedroom",
            state=ServiceSubscriptionState.READY,
            payload={"volume": 20},
        ),
    )

    assert closed._session_id not in manager._subscribers  # noqa: SLF001

    await full.aclose()
    await closed.aclose()


async def _next_state(
    session,
    state: ServiceSubscriptionState,
) -> ServiceSubscriptionMessage[str]:
    with anyio.fail_after(1):
        while True:
            message = await session.messages.receive()
            if message.state is state:
                return message


async def _wait_until(predicate) -> None:
    with anyio.fail_after(1):
        while not predicate():
            await anyio.sleep(0)


def _manager(
    services: Any,
    *,
    replacement: bool = False,
    reconnect_delay_seconds: float = 0.01,
    lease_monitor_interval_seconds: float = 1.0,
    subscriber_buffer_size: int = 100,
) -> SharedResourceSubscriptionManager[str]:
    return SharedResourceSubscriptionManager(
        services,
        name="demo-zones",
        descriptor=services.descriptor,
        ensure_resources=None if replacement else services.ensure_resources,
        release_resources=None if replacement else services.release_resources,
        view_for_resource=lambda descriptor, zone: ServiceViewRef(
            "demo_views",
            f"service/{descriptor.service_id}/zones/{zone}",
        ),
        message_from_view=_message_from_view,
        set_resources=services.set_resources if replacement else None,
        service_use_timeout_seconds=1.0,
        reconnect_delay_seconds=reconnect_delay_seconds,
        lease_monitor_interval_seconds=lease_monitor_interval_seconds,
        subscriber_buffer_size=subscriber_buffer_size,
    )


def _message_from_view(
    resource: str,
    payload: Mapping[str, Any] | None,
) -> ServiceSubscriptionMessage[str]:
    if payload is None:
        return ServiceSubscriptionMessage(
            resource=resource,
            state=ServiceSubscriptionState.UNAVAILABLE,
        )
    return ServiceSubscriptionMessage(
        resource=resource,
        state=ServiceSubscriptionState.READY,
        payload=payload,
    )


def _lease(
    descriptor: ServiceDescriptor,
    *,
    generation: int,
) -> Any:
    return SimpleNamespace(
        descriptor=descriptor,
        contract=SimpleNamespace(
            contract_id=f"contract-{generation}",
            generation=generation,
        ),
        closed=False,
        refresh=AsyncMock(),
    )


def _ok_reply(operation: str) -> ServiceReplyBody:
    return ServiceReplyBody(
        serviceNamespace="dev.deckr.demo.service",
        operation=operation,
        status=ServiceReplyStatus.OK,
        result={"ok": True},
    )


def _descriptor() -> ServiceDescriptor:
    return ServiceDescriptor(
        candidate=None,
        service_id="demo-home",
        namespace="dev.deckr.demo.service",
        endpoint=service_address("demo-home"),
        session_id="service-session",
        advertisement_profile="dev.deckr.demo.advertisement.v1",
        use_profile="dev.deckr.demo.use.v1",
        supported_operations=frozenset(
            {"retainResources", "releaseResources", "setZoneScope", "play"}
        ),
        views={
            "zones": ServiceViewFamily(
                storeName="demo_views",
                keyPrefix="service/demo-home/zones/",
            ),
        },
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={},
    )
