"""Local service descriptor indexes over Beacon discovery events."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

import anyio

from deckr.beacon import Beacon, BeaconFeatureEvent, BeaconFeatureEventType
from deckr.contracts.messages import EndpointAddress, parse_endpoint_address
from deckr.services.runtime import (
    ServiceDescriptor,
    ServiceProtocol,
    newest_service_descriptor,
    parse_service_descriptor,
    service_descriptor_sort_key,
)
from deckr.substrates.nats_kv import KvUnavailable

logger = logging.getLogger(__name__)


class ServiceSelectionPolicy(Protocol):
    def select(
        self,
        descriptors: Collection[ServiceDescriptor],
    ) -> ServiceDescriptor | None: ...


@dataclass(frozen=True, slots=True)
class NewestServiceSelectionPolicy:
    """Select the newest descriptor using the shared service descriptor sort key."""

    def select(
        self,
        descriptors: Collection[ServiceDescriptor],
    ) -> ServiceDescriptor | None:
        return newest_service_descriptor(descriptors)


class ServiceDirectory:
    """Protocol-aware, local service descriptor index backed by Beacon events."""

    def __init__(self, beacon: Beacon, protocol: ServiceProtocol) -> None:
        self._beacon = beacon
        self.protocol = protocol
        self._ready = anyio.Event()
        self._closed = False
        self._started = False
        self._current = False
        self._cancel_scope: anyio.CancelScope | None = None
        self._lock = RLock()
        self._descriptor_by_key: dict[str, ServiceDescriptor] = {}
        self._by_service_id: dict[str, set[str]] = {}
        self._by_namespace: dict[str, set[str]] = {}
        self._by_use_profile: dict[str, set[str]] = {}
        self._by_operation: dict[str, set[str]] = {}
        self._by_view_family: dict[str, set[str]] = {}
        self._by_endpoint: dict[str, set[str]] = {}
        self._by_session: dict[tuple[str, str], set[str]] = {}

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self.start_soon(task_group.start_soon)

    def start_soon(self, start_soon: Callable[..., object] | None) -> None:
        if start_soon is None or self._closed or self._started:
            return
        self._started = True
        start_soon(self._event_loop)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_current(self) -> bool:
        with self._lock:
            return self._ready.is_set() and self._current

    def descriptors(self) -> tuple[ServiceDescriptor, ...]:
        with self._lock:
            return _sorted_descriptors(self._descriptor_by_key.values())

    def match(
        self,
        *,
        protocol: ServiceProtocol | None = None,
        service_id: str | None = None,
        namespace: str | None = None,
        use_profile: str | None = None,
        operations: Collection[str] = (),
        views: Collection[str] = (),
        endpoint: str | EndpointAddress | None = None,
        session_id: str | None = None,
    ) -> tuple[ServiceDescriptor, ...]:
        if protocol is not None and protocol != self.protocol:
            return ()
        with self._lock:
            keys: set[str] = set(self._descriptor_by_key)
            keys = self._intersect_index(keys, self._by_service_id, service_id)
            keys = self._intersect_index(keys, self._by_namespace, namespace)
            keys = self._intersect_index(keys, self._by_use_profile, use_profile)
            endpoint_key = (
                str(parse_endpoint_address(endpoint))
                if endpoint is not None
                else None
            )
            keys = self._intersect_index(keys, self._by_endpoint, endpoint_key)
            if session_id is not None:
                session = _require_text(session_id, field_name="service session id")
                if endpoint_key is None:
                    keys = {
                        key
                        for key in keys
                        if any(
                            key in session_keys
                            and indexed_session_id == session
                            for (
                                _indexed_endpoint,
                                indexed_session_id,
                            ), session_keys in self._by_session.items()
                        )
                    }
                else:
                    keys &= self._by_session.get((endpoint_key, session), set())
            for operation in operations:
                keys &= self._by_operation.get(
                    _require_text(operation, field_name="service operation"),
                    set(),
                )
            for family in views:
                keys &= self._by_view_family.get(
                    _require_text(family, field_name="service view family"),
                    set(),
                )
            return _sorted_descriptors(
                self._descriptor_by_key[key]
                for key in keys
                if key in self._descriptor_by_key
            )

    async def aclose(self) -> None:
        self._closed = True
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()

    async def _event_loop(self) -> None:
        with anyio.CancelScope() as cancel_scope:
            self._cancel_scope = cancel_scope
            while not self._closed:
                try:
                    async with self._beacon.watch(self.protocol.feature_id) as events:
                        self._consume_replayed_events(events)
                        self._mark_current()
                        async for event in events:
                            self._apply_event(event)
                except anyio.get_cancelled_exc_class():
                    raise
                except KvUnavailable:
                    self._mark_stale()
                    await anyio.sleep(1)
                except Exception:
                    self._mark_stale()
                    logger.warning(
                        "Service directory watch failed feature=%s",
                        self.protocol.feature_id,
                        exc_info=True,
                    )
                    await anyio.sleep(1)

    def _consume_replayed_events(
        self,
        events: anyio.abc.ObjectReceiveStream[BeaconFeatureEvent],
    ) -> None:
        while True:
            try:
                event = events.receive_nowait()
            except anyio.WouldBlock:
                return
            except anyio.EndOfStream:
                self._mark_stale()
                return
            self._apply_event(event, mark_current=False)

    def _apply_event(
        self,
        event: BeaconFeatureEvent,
        *,
        mark_current: bool = True,
    ) -> None:
        descriptor = (
            parse_service_descriptor(event.candidate, self.protocol)
            if event.event_type
            in {
                BeaconFeatureEventType.ADVERTISED,
                BeaconFeatureEventType.UPDATED,
            }
            and event.candidate is not None
            else None
        )
        with self._lock:
            self._remove_locked(event.key)
            if descriptor is not None:
                self._add_locked(event.key, descriptor)
            if mark_current:
                self._current = True
                self._ready.set()

    def _mark_current(self) -> None:
        with self._lock:
            self._current = True
            self._ready.set()

    def _mark_stale(self) -> None:
        with self._lock:
            self._current = False
            self._ready.set()

    def _intersect_index(
        self,
        keys: set[str],
        index: Mapping[str, set[str]],
        value: str | None,
    ) -> set[str]:
        if value is None:
            return keys
        return keys & index.get(_require_text(value, field_name="service filter"), set())

    def _add_locked(self, key: str, descriptor: ServiceDescriptor) -> None:
        self._descriptor_by_key[key] = descriptor
        _index(self._by_service_id, descriptor.service_id, key)
        _index(self._by_namespace, descriptor.namespace, key)
        _index(self._by_use_profile, descriptor.use_profile, key)
        _index(self._by_endpoint, str(descriptor.endpoint), key)
        _index_session(self._by_session, str(descriptor.endpoint), descriptor.session_id, key)
        for operation in descriptor.supported_operations:
            _index(self._by_operation, operation, key)
        for family in descriptor.views:
            _index(self._by_view_family, family, key)

    def _remove_locked(self, key: str) -> None:
        descriptor = self._descriptor_by_key.pop(key, None)
        if descriptor is None:
            return
        _unindex(self._by_service_id, descriptor.service_id, key)
        _unindex(self._by_namespace, descriptor.namespace, key)
        _unindex(self._by_use_profile, descriptor.use_profile, key)
        _unindex(self._by_endpoint, str(descriptor.endpoint), key)
        _unindex_session(self._by_session, str(descriptor.endpoint), descriptor.session_id, key)
        for operation in descriptor.supported_operations:
            _unindex(self._by_operation, operation, key)
        for family in descriptor.views:
            _unindex(self._by_view_family, family, key)

class ServiceResolver:
    """Resolve service descriptors from a local service directory."""

    def __init__(
        self,
        directory: ServiceDirectory,
        *,
        policy: ServiceSelectionPolicy | None = None,
    ) -> None:
        self.directory = directory
        self.policy = policy or NewestServiceSelectionPolicy()

    def match(
        self,
        *,
        protocol: ServiceProtocol | None = None,
        service_id: str | None = None,
        namespace: str | None = None,
        use_profile: str | None = None,
        operations: Collection[str] = (),
        views: Collection[str] = (),
        endpoint: str | EndpointAddress | None = None,
        session_id: str | None = None,
    ) -> tuple[ServiceDescriptor, ...]:
        return self.directory.match(
            protocol=protocol,
            service_id=service_id,
            namespace=namespace,
            use_profile=use_profile,
            operations=operations,
            views=views,
            endpoint=endpoint,
            session_id=session_id,
        )

    def resolve(
        self,
        *,
        protocol: ServiceProtocol | None = None,
        service_id: str | None = None,
        namespace: str | None = None,
        use_profile: str | None = None,
        operations: Collection[str] = (),
        views: Collection[str] = (),
        endpoint: str | EndpointAddress | None = None,
        session_id: str | None = None,
    ) -> ServiceDescriptor | None:
        return self.policy.select(
            self.match(
                protocol=protocol,
                service_id=service_id,
                namespace=namespace,
                use_profile=use_profile,
                operations=operations,
                views=views,
                endpoint=endpoint,
                session_id=session_id,
            )
        )


def _index(index: dict[str, set[str]], value: str, key: str) -> None:
    index.setdefault(value, set()).add(key)


def _unindex(index: dict[str, set[str]], value: str, key: str) -> None:
    keys = index.get(value)
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        index.pop(value, None)


def _index_session(
    index: dict[tuple[str, str], set[str]],
    endpoint: str,
    session_id: str,
    key: str,
) -> None:
    index.setdefault((endpoint, session_id), set()).add(key)


def _unindex_session(
    index: dict[tuple[str, str], set[str]],
    endpoint: str,
    session_id: str,
    key: str,
) -> None:
    keys = index.get((endpoint, session_id))
    if keys is None:
        return
    keys.discard(key)
    if not keys:
        index.pop((endpoint, session_id), None)


def _sorted_descriptors(
    descriptors: Collection[ServiceDescriptor],
) -> tuple[ServiceDescriptor, ...]:
    return tuple(sorted(descriptors, key=service_descriptor_sort_key))


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
