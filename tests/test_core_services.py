from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from deckr.core.component import BaseComponent, ComponentManager
from deckr.core.config import ConfigDocument
from deckr.core.hardware_events import DeviceConnectedEvent
from deckr.core.plugin_messages import HostMessage
from deckr.core.services import (
    ServiceNotConfigured,
    activate_services,
    resolve_service_instance_specs,
)
from deckr.hardware.events import DeviceConnectedEvent as LegacyDeviceConnectedEvent
from deckr.plugin.messages import HostMessage as LegacyHostMessage


class _DummyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def test_resolve_service_specs_includes_discovered_services_without_config() -> None:
    document = _document({"deckr": {"services": {}}})

    specs = resolve_service_instance_specs(
        document,
        discovered_service_ids=["deckr.controller", "deckr.plugin_hosts.python"],
    )

    assert [(spec.service_id, dict(spec.raw_config)) for spec in specs] == [
        ("deckr.controller", {}),
        ("deckr.plugin_hosts.python", {}),
    ]


def test_resolve_service_specs_uses_legacy_plugin_host_namespace() -> None:
    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {"host_id": "python"},
                    "python_mqtt": {"hostname": "mqtt.example.net", "topic": "deckr/v1"},
                }
            }
        }
    )

    specs = resolve_service_instance_specs(
        document,
        discovered_service_ids=[
            "deckr.plugin_hosts.python",
            "deckr.plugin_hosts.python.mqtt",
        ],
    )

    assert [(spec.service_id, dict(spec.raw_config)) for spec in specs] == [
        ("deckr.plugin_hosts.python", {"host_id": "python"}),
        (
            "deckr.plugin_hosts.python.mqtt",
            {"hostname": "mqtt.example.net", "topic": "deckr/v1"},
        ),
    ]


@pytest.mark.asyncio
async def test_activate_services_passes_only_own_config_and_supports_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def controller_factory(*, context):
        assert context.raw_config == {"log_level": "debug"}
        context.export_endpoint("plugin_messages", "plugin-bus")
        seen["controller_document"] = context.document
        return _DummyComponent(name="controller")

    def host_factory(*, context):
        seen["host_config"] = dict(context.raw_config)
        seen["endpoint"] = context.require_endpoint("plugin_messages")
        return _DummyComponent(name="python-host")

    monkeypatch.setattr(
        "deckr.core.services.available_service_names",
        lambda: ["deckr.controller", "deckr.plugin_hosts.python"],
    )
    monkeypatch.setattr(
        "deckr.core.services.load_service_factory",
        lambda service_id: {
            "deckr.controller": controller_factory,
            "deckr.plugin_hosts.python": host_factory,
        }.get(service_id),
    )

    document = _document(
        {
            "deckr": {
                "services": {
                    "deckr.controller": {"log_level": "debug"},
                    "deckr.plugin_hosts.python": {"enabled": True},
                }
            }
        }
    )
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_services(document, component_manager)

        assert [component.name for component in result.components] == [
            "python-host",
            "controller",
        ]
        assert seen["host_config"] == {"enabled": True}
        assert seen["endpoint"] == "plugin-bus"
        assert seen["controller_document"] is document

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_activate_services_skips_service_that_declines_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inactive_factory(*, context):
        raise ServiceNotConfigured("inactive")

    monkeypatch.setattr(
        "deckr.core.services.available_service_names",
        lambda: ["deckr.plugin_hosts.python.mqtt"],
    )
    monkeypatch.setattr(
        "deckr.core.services.load_service_factory",
        lambda service_id: inactive_factory,
    )

    document = _document({"deckr": {"services": {}}})
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_services(document, component_manager)

        assert result.components == ()
        assert component_manager.list_components() == []

        tg.cancel_scope.cancel()


def test_core_plugin_messages_alias_matches_legacy_contracts() -> None:
    assert HostMessage is LegacyHostMessage


def test_core_hardware_events_alias_matches_legacy_contracts() -> None:
    assert DeviceConnectedEvent is LegacyDeviceConnectedEvent
