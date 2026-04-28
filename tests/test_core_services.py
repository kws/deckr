from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from deckr.components import (
    BaseComponent,
    ComponentCardinality,
    ComponentDefinition,
    ComponentManifest,
    ComponentState,
    configured_component_instance_specs,
    resolve_component_host_plan,
    resolve_component_instance_specs,
    start_components,
)
from deckr.contracts.lanes import LaneContract
from deckr.contracts.messages import entity_subject
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.runtime import Deckr


class _DummyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


@asynccontextmanager
async def _running_components(document: ConfigDocument):
    plan = resolve_component_host_plan(document)
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as host:
        yield host, deckr


def test_resolve_component_specs_includes_singleton_and_multi_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
            consumes=("hardware_messages", "plugin_messages"),
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
        "deckr.components._host.load_component_definition",
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


def test_configured_component_specs_rejects_uninstalled_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deckr.components._host.available_component_ids", lambda: [])
    document = _document({"deckr": {"controller": {"id": "controller-main"}}})

    with pytest.raises(ValueError, match="deckr.controller"):
        configured_component_instance_specs(document)


@pytest.mark.asyncio
async def test_start_components_passes_lane_registry_to_component() -> None:
    seen: dict[str, object] = {}

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.plugin_hosts.python",
            config_prefix="deckr.plugin_hosts.python",
            consumes=("plugin_messages",),
            publishes=("plugin_messages",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: (
            seen.setdefault("lane", context.require_lane("plugin_messages"))
            and _DummyComponent(name=context.runtime_name)
        ),
    )
    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {},
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"deckr.plugin_hosts.python": definition},
    )
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as host:
        await host.component_manager.wait_for_state(
            "deckr.plugin_hosts.python:main",
            ComponentState.RUNNING,
        )
        await deckr.lane("plugin_messages").endpoint("controller:main").send(
            recipient="host:main",
            subject=entity_subject("test"),
            message_type="setTitle",
            body={},
        )

    assert isinstance(seen["lane"], Lane)


def test_deployment_lane_contract_uses_direct_v1_fields() -> None:
    component = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="acme.worker",
            config_prefix="deckr.acme.worker",
            publishes=("acme.events",),
            lane_contracts=(
                LaneContract(
                    lane="acme.events",
                    schema_id="acme.events.v1",
                    message_types=frozenset({"ping"}),
                    allowed_sender_families=frozenset({"acme"}),
                    allowed_recipient_families=frozenset({"controller"}),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    document = _document(
        {
            "deckr": {
                "acme": {"worker": {}},
                "lane_contracts": {
                    "acme.events": {
                        "message_types": ["ping"],
                        "allowed_sender_families": ["acme"],
                        "allowed_recipient_families": ["controller"],
                    }
                },
            }
        }
    )

    plan = resolve_component_host_plan(document, definitions={"acme.worker": component})
    contract = plan.lane_contracts.contract_for("acme.events")

    assert contract.allowed_sender_families == frozenset({"acme"})
    assert contract.allowed_recipient_families == frozenset({"controller"})


def test_deployment_lane_contract_rejects_removed_route_policy() -> None:
    document = _document(
        {
            "deckr": {
                "lane_contracts": {
                    "acme.events": {
                        "schema_id": "acme.events.v1",
                        "route_policy": {
                            "remote_claim_endpoint_families": ["acme"],
                        },
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="route_policy has been removed"):
        resolve_component_host_plan(document, definitions={})
