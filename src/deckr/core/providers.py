from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

from deckr.core.backplane import DeckrBackplane
from deckr.core.component import Component
from deckr.core.config import ConfigDocument

DRIVER_ENTRYPOINT_GROUP = "deckr.drivers"
PLUGIN_HOST_ENTRYPOINT_GROUP = "deckr.plugin_hosts"


class ActivationOrigin(StrEnum):
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"


class ProviderNotConfigured(RuntimeError):
    """Raised by implicitly discovered providers that choose not to self-start."""


@dataclass(frozen=True, slots=True)
class ProviderInstanceSpec:
    instance_id: str
    provider_id: str
    activation_origin: ActivationOrigin
    raw_config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DriverProviderContext:
    instance_id: str
    provider_id: str
    activation_origin: ActivationOrigin
    raw_config: Mapping[str, Any]
    document: ConfigDocument
    base_dir: Path
    backplane: DeckrBackplane


@dataclass(frozen=True, slots=True)
class PluginHostProviderContext:
    instance_id: str
    provider_id: str
    activation_origin: ActivationOrigin
    raw_config: Mapping[str, Any]
    document: ConfigDocument
    base_dir: Path
    backplane: DeckrBackplane
    controller_id: str


class DriverProviderFactory(Protocol):
    def __call__(self, context: DriverProviderContext) -> Component: ...


class PluginHostProviderFactory(Protocol):
    def __call__(self, context: PluginHostProviderContext) -> Component: ...


def available_provider_names(entrypoint_group: str) -> list[str]:
    return sorted(
        entry_point.name
        for entry_point in entry_points().select(group=entrypoint_group)
    )


def load_provider_factories(entrypoint_group: str) -> dict[str, object]:
    factories: dict[str, object] = {}
    for entry_point in sorted(
        entry_points().select(group=entrypoint_group),
        key=lambda item: item.name,
    ):
        factories[entry_point.name] = entry_point.load()
    return factories


def load_provider_factory(entrypoint_group: str, provider_id: str) -> object | None:
    for entry_point in entry_points().select(group=entrypoint_group):
        if entry_point.name != provider_id:
            continue
        return entry_point.load()
    return None


def resolve_provider_instance_specs(
    document: ConfigDocument,
    *,
    namespace_path: str,
    discovered_provider_ids: list[str] | tuple[str, ...],
) -> list[ProviderInstanceSpec]:
    specs: dict[str, ProviderInstanceSpec] = {
        provider_id: ProviderInstanceSpec(
            instance_id=provider_id,
            provider_id=provider_id,
            activation_origin=ActivationOrigin.IMPLICIT,
            raw_config={},
        )
        for provider_id in discovered_provider_ids
    }

    for instance_id, raw_config in document.children(namespace_path).items():
        provider_id = str(raw_config.get("provider", instance_id)).strip()
        if not provider_id:
            raise ValueError(
                f"{namespace_path}.{instance_id}.provider must not be empty"
            )
        existing = specs.get(instance_id)
        if existing is not None and existing.provider_id != provider_id:
            raise ValueError(
                f"{namespace_path}.{instance_id} cannot override implicit provider "
                f"{existing.provider_id!r} with {provider_id!r}"
            )
        specs[instance_id] = ProviderInstanceSpec(
            instance_id=instance_id,
            provider_id=provider_id,
            activation_origin=ActivationOrigin.EXPLICIT,
            raw_config=raw_config,
        )

    return [specs[name] for name in sorted(specs)]
