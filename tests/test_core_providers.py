from __future__ import annotations

from pathlib import Path

import pytest

from deckr.core.config import ConfigDocument
from deckr.core.providers import (
    ActivationOrigin,
    resolve_provider_instance_specs,
)


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def test_resolve_provider_specs_creates_implicit_instances() -> None:
    document = _document({"deckr": {"plugin_hosts": {}}})

    specs = resolve_provider_instance_specs(
        document,
        namespace_path="deckr.plugin_hosts",
        discovered_provider_ids=["python", "python_mqtt"],
    )

    assert [
        (spec.instance_id, spec.provider_id, spec.activation_origin)
        for spec in specs
    ] == [
        ("python", "python", ActivationOrigin.IMPLICIT),
        ("python_mqtt", "python_mqtt", ActivationOrigin.IMPLICIT),
    ]


def test_resolve_provider_specs_overlays_explicit_config() -> None:
    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {"enabled": False},
                    "remote": {"provider": "python_mqtt", "topic": "deckr/v1"},
                }
            }
        }
    )

    specs = resolve_provider_instance_specs(
        document,
        namespace_path="deckr.plugin_hosts",
        discovered_provider_ids=["python", "python_mqtt"],
    )

    assert [
        (spec.instance_id, spec.provider_id, spec.activation_origin, dict(spec.raw_config))
        for spec in specs
    ] == [
        ("python", "python", ActivationOrigin.EXPLICIT, {"enabled": False}),
        ("python_mqtt", "python_mqtt", ActivationOrigin.IMPLICIT, {}),
        (
            "remote",
            "python_mqtt",
            ActivationOrigin.EXPLICIT,
            {"provider": "python_mqtt", "topic": "deckr/v1"},
        ),
    ]


def test_resolve_provider_specs_rejects_colliding_provider_override() -> None:
    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {"provider": "python_mqtt"},
                }
            }
        }
    )

    with pytest.raises(ValueError, match="cannot override implicit provider"):
        resolve_provider_instance_specs(
            document,
            namespace_path="deckr.plugin_hosts",
            discovered_provider_ids=["python"],
        )
