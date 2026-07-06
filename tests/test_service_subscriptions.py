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
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    ServiceDescriptor,
    ServiceError,
    ServiceSubscriptionMessage,
    ServiceSubscriptionState,
    ServiceUnavailable,
    ServiceViewFamily,
    ServiceViewRef,
    SharedResourceSubscriptionManager,
    SharedServiceCommandPool,
)
from deckr.services.subscriptions import _views_key


@pytest.mark.asyncio
async def test_shared_resource_subscription_does_not_replay_stale_latest_after_drop() -> None:
    async with anyio.create_task_group() as tg:
        use_calls: list[dict[str, Any]] = []
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        ensure_resources = AsyncMock()
        release_resources = AsyncMock()
        command = AsyncMock(return_value=_ok_reply("play"))

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
            command=command,
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
            command=AsyncMock(return_value=_ok_reply("play")),
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
            command=AsyncMock(return_value=_ok_reply("play")),
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
async def test_shared_resource_subscription_active_command_requires_retained_resource() -> None:
    async with anyio.create_task_group() as tg:
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        command = AsyncMock(return_value=_ok_reply("play"))

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
            command=command,
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
        )
        manager = _manager(services)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)

        watched = await manager.command_on_active_lease(
            "play",
            {"zone": "Kitchen"},
            required_resource="Kitchen",
        )
        unwatched = await manager.command_on_active_lease(
            "play",
            {"zone": "Bedroom"},
            required_resource="Bedroom",
        )

        assert watched is not None
        assert watched.status == ServiceCommandStatus.OK
        assert unwatched is None
        command.assert_awaited_once()
        assert command.await_args.args[1] == "play"

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_required_resource_avoids_command_pool() -> None:
    async with anyio.create_task_group() as tg:
        use_calls: list[dict[str, Any]] = []
        leases: list[Any] = []
        descriptor = AsyncMock(return_value=_descriptor())
        command = AsyncMock(return_value=_ok_reply("play"))

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
            command=command,
            ensure_resources=AsyncMock(),
            release_resources=AsyncMock(),
        )
        pool = SharedServiceCommandPool(
            services,
            name="demo",
            descriptor=descriptor,
            default_service_use_timeout_seconds=1.0,
        )
        manager = _manager(services, command_pool=pool)

        session = await manager.open_session({"Kitchen"})
        await _next_state(session, ServiceSubscriptionState.READY)

        with pytest.raises(ServiceUnavailable) as exc_info:
            await manager.command(
                "play",
                {"zone": "Bedroom"},
                required_resource="Bedroom",
            )

        assert exc_info.value.code == "service_subscription_command_unavailable"
        command.assert_not_awaited()
        assert len(use_calls) == 1

        await session.aclose()
        await manager.aclose()
        await pool.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_command_pool_reuses_compatible_lease() -> None:
    leases: list[Any] = []
    use_calls: list[dict[str, Any]] = []
    descriptor = AsyncMock(return_value=_descriptor())

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

    async def command(_lease, operation, _params=None, *, timeout_seconds=None):
        del timeout_seconds
        return _ok_reply(operation)

    services = SimpleNamespace(
        descriptor=descriptor,
        use=use,
        command=AsyncMock(side_effect=command),
    )
    pool = SharedServiceCommandPool(
        services,
        name="demo",
        descriptor=descriptor,
        default_service_use_timeout_seconds=1.0,
    )

    first = await pool.command("play", {"zone": "Kitchen"})
    second = await pool.command("play", {"zone": "Kitchen"})

    assert first.status == ServiceCommandStatus.OK
    assert second.status == ServiceCommandStatus.OK
    assert [call["operations"] for call in use_calls] == [frozenset({"play"})]
    assert [await_call.args[1] for await_call in services.command.await_args_list] == [
        "play",
        "play",
    ]

    await pool.aclose()


@pytest.mark.asyncio
async def test_shared_command_pool_retries_after_service_use_reply() -> None:
    leases: list[Any] = []
    descriptor = AsyncMock(return_value=_descriptor())
    command = AsyncMock(
        side_effect=[
            ServiceCommandReplyBody(
                serviceNamespace="dev.deckr.demo.service",
                operation="play",
                status=ServiceCommandStatus.UNAVAILABLE,
                error=ServiceError(
                    code="service_use_contract_invalid",
                    message="ended",
                    diagnostics={"status": "cancelled"},
                ),
            ),
            _ok_reply("play"),
        ]
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
        leases.append(lease)
        try:
            yield lease
        finally:
            lease.closed = True

    services = SimpleNamespace(
        descriptor=descriptor,
        use=use,
        command=command,
    )
    pool = SharedServiceCommandPool(
        services,
        name="demo",
        descriptor=descriptor,
        default_service_use_timeout_seconds=1.0,
    )

    reply = await pool.command("play", {"zone": "Kitchen"})

    assert reply.status == ServiceCommandStatus.OK
    assert descriptor.await_count == 2
    assert [entry.closed for entry in leases] == [True, False]

    await pool.aclose()
    assert [entry.closed for entry in leases] == [True, True]


def test_shared_resource_subscription_constructor_validation() -> None:
    services = SimpleNamespace()
    kwargs = {
        "services": services,
        "name": "demo-zones",
        "descriptor": AsyncMock(return_value=_descriptor()),
        "operations": {"play"},
        "views": {"zones"},
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
            command=AsyncMock(return_value=_ok_reply("play")),
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
            command=AsyncMock(return_value=_ok_reply("play")),
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
async def test_shared_command_pool_closed_path() -> None:
    pool = SharedServiceCommandPool(
        SimpleNamespace(),
        name="demo",
        descriptor=AsyncMock(return_value=_descriptor()),
    )

    await pool.aclose()

    with pytest.raises(ServiceUnavailable) as exc_info:
        await pool.command("play", {"zone": "Kitchen"})

    assert exc_info.value.code == "service_command_pool_closed"


def test_shared_command_pool_views_key_normalizes_strings_and_mappings() -> None:
    assert _views_key("zones") == ("zones",)
    assert _views_key({"zones": "service/demo", "rooms": {"b", "a"}}) == (
        ("rooms", ("a", "b")),
        ("zones", ("service/demo",)),
    )


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
    command_pool: SharedServiceCommandPool | None = None,
    replacement: bool = False,
    reconnect_delay_seconds: float = 0.01,
    subscriber_buffer_size: int = 100,
) -> SharedResourceSubscriptionManager[str]:
    return SharedResourceSubscriptionManager(
        services,
        name="demo-zones",
        descriptor=services.descriptor,
        operations=(
            {"setZoneScope", "play"}
            if replacement
            else {"retainResources", "releaseResources", "play"}
        ),
        views={"zones"},
        ensure_resources=None if replacement else services.ensure_resources,
        release_resources=None if replacement else services.release_resources,
        view_for_resource=lambda descriptor, zone: ServiceViewRef(
            "demo_views",
            f"service/{descriptor.service_id}/zones/{zone}",
        ),
        message_from_view=_message_from_view,
        command_pool=command_pool,
        set_resources=services.set_resources if replacement else None,
        service_use_timeout_seconds=1.0,
        reconnect_delay_seconds=reconnect_delay_seconds,
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


def _ok_reply(operation: str) -> ServiceCommandReplyBody:
    return ServiceCommandReplyBody(
        serviceNamespace="dev.deckr.demo.service",
        operation=operation,
        status=ServiceCommandStatus.OK,
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
