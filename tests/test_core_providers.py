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


def test_singleton_cardinality_rejects_second_planned_instance() -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(component_id="dev.deckr.controller"),
        factory=lambda context: None,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "one": {
                            "component": "dev.deckr.controller",
                            "instance_id": "one",
                        },
                        "two": {
                            "component": "dev.deckr.controller",
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
            definitions={"dev.deckr.controller": controller},
        )


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
