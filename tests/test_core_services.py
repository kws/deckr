from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from memory_lane_substrate import memory_deckr

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
from deckr.launcher import build_runtime_substrate
from deckr.substrates.nats import NatsSubstrate


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
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        yield component_host, deckr


def test_resolve_component_specs_includes_singleton_and_multi_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
            consumes=("hardware_messages", "actions"),
            publishes=("actions",),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    provider_runtime = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.action_providers.python",
            config_prefix="deckr.action_providers.python",
            consumes=("actions",),
            publishes=("actions",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )

    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: {
            "deckr.controller": controller,
            "deckr.action_providers.python": provider_runtime,
        }[component_id],
    )

    document = _document(
        {
            "deckr": {
                "controller": {"log_level": "debug"},
                "action_providers": {
                    "python": {
                        "enabled": False,
                        "instances": {
                            "main": {"provider_instance_id": "python"},
                            "remote": {"provider_instance_id": "remote"},
                        },
                    }
                },
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller", "deckr.action_providers.python"],
    )

    assert [
        (spec.component_id, spec.instance_id, dict(spec.raw_config), spec.runtime_name)
        for spec in specs
    ] == [
        (
            "deckr.action_providers.python",
            "main",
            {"provider_instance_id": "python"},
            "deckr.action_providers.python:main",
        ),
        (
            "deckr.action_providers.python",
            "remote",
            {"provider_instance_id": "remote"},
            "deckr.action_providers.python:remote",
        ),
        ("deckr.controller", "default", {"log_level": "debug"}, "deckr.controller"),
    ]


def test_configured_component_specs_rejects_uninstalled_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deckr.components._host.available_component_ids", lambda: [])
    document = _document({"deckr": {"controller": {"id": "controller-main"}}})

    with pytest.raises(ValueError, match="deckr.controller"):
        configured_component_instance_specs(document)


@pytest.mark.parametrize(
    ("namespace", "detail"),
    [
        ("websocket", {"instances": {"main": {"mode": "server"}}}),
        ("mqtt", {"instances": {"main": {"hostname": "mqtt"}}}),
        ("bus", {"instances": {"main": {}}}),
        ("routes", {"instances": {"main": {}}}),
    ],
)
def test_configured_component_specs_rejects_removed_lane_transport_prefixes(
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    detail: dict[str, object],
) -> None:
    monkeypatch.setattr("deckr.components._host.available_component_ids", lambda: [])
    document = _document({"deckr": {"transports": {namespace: detail}}})

    with pytest.raises(ValueError, match="removed Deckr lane transport"):
        configured_component_instance_specs(document)


def test_configured_component_specs_loads_only_configured_entrypoints(
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
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.controller", "deckr.drivers.elgato"],
    )

    def load(component_id: str):
        if component_id == "deckr.drivers.elgato":
            raise ModuleNotFoundError("deckr.transports")
        if component_id == "deckr.controller":
            return controller
        raise AssertionError(component_id)

    monkeypatch.setattr("deckr.components._host.load_component_definition", load)
    document = _document({"deckr": {"controller": {"id": "controller-main"}}})

    specs = configured_component_instance_specs(document)

    assert [spec.component_id for spec in specs] == ["deckr.controller"]


@pytest.mark.asyncio
async def test_start_components_passes_lane_registry_to_component() -> None:
    seen: dict[str, object] = {}

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.action_providers.python",
            config_prefix="deckr.action_providers.python",
            consumes=("actions",),
            publishes=("actions",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: (
            seen.setdefault("lane", context.require_lane("actions"))
            and _DummyComponent(name=context.runtime_name)
        ),
    )
    document = _document(
        {
            "deckr": {
                "action_providers": {
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
        definitions={"deckr.action_providers.python": definition},
    )
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        await component_host.component_manager.wait_for_state(
            "deckr.action_providers.python:main",
            ComponentState.RUNNING,
        )
        async with deckr.lane("actions").register_endpoint(
            "controller:main"
        ) as controller:
            await controller.send(
                recipient="action_provider:main",
                subject=entity_subject("test"),
                message_type="actionExtension",
                body={
                    "extensionType": "test.component",
                    "extensionSchemaId": "test.component.v1",
                    "data": {},
                },
            )

    assert isinstance(seen["lane"], Lane)


@pytest.mark.asyncio
async def test_start_components_passes_current_state_to_component() -> None:
    seen: dict[str, object] = {}

    class StateComponent(_DummyComponent):
        async def start(self, ctx) -> None:
            return

    def factory(context):
        seen["state"] = context.state()
        return StateComponent(name=context.runtime_name)

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
        ),
        factory=factory,
    )
    document = _document({"deckr": {"controller": {}}})
    plan = resolve_component_host_plan(
        document,
        definitions={"deckr.controller": definition},
    )
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan):
        assert seen["state"] is deckr.state()


def test_runtime_substrate_config_defaults_to_nats() -> None:
    document = _document({"deckr": {}})
    plan = resolve_component_host_plan(document, definitions={})

    substrate = build_runtime_substrate(document, lane_contracts=plan.lane_contracts)

    assert isinstance(substrate, NatsSubstrate)
    assert substrate.url == "nats://127.0.0.1:4222"


def test_runtime_substrate_config_rejects_local_substrate() -> None:
    document = _document({"deckr": {"runtime": {"substrate": {"kind": "local"}}}})
    plan = resolve_component_host_plan(document, definitions={})

    with pytest.raises(ValueError, match="Unsupported Deckr runtime substrate"):
        build_runtime_substrate(document, lane_contracts=plan.lane_contracts)


def test_runtime_substrate_config_builds_nats_substrate() -> None:
    document = _document(
        {
            "deckr": {
                "runtime": {
                    "substrate": {
                        "kind": "nats",
                        "url": "nats://nats.example:4222",
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(document, definitions={})

    substrate = build_runtime_substrate(document, lane_contracts=plan.lane_contracts)

    assert isinstance(substrate, NatsSubstrate)
    assert substrate.url == "nats://nats.example:4222"


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

    with pytest.raises(ValueError, match="removed field\\(s\\) route_policy"):
        resolve_component_host_plan(document, definitions={})


@pytest.mark.parametrize(
    "field",
    [
        "mqtt",
        "remote_endpoints",
        "route_table",
        "routes",
        "transport_route",
        "websocket",
    ],
)
def test_deployment_lane_contract_rejects_removed_transport_fields(
    field: str,
) -> None:
    document = _document(
        {
            "deckr": {
                "lane_contracts": {
                    "acme.events": {
                        "schema_id": "acme.events.v1",
                        field: {},
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match=field):
        resolve_component_host_plan(document, definitions={})


@pytest.mark.parametrize("field", ["mqtt", "websocket", "remote_endpoints"])
def test_deployment_lane_contract_rejects_removed_delivery_fields(field: str) -> None:
    document = _document(
        {
            "deckr": {
                "lane_contracts": {
                    "acme.events": {
                        "schema_id": "acme.events.v1",
                        "delivery": {field: {}},
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match=field):
        resolve_component_host_plan(document, definitions={})
