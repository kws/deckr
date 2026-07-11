"""Reusable managed service subscription helpers."""

from __future__ import annotations

import logging
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Hashable,
    Mapping,
)
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Generic, TypeVar

import anyio

from deckr.core.util.anyio import (
    CoalescedStateBroadcaster,
    CoalescedStateSubscription,
)
from deckr.services.messages import ServiceError, ServiceMessageBody
from deckr.services.runtime import (
    ServiceDescriptor,
    ServiceUnavailable,
    ServiceUseLease,
    ServiceViewRef,
    service_message_ends_service_use,
    service_unavailable_ends_service_use,
)

logger = logging.getLogger(__name__)

ResourceT = TypeVar("ResourceT", bound=Hashable)


class ServiceSubscriptionState(StrEnum):
    """Small state vocabulary for managed service subscriptions."""

    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ServiceSubscriptionMessage(Generic[ResourceT]):
    """One resource-state update from a managed service subscription."""

    resource: ResourceT
    state: ServiceSubscriptionState
    payload: Mapping[str, Any] | None = None
    error: ServiceError | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.payload is not None:
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(
            self,
            "diagnostics",
            MappingProxyType(dict(self.diagnostics)),
        )


@dataclass(slots=True)
class _LogicalSubscriber(Generic[ResourceT]):
    resources: set[ResourceT]


class _LogicalMessageStream(Generic[ResourceT]):
    def __init__(
        self,
        manager: SharedResourceSubscriptionManager[ResourceT],
        session_id: str,
        subscription: CoalescedStateSubscription[
            ResourceT,
            tuple[ResourceT, ...],
        ],
        subscription_context: AbstractAsyncContextManager[
            CoalescedStateSubscription[ResourceT, tuple[ResourceT, ...]]
        ],
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._subscription = subscription
        self._subscription_context = subscription_context
        self._pending = list(subscription.initial)
        self._closed = False

    def __aiter__(self) -> _LogicalMessageStream[ResourceT]:
        return self

    async def __anext__(self) -> ServiceSubscriptionMessage[ResourceT]:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def receive(self) -> ServiceSubscriptionMessage[ResourceT]:
        while True:
            while self._pending:
                resource = self._pending.pop(0)
                message = await self._manager._current_session_message(  # noqa: SLF001
                    self._session_id,
                    resource,
                )
                if message is not None:
                    return message
            wakeup = await self._subscription.receive()
            self._pending = list(
                await self._manager._dirty_session_resources(  # noqa: SLF001
                    self._session_id,
                    None if wakeup.resnapshot_required else wakeup.changed,
                )
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._manager.close_session(self._session_id)
        await self._subscription_context.__aexit__(None, None, None)


class ResourceSubscriptionSession(Generic[ResourceT]):
    """Logical subscription handle backed by a shared resource manager."""

    def __init__(
        self,
        manager: SharedResourceSubscriptionManager[ResourceT],
        session_id: str,
        messages: _LogicalMessageStream[ResourceT],
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._messages = messages
        self._closed = False

    @property
    def messages(
        self,
    ) -> _LogicalMessageStream[ResourceT]:
        return self._messages

    async def ensure(self, resources: Collection[ResourceT]) -> None:
        await self._manager.ensure(self._session_id, resources)

    async def set(self, resources: Collection[ResourceT]) -> None:
        await self._manager.set(self._session_id, resources)

    async def drop(self, resources: Collection[ResourceT]) -> None:
        await self._manager.drop(self._session_id, resources)

    async def request(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        required_resource: ResourceT | None = None,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody:
        return await self._manager.request(
            operation,
            params,
            required_resource=required_resource,
            timeout_seconds=timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._messages.aclose()


class SharedResourceSubscriptionManager(Generic[ResourceT]):
    """Share one service-use subscription lease across logical consumers."""

    def __init__(
        self,
        services: Any,
        *,
        name: str,
        descriptor: Callable[[float | None], Awaitable[ServiceDescriptor]],
        ensure_resources: Callable[
            [ServiceUseLease, frozenset[ResourceT]], Awaitable[None]
        ]
        | None,
        release_resources: Callable[
            [ServiceUseLease, frozenset[ResourceT]], Awaitable[None]
        ]
        | None,
        view_for_resource: Callable[[ServiceDescriptor, ResourceT], ServiceViewRef],
        message_from_view: Callable[
            [ResourceT, Mapping[str, Any] | None],
            ServiceSubscriptionMessage[ResourceT],
        ],
        set_resources: Callable[
            [ServiceUseLease, frozenset[ResourceT]], Awaitable[None]
        ]
        | None = None,
        service_use_timeout_seconds: float | None = None,
        reconnect_delay_seconds: float = 0.05,
        lease_monitor_interval_seconds: float = 1.0,
    ) -> None:
        if reconnect_delay_seconds < 0:
            raise ValueError("reconnect_delay_seconds must not be negative")
        if lease_monitor_interval_seconds <= 0:
            raise ValueError("lease_monitor_interval_seconds must be greater than zero")
        if set_resources is None and ensure_resources is None:
            raise ValueError("ensure_resources or set_resources is required")
        if set_resources is not None and (
            ensure_resources is not None or release_resources is not None
        ):
            raise ValueError(
                "set_resources replacement mode cannot be combined with "
                "ensure_resources or release_resources"
            )
        self._services = services
        self._name = name
        self._descriptor = descriptor
        self._ensure_resources = ensure_resources
        self._release_resources = release_resources
        self._set_resources = set_resources
        self._view_for_resource = view_for_resource
        self._message_from_view = message_from_view
        self._service_use_timeout_seconds = service_use_timeout_seconds
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._lease_monitor_interval_seconds = lease_monitor_interval_seconds
        self._state = CoalescedStateBroadcaster[ResourceT](current=True)
        self._lock = self._state.lock
        self._subscribers: dict[str, _LogicalSubscriber[ResourceT]] = {}
        self._latest: dict[ResourceT, ServiceSubscriptionMessage[ResourceT]] = {}
        self._generation = 0
        self._change_event = anyio.Event()
        self._closed = False
        self._next_session_id = 0
        self._runner_started = False
        self._runner_done: anyio.Event | None = None
        self._active_lease: ServiceUseLease | None = None
        self._active_lease_lost: ServiceUnavailable | None = None

    async def open_session(
        self,
        resources: Collection[ResourceT] = (),
    ) -> ResourceSubscriptionSession[ResourceT]:
        async with self._lock:
            self._next_session_id += 1
            session_id = f"{self._name}:{self._next_session_id}"
            requested = set(resources)
            self._subscribers[session_id] = _LogicalSubscriber(
                resources=requested,
            )
            self._ensure_runner_locked()
            self._notify_changed_locked()
        subscription_context = self._state.subscribe(
            lambda _version, _current: self._session_resources_locked(session_id)
        )
        try:
            subscription = await subscription_context.__aenter__()
        except BaseException:
            async with self._lock:
                self._subscribers.pop(session_id, None)
                self._prune_latest_locked()
                self._notify_changed_locked()
            raise
        messages = _LogicalMessageStream(
            self,
            session_id,
            subscription,
            subscription_context,
        )
        return ResourceSubscriptionSession(
            self,
            session_id,
            messages,
        )

    async def set(
        self,
        session_id: str,
        resources: Collection[ResourceT],
    ) -> None:
        requested = set(resources)
        async with self._lock:
            subscriber = self._subscribers.get(session_id)
            if subscriber is None:
                return
            previous = set(subscriber.resources)
            if previous == requested:
                return
            added = requested.difference(previous)
            subscriber.resources = requested
            self._prune_latest_locked()
            self._state.publish_locked(added)
            self._notify_changed_locked()

    async def ensure(
        self,
        session_id: str,
        resources: Collection[ResourceT],
    ) -> None:
        requested = set(resources)
        if not requested:
            return
        async with self._lock:
            subscriber = self._subscribers.get(session_id)
            if subscriber is None:
                return
            added = requested.difference(subscriber.resources)
            subscriber.resources.update(requested)
            self._state.publish_locked(added)
            self._notify_changed_locked()

    async def drop(
        self,
        session_id: str,
        resources: Collection[ResourceT],
    ) -> None:
        requested = set(resources)
        if not requested:
            return
        async with self._lock:
            subscriber = self._subscribers.get(session_id)
            if subscriber is None:
                return
            subscriber.resources.difference_update(requested)
            self._prune_latest_locked()
            self._state.publish_locked(requested)
            self._notify_changed_locked()

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            subscriber = self._subscribers.pop(session_id, None)
            self._prune_latest_locked()
            self._notify_changed_locked()
        del subscriber

    async def request(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        required_resource: ResourceT | None = None,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody:
        reply = await self.request_on_active_lease(
            operation,
            params,
            required_resource=required_resource,
            timeout_seconds=timeout_seconds,
        )
        if reply is not None:
            return reply

        if required_resource is not None:
            raise ServiceUnavailable(
                "service_subscription_request_unavailable",
                "No compatible active subscription lease is available",
                {
                    "operation": operation,
                    "manager": self._name,
                    "resource": repr(required_resource),
                },
            )
        raise ServiceUnavailable(
            "service_subscription_request_unavailable",
            "No compatible active subscription lease is available",
            {"operation": operation, "manager": self._name},
        )

    async def request_on_active_lease(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        required_resource: ResourceT | None = None,
        timeout_seconds: float | None = None,
    ) -> ServiceMessageBody | None:
        lease = await self._active_request_lease(required_resource=required_resource)
        if lease is None:
            return None
        try:
            reply = await self._services.request(
                lease,
                operation,
                params,
                timeout_seconds=timeout_seconds,
            )
        except ServiceUnavailable as exc:
            if service_unavailable_ends_service_use(exc):
                await self._mark_active_lease_lost(lease, exc)
                return None
            raise
        if not service_message_ends_service_use(reply):
            return reply
        await self._mark_active_lease_lost(
            lease,
            _service_unavailable_from_reply(reply),
        )
        return None

    async def aclose(self) -> None:
        # Let cancellation cleanup from resource watcher task groups relinquish
        # any queued state-lock acquisition before closing the manager itself.
        await anyio.sleep(0)
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._subscribers.clear()
            done = self._runner_done
            self._notify_changed_locked()
        if done is not None:
            await done.wait()
        await self._state.aclose()

    async def _active_request_lease(
        self,
        *,
        required_resource: ResourceT | None = None,
    ) -> ServiceUseLease | None:
        async with self._lock:
            if self._closed:
                return None
            if (
                required_resource is not None
                and required_resource not in self._retained_resources_locked()
            ):
                return None
            return self._active_lease

    def _ensure_runner_locked(self) -> None:
        if self._runner_started or self._closed:
            return
        self._runner_started = True
        self._runner_done = anyio.Event()
        self._services._task_group.start_soon(self._run)

    def _notify_changed_locked(self) -> None:
        self._generation += 1
        self._change_event.set()
        self._change_event = anyio.Event()

    async def _run(self) -> None:
        try:
            while True:
                snapshot = await self._wait_for_resources()
                if snapshot is None:
                    return
                generation, resources = snapshot
                await self._emit_state(resources, ServiceSubscriptionState.PENDING)
                try:
                    descriptor = await self._descriptor(
                        self._service_use_timeout_seconds
                    )
                except ServiceUnavailable as exc:
                    await self._emit_error_state(
                        resources,
                        ServiceSubscriptionState.UNAVAILABLE,
                        exc,
                    )
                    await self._sleep_until_change(generation)
                    continue
                try:
                    await self._run_with_descriptor(descriptor)
                except ServiceUnavailable as exc:
                    current = await self._retained_resources()
                    if not current:
                        continue
                    if service_unavailable_ends_service_use(exc):
                        await self._emit_error_state(
                            current,
                            ServiceSubscriptionState.RECONNECTING,
                            exc,
                        )
                    else:
                        await self._emit_error_state(
                            current,
                            ServiceSubscriptionState.UNAVAILABLE,
                            exc,
                        )
                    await self._sleep_until_change(self._generation)
                except Exception as exc:
                    current = await self._retained_resources()
                    if current:
                        await self._emit_exception_state(current, exc)
                    await self._sleep_until_change(self._generation)
        finally:
            async with self._lock:
                self._runner_started = False
                self._active_lease = None
                done = self._runner_done
            if done is not None:
                done.set()

    async def _run_with_descriptor(self, descriptor: ServiceDescriptor) -> None:
        context = self._services.use(
            descriptor,
            timeout_seconds=self._service_use_timeout_seconds,
        )
        lease = await context.__aenter__()
        try:
            async with self._lock:
                self._active_lease = lease
                self._active_lease_lost = None
            resources = await self._retained_resources()
            if not resources:
                return
            await self._apply_retained_resources(lease, resources)
            while resources:
                generation = await self._snapshot_generation()
                await self._watch_until_change(lease, resources, generation)
                async with self._lock:
                    closed = self._closed
                    lease_lost = self._active_lease_lost
                    self._active_lease_lost = None
                if closed:
                    if lease_lost is None:
                        await self._clear_retained_resources(lease, resources)
                    return
                if lease_lost is not None:
                    raise lease_lost
                updated = await self._retained_resources()
                removed = resources.difference(updated)
                added = updated.difference(resources)
                if added:
                    await self._emit_state(added, ServiceSubscriptionState.PENDING)
                await self._apply_retained_resource_change(
                    lease,
                    previous=resources,
                    updated=updated,
                    removed=removed,
                    added=added,
                )
                if not updated:
                    return
                resources = updated
        finally:
            async with self._lock:
                if self._active_lease is lease:
                    self._active_lease = None
                    self._active_lease_lost = None
            await context.__aexit__(None, None, None)

    async def _apply_retained_resources(
        self,
        lease: ServiceUseLease,
        resources: frozenset[ResourceT],
    ) -> None:
        if self._set_resources is not None:
            await self._set_resources(lease, resources)
            return
        assert self._ensure_resources is not None
        await self._ensure_resources(lease, resources)

    async def _apply_retained_resource_change(
        self,
        lease: ServiceUseLease,
        *,
        previous: frozenset[ResourceT],
        updated: frozenset[ResourceT],
        removed: frozenset[ResourceT],
        added: frozenset[ResourceT],
    ) -> None:
        if self._set_resources is not None:
            if updated != previous:
                await self._set_resources(lease, updated)
            return
        if removed and self._release_resources is not None:
            await self._release_resources(lease, removed)
        if added:
            assert self._ensure_resources is not None
            await self._ensure_resources(lease, updated)

    async def _clear_retained_resources(
        self,
        lease: ServiceUseLease,
        resources: frozenset[ResourceT],
    ) -> None:
        if not resources:
            return
        if self._set_resources is not None:
            await self._set_resources(lease, frozenset())
            return
        if self._release_resources is not None:
            await self._release_resources(lease, resources)

    async def _watch_until_change(
        self,
        lease: ServiceUseLease,
        resources: frozenset[ResourceT],
        generation: int,
    ) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._monitor_active_lease, lease)
            for resource in sorted(resources, key=repr):
                tg.start_soon(self._watch_resource, lease, resource)
            await self._wait_for_change_after(generation)
            tg.cancel_scope.cancel()

    async def _monitor_active_lease(self, lease: ServiceUseLease) -> None:
        while True:
            await anyio.sleep(self._lease_monitor_interval_seconds)
            try:
                await lease.refresh()
            except ServiceUnavailable as exc:
                if service_unavailable_ends_service_use(exc):
                    await self._mark_active_lease_lost(lease, exc)
                    return
                logger.debug(
                    "Service subscription lease monitor ignored nonterminal "
                    "refresh failure manager=%s code=%s message=%s",
                    self._name,
                    exc.code,
                    exc.message,
                )

    async def _watch_resource(
        self,
        lease: ServiceUseLease,
        resource: ResourceT,
    ) -> None:
        view = self._view_for_resource(lease.descriptor, resource)
        try:
            async for payload in self._services.watch_view(lease, view):
                await self._emit(resource, self._message_from_view(resource, payload))
        except ServiceUnavailable as exc:
            if service_unavailable_ends_service_use(exc):
                await self._mark_active_lease_lost(lease, exc)
                return
            await self._emit(
                resource,
                _state_message(
                    resource,
                    ServiceSubscriptionState.UNAVAILABLE,
                    error=_service_error_from_unavailable(exc),
                    diagnostics=exc.diagnostics,
                ),
            )
        except Exception as exc:
            await self._emit(
                resource,
                _state_message(
                    resource,
                    ServiceSubscriptionState.ERROR,
                    error=ServiceError(
                        code="service_subscription_watch_failed",
                        message="Service subscription watch failed",
                        diagnostics={"reason": str(exc)},
                    ),
                ),
            )

    async def _mark_active_lease_lost(
        self,
        lease: ServiceUseLease,
        exc: ServiceUnavailable,
    ) -> None:
        async with self._lock:
            if self._active_lease is not lease:
                return
            self._active_lease_lost = exc
            self._change_event.set()

    async def _wait_for_resources(
        self,
    ) -> tuple[int, frozenset[ResourceT]] | None:
        while True:
            async with self._lock:
                if self._closed:
                    return None
                resources = self._retained_resources_locked()
                generation = self._generation
                event = self._change_event
            if resources:
                return generation, resources
            await event.wait()

    async def _wait_for_change_after(self, generation: int) -> None:
        while True:
            async with self._lock:
                if (
                    self._closed
                    or self._generation != generation
                    or self._active_lease_lost is not None
                ):
                    return
                event = self._change_event
            await event.wait()

    async def _sleep_until_change(self, generation: int) -> None:
        if self._reconnect_delay_seconds <= 0:
            return
        with anyio.move_on_after(self._reconnect_delay_seconds):
            await self._wait_for_change_after(generation)

    async def _snapshot_generation(self) -> int:
        async with self._lock:
            return self._generation

    async def _retained_resources(self) -> frozenset[ResourceT]:
        async with self._lock:
            return self._retained_resources_locked()

    def _retained_resources_locked(self) -> frozenset[ResourceT]:
        retained: set[ResourceT] = set()
        for subscriber in self._subscribers.values():
            retained.update(subscriber.resources)
        return frozenset(retained)

    def _session_resources_locked(self, session_id: str) -> tuple[ResourceT, ...]:
        subscriber = self._subscribers.get(session_id)
        if subscriber is None:
            return ()
        return tuple(sorted(subscriber.resources, key=repr))

    async def _dirty_session_resources(
        self,
        session_id: str,
        changed: frozenset[ResourceT] | None,
    ) -> tuple[ResourceT, ...]:
        return await self._state.capture(
            lambda _version, _current: tuple(
                resource
                for resource in self._session_resources_locked(session_id)
                if changed is None or resource in changed
            )
        )

    async def _current_session_message(
        self,
        session_id: str,
        resource: ResourceT,
    ) -> ServiceSubscriptionMessage[ResourceT] | None:
        return await self._state.capture(
            lambda _version, _current: self._current_session_message_locked(
                session_id,
                resource,
            )
        )

    def _current_session_message_locked(
        self,
        session_id: str,
        resource: ResourceT,
    ) -> ServiceSubscriptionMessage[ResourceT] | None:
        subscriber = self._subscribers.get(session_id)
        if subscriber is None or resource not in subscriber.resources:
            return None
        return self._latest.get(resource) or _state_message(
            resource,
            ServiceSubscriptionState.PENDING,
        )

    def _prune_latest_locked(self) -> None:
        retained = self._retained_resources_locked()
        for resource in tuple(self._latest):
            if resource not in retained:
                self._latest.pop(resource, None)

    async def _emit_state(
        self,
        resources: Collection[ResourceT],
        state: ServiceSubscriptionState,
    ) -> None:
        for resource in resources:
            await self._emit(resource, _state_message(resource, state))

    async def _emit_error_state(
        self,
        resources: Collection[ResourceT],
        state: ServiceSubscriptionState,
        exc: ServiceUnavailable,
    ) -> None:
        error = _service_error_from_unavailable(exc)
        for resource in resources:
            await self._emit(
                resource,
                _state_message(
                    resource,
                    state,
                    error=error,
                    diagnostics=exc.diagnostics,
                ),
            )

    async def _emit_exception_state(
        self,
        resources: Collection[ResourceT],
        exc: Exception,
    ) -> None:
        error = ServiceError(
            code="service_subscription_failed",
            message="Service subscription failed",
            diagnostics={"reason": str(exc)},
        )
        for resource in resources:
            await self._emit(
                resource,
                _state_message(resource, ServiceSubscriptionState.ERROR, error=error),
            )

    async def _emit(
        self,
        resource: ResourceT,
        message: ServiceSubscriptionMessage[ResourceT],
    ) -> None:
        async with self._lock:
            if resource not in self._retained_resources_locked():
                return
            self._latest[resource] = message
            self._state.publish_locked((resource,))

def _state_message(
    resource: ResourceT,
    state: ServiceSubscriptionState,
    *,
    error: ServiceError | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> ServiceSubscriptionMessage[ResourceT]:
    return ServiceSubscriptionMessage(
        resource=resource,
        state=state,
        error=error,
        diagnostics=dict(diagnostics or {}),
    )


def _service_error_from_unavailable(exc: ServiceUnavailable) -> ServiceError:
    return ServiceError(
        code=exc.code,
        message=exc.message,
        diagnostics=dict(exc.diagnostics),
    )


def _service_unavailable_from_reply(reply: ServiceMessageBody) -> ServiceUnavailable:
    error = reply.error
    if error is None:
        return ServiceUnavailable(
            "service_use_contract_invalid",
            "Service-use contract ended",
        )
    return ServiceUnavailable(error.code, error.message, dict(error.diagnostics))
