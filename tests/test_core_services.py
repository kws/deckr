from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    controllers_broadcast,
    endpoint_target,
    entity_subject,
    host_address,
    message_targets_endpoint,
)
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
from deckr.hardware.events import hardware_message_schema
from deckr.pluginhost.messages import (
    HOST_ONLINE,
    PluginMessageBody,
    plugin_host_subject,
    plugin_message,
    plugin_payload,
)


def _core_wire_schemas() -> dict[str, dict]:
    return {
        "deckr.message.plugin_messages.v1": DeckrMessage.schema_dict(),
        "deckr.message.hardware_events.v1": hardware_message_schema(),
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


def test_deckr_message_is_pydantic_and_schema_exportable() -> None:
    message = plugin_message(
        sender=host_address("python"),
        recipient=controllers_broadcast(),
        message_type=HOST_ONLINE,
        payload={"hostId": "python"},
        subject=plugin_host_subject("python"),
    )

    payload = message.to_dict()

    assert payload["sender"] == "host:python"
    assert payload["recipient"] == {
        "targetType": "broadcast",
        "scope": "controllers",
        "endpointFamily": "controller",
    }
    assert payload["messageId"]
    assert payload["protocolVersion"] == "1"
    assert payload["lane"] == "plugin_messages"
    assert payload["messageType"] == HOST_ONLINE
    assert DeckrMessage.from_dict(payload) == message
    assert plugin_payload(message) == {"hostId": "python"}

    schemas = _core_wire_schemas()
    assert "deckr.message.plugin_messages.v1" in schemas
    assert "deckr.message.hardware_events.v1" in schemas


def test_plugin_body_payload_is_immutable_json() -> None:
    source_payload = {"value": {"nested": [1]}}
    body = PluginMessageBody(payload=source_payload)

    source_payload["value"] = {"nested": [2]}

    assert body.payload == {"value": {"nested": (1,)}}
    with pytest.raises(TypeError):
        body.payload["added"] = True  # type: ignore[index]
    assert body.to_dict()["payload"] == {"value": {"nested": [1]}}


def test_deckr_message_routing_uses_targets_not_legacy_strings() -> None:
    controller_message = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("controller-main"),
        message_type=HOST_ONLINE,
        payload={"hostId": "python"},
        subject=entity_subject("plugin_host", hostId="python"),
    )
    assert message_targets_endpoint(
        controller_message, controller_address("controller-main")
    )
    assert not message_targets_endpoint(
        controller_message, controller_address("controller-other")
    )

    host_message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type="requestActions",
        payload={},
        subject=entity_subject("plugin_actions"),
    )
    assert message_targets_endpoint(host_message, host_address("python"))

    with pytest.raises(ValueError):
        endpoint_target("python")


def test_context_id_routing_extractors_are_removed() -> None:
    from deckr.pluginhost import messages

    assert not hasattr(messages, "extract_device_id")
    assert not hasattr(messages, "extract_slot_id")
    assert not hasattr(messages, "extract_controller_id")
