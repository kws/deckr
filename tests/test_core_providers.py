from __future__ import annotations

from pathlib import Path

from deckr.components import (
    ComponentCardinality,
    ComponentDefinition,
    ComponentManifest,
    ResolvedLaneSet,
    resolve_component_instance_specs,
)
from deckr.core.config import ConfigDocument


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def test_singleton_component_uses_exact_prefix_mapping(
    monkeypatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
        ),
        factory=lambda context: None,
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: controller,
    )

    document = _document(
        {
            "deckr": {
                "controller": {
                    "log_level": "info",
                    "settings": {"file": {"path": "state"}},
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller"],
    )

    assert len(specs) == 1
    assert dict(specs[0].raw_config) == {
        "log_level": "info",
        "settings": {"file": {"path": "state"}},
    }


def test_singleton_component_without_exact_prefix_is_not_created(
    monkeypatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
        ),
        factory=lambda context: None,
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: controller,
    )

    document = _document({"deckr": {}})

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller"],
    )

    assert specs == []


def test_multi_instance_component_only_creates_declared_instances(
    monkeypatch,
) -> None:
    host = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.plugin_hosts.python",
            config_prefix="deckr.plugin_hosts.python",
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: None,
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: host,
    )

    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {"host_id": "python"},
                            "remote": {"host_id": "remote"},
                        }
                    }
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.plugin_hosts.python"],
    )

    assert [(spec.instance_id, dict(spec.raw_config)) for spec in specs] == [
        ("main", {"host_id": "python"}),
        ("remote", {"host_id": "remote"}),
    ]


def test_component_definition_can_resolve_instance_specific_lanes() -> None:
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.transports.mqtt",
            config_prefix="deckr.transports.mqtt",
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: None,
        resolve_lanes=lambda **kwargs: ResolvedLaneSet(
            consumes=("plugin_messages",),
            publishes=("plugin_messages", "hardware_events"),
        ),
    )

    lanes = definition.lanes_for(
        raw_config={"bindings": {"plugin": {"lane": "plugin_messages"}}},
        instance_id="main",
    )

    assert lanes == ResolvedLaneSet(
        consumes=("plugin_messages",),
        publishes=("plugin_messages", "hardware_events"),
    )
