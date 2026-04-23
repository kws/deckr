from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

import anyio

from deckr.core.component import Component, ComponentManager
from deckr.core.config import ConfigDocument

logger = logging.getLogger(__name__)

SERVICE_ENTRYPOINT_GROUP = "deckr.services"


class ServiceNotConfigured(RuntimeError):
    """Raised by discovered services that choose not to self-start."""


@dataclass(frozen=True, slots=True)
class ServiceInstanceSpec:
    service_id: str
    raw_config: Mapping[str, Any]


class ServiceFactory(Protocol):
    def __call__(self, context: ServiceContext) -> Component: ...


class _EndpointRegistry:
    def __init__(self) -> None:
        self._values: dict[str, object] = {}
        self._owners: dict[str, str] = {}

    def export(
        self,
        name: str,
        value: object,
        *,
        owner: str,
        replace: bool = False,
    ) -> None:
        existing_owner = self._owners.get(name)
        if existing_owner is not None and not replace:
            raise RuntimeError(
                f"Endpoint {name!r} is already exported by service {existing_owner!r}"
            )
        self._values[name] = value
        self._owners[name] = owner

    def get(self, name: str, default: object | None = None) -> object | None:
        return self._values.get(name, default)

    def require(self, name: str) -> object:
        value = self.get(name)
        if value is None:
            raise LookupError(f"Required endpoint {name!r} is not available")
        return value


@dataclass(frozen=True, slots=True)
class ServiceContext:
    service_id: str
    raw_config: Mapping[str, Any]
    document: ConfigDocument
    base_dir: Path
    _registry: _EndpointRegistry

    def export_endpoint(
        self,
        name: str,
        value: object,
        *,
        replace: bool = False,
    ) -> object:
        self._registry.export(name, value, owner=self.service_id, replace=replace)
        return value

    def get_endpoint(self, name: str, default: object | None = None) -> object | None:
        return self._registry.get(name, default)

    def require_endpoint(self, name: str) -> object:
        return self._registry.require(name)


@dataclass(frozen=True, slots=True)
class ServiceActivationResult:
    components: tuple[Component, ...]
    endpoint_names: tuple[str, ...]
    _registry: _EndpointRegistry

    def get_endpoint(self, name: str, default: object | None = None) -> object | None:
        return self._registry.get(name, default)


def available_service_names() -> list[str]:
    return sorted(
        entry_point.name
        for entry_point in entry_points().select(group=SERVICE_ENTRYPOINT_GROUP)
    )


def load_service_factory(service_id: str) -> object | None:
    for entry_point in entry_points().select(group=SERVICE_ENTRYPOINT_GROUP):
        if entry_point.name != service_id:
            continue
        return entry_point.load()
    return None


def _legacy_service_config(
    document: ConfigDocument,
    service_id: str,
) -> Mapping[str, Any]:
    if service_id == "deckr.controller":
        legacy = document.namespace("deckr.controller")
        return legacy if legacy is not None else {}

    if service_id.startswith("deckr.plugin_hosts."):
        legacy_name = service_id.removeprefix("deckr.plugin_hosts.").replace(".", "_")
        return document.children("deckr.plugin_hosts").get(legacy_name, {})

    return {}


def resolve_service_instance_specs(
    document: ConfigDocument,
    *,
    discovered_service_ids: list[str] | tuple[str, ...],
    service_filter: Callable[[str], bool] | None = None,
) -> list[ServiceInstanceSpec]:
    configured = document.children("deckr.services")
    service_ids = set(discovered_service_ids)
    service_ids.update(configured)
    if service_filter is not None:
        service_ids = {service_id for service_id in service_ids if service_filter(service_id)}
    return [
        ServiceInstanceSpec(
            service_id=service_id,
            raw_config=configured.get(service_id, _legacy_service_config(document, service_id)),
        )
        for service_id in sorted(service_ids)
    ]


def _accepts_context(factory: object) -> bool:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    return "context" in signature.parameters


def _invoke_factory(factory: object, *, context: ServiceContext) -> object:
    if _accepts_context(factory):
        return factory(context=context)
    return factory(context)


def _start_order(service_id: str) -> tuple[int, str]:
    if service_id == "deckr.controller":
        return (1, service_id)
    return (0, service_id)


async def activate_services(
    document: ConfigDocument,
    component_manager: ComponentManager,
    *,
    service_filter: Callable[[str], bool] | None = None,
) -> ServiceActivationResult:
    registry = _EndpointRegistry()
    specs = resolve_service_instance_specs(
        document,
        discovered_service_ids=available_service_names(),
        service_filter=service_filter,
    )
    created: list[tuple[str, Component]] = []

    for spec in specs:
        try:
            factory = load_service_factory(spec.service_id)
        except Exception as exc:
            if spec.raw_config:
                raise RuntimeError(
                    f"Configured service {spec.service_id!r} failed to load"
                ) from exc
            logger.info(
                "Skipping implicit service %s: %s",
                spec.service_id,
                exc,
            )
            continue
        if factory is None:
            if spec.raw_config:
                raise RuntimeError(
                    f"Configured service {spec.service_id!r} is not installed"
                )
            continue

        context = ServiceContext(
            service_id=spec.service_id,
            raw_config=spec.raw_config,
            document=document,
            base_dir=document.base_dir,
            _registry=registry,
        )
        try:
            component = _invoke_factory(factory, context=context)
        except ServiceNotConfigured:
            logger.info("Skipping service %s: inactive or not configured", spec.service_id)
            continue

        if not isinstance(component, Component):
            raise TypeError(
                f"Service {spec.service_id!r} did not return a Component"
            )
        created.append((spec.service_id, component))

    components: list[Component] = []
    for _service_id, component in sorted(
        created, key=lambda item: _start_order(item[0])
    ):
        await component_manager.add_component(component)
        components.append(component)

    return ServiceActivationResult(
        components=tuple(components),
        endpoint_names=tuple(sorted(registry._values)),
        _registry=registry,
    )


async def service_runner(
    document: ConfigDocument,
    *,
    service_filter: Callable[[str], bool] | None = None,
) -> None:
    component_manager = ComponentManager()
    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await activate_services(
            document,
            component_manager,
            service_filter=service_filter,
        )
        await anyio.sleep_forever()
