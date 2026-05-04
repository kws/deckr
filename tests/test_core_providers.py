from __future__ import annotations

from pathlib import Path

import pytest

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


def test_component_instance_uses_generic_config_mapping() -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.k-si.deckr.controller",
            endpoint_slots=("controller",),
        ),
        factory=lambda context: None,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "controller_main": {
                            "component": "com.k-si.deckr.controller",
                            "instance_id": "main",
                            "endpoints": {"controller": "controller-main"},
                            "config": {
                                "log_level": "info",
                                "settings": {"file": {"path": "state"}},
                            },
                        }
                    }
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        definitions={"com.k-si.deckr.controller": controller},
    )

    assert len(specs) == 1
    assert specs[0].component_id == "com.k-si.deckr.controller"
    assert specs[0].instance_id == "main"
    assert dict(specs[0].endpoints) == {"controller": "controller-main"}
    assert dict(specs[0].config) == {
        "log_level": "info",
        "settings": {"file": {"path": "state"}},
    }


def test_installed_component_without_instance_is_not_created() -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(component_id="com.k-si.deckr.controller"),
        factory=lambda context: None,
    )
    document = _document({"deckr": {}})

    specs = resolve_component_instance_specs(
        document,
        definitions={"com.k-si.deckr.controller": controller},
    )

    assert specs == []


def test_singleton_cardinality_rejects_second_planned_instance() -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(component_id="com.k-si.deckr.controller"),
        factory=lambda context: None,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "one": {
                            "component": "com.k-si.deckr.controller",
                            "instance_id": "one",
                        },
                        "two": {
                            "component": "com.k-si.deckr.controller",
                            "instance_id": "two",
                        },
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="singleton cardinality"):
        resolve_component_instance_specs(
            document,
            definitions={"com.k-si.deckr.controller": controller},
        )


def test_multi_instance_component_creates_declared_instances() -> None:
    runtime = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.k-si.deckr.action_provider_runtime.python",
            cardinality=ComponentCardinality.MULTI_INSTANCE,
            endpoint_slots=("action_provider",),
        ),
        factory=lambda context: None,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "main": {
                            "component": (
                                "com.k-si.deckr.action_provider_runtime.python"
                            ),
                            "instance_id": "main",
                            "endpoints": {"action_provider": "python-main"},
                            "config": {"provider_id": "main"},
                        },
                        "remote": {
                            "component": (
                                "com.k-si.deckr.action_provider_runtime.python"
                            ),
                            "instance_id": "remote",
                            "endpoints": {"action_provider": "python-remote"},
                            "config": {"provider_id": "remote"},
                        },
                    }
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        definitions={"com.k-si.deckr.action_provider_runtime.python": runtime},
    )

    assert [(spec.instance_id, dict(spec.config)) for spec in specs] == [
        ("main", {"provider_id": "main"}),
        ("remote", {"provider_id": "remote"}),
    ]


def test_component_definition_can_resolve_instance_specific_lanes() -> None:
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.example.worker",
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: None,
        resolve_lanes=lambda **kwargs: ResolvedLaneSet(
            consumes=("actions",),
            publishes=("actions", "hardware_messages"),
        ),
    )

    lanes = definition.lanes_for(
        config={"bindings": {"action_provider": {"lane": "actions"}}},
        endpoints={},
        instance_id="main",
    )

    assert lanes == ResolvedLaneSet(
        consumes=("actions",),
        publishes=("actions", "hardware_messages"),
    )
