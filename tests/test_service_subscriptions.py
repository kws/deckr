from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

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


@pytest.mark.asyncio
async def test_shared_resource_subscription_reuses_lease_and_retained_union() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        manager = _manager(services)

        first = await manager.open_session({"Kitchen"})
        ready = await _next_state(first, ServiceSubscriptionState.READY)
        assert ready.payload == {"volume": 12}

        second = await manager.open_session({"Kitchen"})
        second_ready = await _next_state(second, ServiceSubscriptionState.READY)
        assert second_ready.payload == {"volume": 12}

        assert len(services.use_calls) == 1
        assert services.ensure_calls == [frozenset({"Kitchen"})]

        await second.aclose()
        await anyio.sleep(0)
        assert services.release_calls == []

        await first.drop({"Kitchen"})
        with anyio.fail_after(1):
            while services.release_calls != [frozenset({"Kitchen"})]:
                await anyio.sleep(0)

        await first.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_missing_view_is_unavailable() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        services.views["Kitchen"] = None
        manager = _manager(services)

        session = await manager.open_session({"Kitchen"})
        message = await _next_state(session, ServiceSubscriptionState.UNAVAILABLE)

        assert message.resource == "Kitchen"
        assert message.payload is None

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_does_not_replay_stale_latest_after_drop() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        manager = _manager(services)

        first = await manager.open_session({"Kitchen"})
        await _next_state(first, ServiceSubscriptionState.READY)
        await first.drop({"Kitchen"})
        with anyio.fail_after(1):
            while services.release_calls != [frozenset({"Kitchen"})]:
                await anyio.sleep(0)

        second = await manager.open_session({"Kitchen"})
        message = await second.messages.receive()

        assert message.state is ServiceSubscriptionState.PENDING
        assert message.payload is None

        await first.aclose()
        await second.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_does_not_replay_stale_latest_after_close() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        manager = _manager(services)

        first = await manager.open_session({"Kitchen"})
        await _next_state(first, ServiceSubscriptionState.READY)
        await first.aclose()

        second = await manager.open_session({"Kitchen"})
        message = await second.messages.receive()

        assert message.state is ServiceSubscriptionState.PENDING
        assert message.payload is None

        await second.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_reconnects_after_lease_loss() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        services.watch_failures = {
            1: ServiceUnavailable("contract_cancelled", "cancelled")
        }
        services.views_by_generation = {
            1: {"Kitchen": {"volume": 12}},
            2: {"Kitchen": {"volume": 13}},
        }
        manager = _manager(services, reconnect_delay_seconds=0)

        session = await manager.open_session({"Kitchen"})
        first = await _next_state(session, ServiceSubscriptionState.READY)
        reconnecting = await _next_state(
            session,
            ServiceSubscriptionState.RECONNECTING,
        )
        second = await _next_state(session, ServiceSubscriptionState.READY)

        assert first.payload == {"volume": 12}
        assert reconnecting.error is not None
        assert second.payload == {"volume": 13}
        assert len(services.use_calls) == 2
        assert services.ensure_calls == [
            frozenset({"Kitchen"}),
            frozenset({"Kitchen"}),
        ]

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_resource_subscription_active_command_requires_retained_resource() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
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
        assert [call["operation"] for call in services.command_calls] == ["play"]

        await session.aclose()
        await manager.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_command_pool_reuses_compatible_lease() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        pool = SharedServiceCommandPool(
            services,
            name="demo",
            descriptor=services.descriptor,
            default_service_use_timeout_seconds=1.0,
        )

        first = await pool.command("play", {"zone": "Kitchen"})
        second = await pool.command("play", {"zone": "Kitchen"})

        assert first.status == ServiceCommandStatus.OK
        assert second.status == ServiceCommandStatus.OK
        assert [call["operations"] for call in services.use_calls] == [
            frozenset({"play"})
        ]
        assert [call["operation"] for call in services.command_calls] == [
            "play",
            "play",
        ]

        await pool.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_shared_command_pool_retries_after_service_use_reply() -> None:
    async with anyio.create_task_group() as tg:
        services = _FakeServices(tg)
        services.command_replies = [
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
            ServiceCommandReplyBody(
                serviceNamespace="dev.deckr.demo.service",
                operation="play",
                status=ServiceCommandStatus.OK,
                result={"ok": True},
            ),
        ]
        pool = SharedServiceCommandPool(
            services,
            name="demo",
            descriptor=services.descriptor,
            default_service_use_timeout_seconds=1.0,
        )

        reply = await pool.command("play", {"zone": "Kitchen"})

        assert reply.status == ServiceCommandStatus.OK
        assert len(services.use_calls) == 2
        assert [entry.closed for entry in services.leases] == [True, False]

        await pool.aclose()
        assert [entry.closed for entry in services.leases] == [True, True]
        tg.cancel_scope.cancel()


async def _next_state(
    session,
    state: ServiceSubscriptionState,
) -> ServiceSubscriptionMessage[str]:
    with anyio.fail_after(1):
        while True:
            message = await session.messages.receive()
            if message.state is state:
                return message


def _manager(
    services: _FakeServices,
    *,
    reconnect_delay_seconds: float = 0.01,
) -> SharedResourceSubscriptionManager[str]:
    return SharedResourceSubscriptionManager(
        services,
        name="demo-zones",
        descriptor=services.descriptor,
        operations={"ensureZones", "releaseZones", "play"},
        views={"zones"},
        ensure_resources=services.ensure_resources,
        release_resources=services.release_resources,
        view_for_resource=lambda descriptor, zone: ServiceViewRef(
            "demo_views",
            f"service/{descriptor.service_id}/zones/{zone}",
        ),
        message_from_view=_message_from_view,
        service_use_timeout_seconds=1.0,
        reconnect_delay_seconds=reconnect_delay_seconds,
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


class _FakeServices:
    def __init__(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group
        self.use_calls: list[dict[str, Any]] = []
        self.command_calls: list[dict[str, Any]] = []
        self.ensure_calls: list[frozenset[str]] = []
        self.release_calls: list[frozenset[str]] = []
        self.leases: list[_FakeLease] = []
        self.views: dict[str, Mapping[str, Any] | None] = {
            "Kitchen": {"volume": 12}
        }
        self.views_by_generation: dict[
            int,
            dict[str, Mapping[str, Any] | None],
        ] = {}
        self.watch_failures: dict[int, ServiceUnavailable] = {}
        self.command_replies: list[ServiceCommandReplyBody] = []

    async def descriptor(self, timeout_seconds: float | None = None) -> ServiceDescriptor:
        del timeout_seconds
        return _descriptor()

    @asynccontextmanager
    async def use(
        self,
        descriptor: ServiceDescriptor,
        *,
        operations: Collection[str] = (),
        views: Collection[str] | Mapping[str, Collection[str]] = (),
        timeout_seconds: float | None = None,
    ):
        del timeout_seconds
        lease = _FakeLease(descriptor=descriptor, generation=len(self.leases) + 1)
        self.leases.append(lease)
        self.use_calls.append(
            {
                "operations": frozenset(operations),
                "views": views,
            }
        )
        try:
            yield lease
        finally:
            lease.closed = True

    async def command(
        self,
        lease,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> ServiceCommandReplyBody:
        del lease, timeout_seconds
        self.command_calls.append(
            {
                "operation": operation,
                "params": dict(params or {}),
            }
        )
        if self.command_replies:
            return self.command_replies.pop(0)
        return ServiceCommandReplyBody(
            serviceNamespace="dev.deckr.demo.service",
            operation=operation,
            status=ServiceCommandStatus.OK,
            result={"ok": True},
        )

    async def watch_view(
        self,
        lease,
        view: ServiceViewRef,
    ):
        zone = view.key.rsplit("/", 1)[-1]
        generation = lease.contract.generation
        generation_views = self.views_by_generation.get(generation, self.views)
        yield generation_views.get(zone)
        failure = self.watch_failures.pop(generation, None)
        if failure is not None:
            raise failure
        await anyio.sleep_forever()

    async def ensure_resources(
        self,
        lease,
        resources: frozenset[str],
    ) -> None:
        del lease
        self.ensure_calls.append(resources)

    async def release_resources(
        self,
        lease,
        resources: frozenset[str],
    ) -> None:
        del lease
        self.release_calls.append(resources)


class _FakeLease:
    def __init__(self, *, descriptor: ServiceDescriptor, generation: int) -> None:
        self.descriptor = descriptor
        self.contract = SimpleNamespace(
            contract_id=f"contract-{generation}",
            generation=generation,
        )
        self.closed = False

    async def refresh(self) -> None:
        return None


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
            {"ensureZones", "releaseZones", "play"}
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
