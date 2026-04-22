from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from deckr.core.component import BaseComponent, ComponentManager
from deckr.core.components import (
    ComponentActivationResult,
    ComponentCardinality,
    ComponentDefinition,
    ComponentManifest,
    activate_components,
    resolve_component_instance_specs,
)
from deckr.core.config import ConfigDocument
from deckr.hardware.events import hardware_transport_message_schema
from deckr.plugin.messages import HostMessage


def _core_wire_schemas() -> dict[str, dict]:
    return {
        "plugin.host_message": HostMessage.schema_dict(),
        "hardware.transport_message": hardware_transport_message_schema(),
    }


class _DummyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def test_resolve_component_specs_includes_singleton_and_multi_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
            consumes=("hardware_events", "plugin_messages"),
            publishes=("plugin_messages",),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    host = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.plugin_hosts.python",
            config_prefix="deckr.plugin_hosts.python",
            consumes=("plugin_messages",),
            publishes=("plugin_messages",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )

    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: {
            "deckr.controller": controller,
            "deckr.plugin_hosts.python": host,
        }[component_id],
    )

    document = _document(
        {
            "deckr": {
                "controller": {"log_level": "debug"},
                "plugin_hosts": {
                    "python": {
                        "enabled": False,
                        "instances": {
                            "main": {"host_id": "python"},
                            "remote": {"host_id": "remote"},
                        },
                    }
                },
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller", "deckr.plugin_hosts.python"],
    )

    assert [
        (spec.component_id, spec.instance_id, dict(spec.raw_config), spec.runtime_name)
        for spec in specs
    ] == [
        ("deckr.controller", "default", {"log_level": "debug"}, "deckr.controller"),
        (
            "deckr.plugin_hosts.python",
            "main",
            {"host_id": "python"},
            "deckr.plugin_hosts.python:main",
        ),
        (
            "deckr.plugin_hosts.python",
            "remote",
            {"host_id": "remote"},
            "deckr.plugin_hosts.python:remote",
        ),
    ]


def test_resolve_component_specs_do_not_inherit_parent_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.plugin_hosts.python",
            config_prefix="deckr.plugin_hosts.python",
            consumes=("plugin_messages",),
            publishes=("plugin_messages",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )

    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: host,
    )

    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {
                        "enabled": False,
                        "instances": {
                            "main": {},
                        },
                    }
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.plugin_hosts.python"],
    )

    assert len(specs) == 1
    assert dict(specs[0].raw_config) == {}


def test_resolve_component_specs_skip_unconfigured_singletons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )

    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: controller,
    )

    document = _document({"deckr": {}})

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller"],
    )

    assert specs == []


@pytest.mark.asyncio
async def test_activate_components_provides_prebuilt_lanes_and_exact_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def controller_factory(context):
        seen["controller_config"] = dict(context.raw_config)
        seen["controller_has_document"] = hasattr(context, "document")
        seen["plugin_lane"] = context.require_lane("plugin_messages")
        seen["hardware_lane"] = context.require_lane("hardware_events")
        return _DummyComponent(name=context.runtime_name)

    def host_factory(context):
        seen["host_config"] = dict(context.raw_config)
        seen["host_has_document"] = hasattr(context, "document")
        seen["host_lane"] = context.require_lane("plugin_messages")
        return _DummyComponent(name=context.runtime_name)

    monkeypatch.setattr(
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.controller", "deckr.plugin_hosts.python"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: {
            "deckr.controller": ComponentDefinition(
                manifest=ComponentManifest(
                    component_id="deckr.controller",
                    config_prefix="deckr.controller",
                    consumes=("hardware_events", "plugin_messages"),
                    publishes=("plugin_messages",),
                ),
                factory=controller_factory,
            ),
            "deckr.plugin_hosts.python": ComponentDefinition(
                manifest=ComponentManifest(
                    component_id="deckr.plugin_hosts.python",
                    config_prefix="deckr.plugin_hosts.python",
                    consumes=("plugin_messages",),
                    publishes=("plugin_messages",),
                    cardinality=ComponentCardinality.MULTI_INSTANCE,
                ),
                factory=host_factory,
            ),
        }[component_id],
    )

    document = _document(
        {
            "deckr": {
                "controller": {"log_level": "debug"},
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {"host_id": "python"},
                        }
                    }
                },
            }
        }
    )
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result: ComponentActivationResult = await activate_components(
            document,
            component_manager,
        )

        assert [component.name for component in result.components] == [
            "deckr.controller",
            "deckr.plugin_hosts.python:main",
        ]
        assert result.lane_names == ("hardware_events", "plugin_messages")
        assert seen["controller_config"] == {"log_level": "debug"}
        assert seen["host_config"] == {"host_id": "python"}
        assert seen["controller_has_document"] is False
        assert seen["host_has_document"] is False
        assert seen["plugin_lane"] is seen["host_lane"]
        assert seen["plugin_lane"] is not None
        assert seen["hardware_lane"] is not None

        tg.cancel_scope.cancel()


def test_host_message_is_pydantic_and_schema_exportable() -> None:
    message = HostMessage(
        from_id="host:python",
        to_id="all_controllers",
        type="hostOnline",
        payload={"hostId": "python"},
    )

    payload = message.to_dict()

    assert payload["from"] == "host:python"
    assert payload["to"] == "all_controllers"
    assert payload["messageId"]
    assert HostMessage.from_dict(payload) == message

    schemas = _core_wire_schemas()
    assert "plugin.host_message" in schemas
    assert "hardware.transport_message" in schemas


def test_host_message_from_dict_requires_message_id() -> None:
    with pytest.raises(ValueError, match="messageId is required"):
        HostMessage.from_dict(
            {
                "from": "host:python",
                "to": "controller:controller-main",
                "type": "hostOnline",
                "payload": {"hostId": "python"},
            }
        )


def test_host_message_routing_requires_canonical_addresses() -> None:
    controller_message = HostMessage(
        from_id="host:python",
        to_id="controller:controller-main",
        type="hostOnline",
        payload={"hostId": "python"},
    )
    assert controller_message.for_controller("controller-main") is True
    assert controller_message.for_controller("controller-other") is False

    bare_controller = HostMessage(
        from_id="host:python",
        to_id="controller",
        type="hostOnline",
        payload={"hostId": "python"},
    )
    assert bare_controller.for_controller("controller-main") is False

    host_message = HostMessage(
        from_id="controller:controller-main",
        to_id="host:python",
        type="requestActions",
        payload={},
    )
    assert host_message.for_host("python") is True

    bare_host = HostMessage(
        from_id="controller:controller-main",
        to_id="python",
        type="requestActions",
        payload={},
    )
    assert bare_host.for_host("python") is False


def test_extract_device_id_rejects_legacy_context_shape() -> None:
    with pytest.raises(ValueError, match="Invalid contextId"):
        from deckr.plugin.messages import extract_device_id

        extract_device_id("device-1.0,0")
