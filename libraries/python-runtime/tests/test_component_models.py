from __future__ import annotations

import pytest

from deckr_python_runtime.components import (
    ComponentManifest,
    DependencyCondition,
    DependencyConditionState,
    DependencyKind,
    DependencyMode,
    ReadinessState,
    dependency_effective_readiness,
    dependency_from_mapping,
)


def test_component_manifest_is_python_runtime_pydantic_model() -> None:
    manifest = ComponentManifest.model_validate(
        {
            "componentId": "dev.deckr.example",
            "consumes": ["actions"],
            "publishes": ["services"],
            "endpointSlots": ["action_provider"],
        }
    )

    assert manifest.component_id == "dev.deckr.example"
    assert manifest.consumes == ("actions",)
    assert manifest.endpoint_slots == ("action_provider",)
    assert manifest.model_dump(by_alias=True, exclude_none=True, mode="json")[
        "componentId"
    ] == "dev.deckr.example"

    with pytest.raises(ValueError):
        ComponentManifest.model_validate(
            {"componentId": "dev.deckr.example", "unknown": True}
        )


def test_dependency_from_mapping_and_effective_readiness() -> None:
    dependency = dependency_from_mapping(
        "controller",
        {
            "kind": "endpoint",
            "mode": "required",
            "lane": "actions",
            "endpoint": "controller:controller-main",
        },
        field_name="deckr.components.instances.worker.dependencies.controller",
    )

    assert dependency.endpoint.endpoint_id == "controller-main"
    assert dependency.lane == "actions"

    readiness, reasons, diagnostics = dependency_effective_readiness(
        {
            "controller": DependencyCondition(
                name="controller",
                kind=DependencyKind.ENDPOINT,
                mode=DependencyMode.REQUIRED,
                state=DependencyConditionState.UNKNOWN,
                reason="state_unavailable",
            )
        }
    )

    assert readiness == ReadinessState.UNREADY
    assert reasons == ("dependency.controller.unknown",)
    assert diagnostics == {
        "dependencies": {
            "controller": {
                "kind": "endpoint",
                "mode": "required",
                "state": "unknown",
                "reason": "state_unavailable",
            }
        }
    }
