from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from memory_lane_substrate import memory_deckr

from deckr.components import (
    BaseComponent,
    ComponentDefinition,
    ComponentInstanceDefinition,
    ComponentInstanceSourceContext,
    ComponentInstanceSourceDefinition,
    ComponentManifest,
    ComponentState,
    configured_component_instance_specs,
    resolve_component_host_plan,
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


def _component(
    component_id: str,
    *,
    consumes: tuple[str, ...] = (),
    publishes: tuple[str, ...] = (),
    endpoints: tuple[str, ...] = (),
    lane_contracts: tuple[LaneContract, ...] = (),
) -> ComponentDefinition:
    return ComponentDefinition(
        manifest=ComponentManifest(
            component_id=component_id,
            consumes=consumes,
            publishes=publishes,
            endpoint_slots=endpoints,
            lane_contracts=lane_contracts,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )


def test_resolve_component_specs_from_generic_instances() -> None:
    controller = _component(
        "com.k-si.deckr.controller",
        consumes=("hardware_messages", "actions"),
        publishes=("actions",),
        endpoints=("controller",),
    )
    provider_runtime = _component(
        "com.k-si.deckr.action_provider_runtime.python",
        consumes=("actions",),
        publishes=("actions",),
        endpoints=("action_provider",),
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
                            "config": {"log_level": "debug"},
                        },
                        "python_clock": {
                            "component": (
                                "com.k-si.deckr.action_provider_runtime.python"
                            ),
                            "instance_id": "clock-main",
                            "endpoints": {"action_provider": "python-clock"},
                            "config": {"provider_id": "clock"},
                        },
                    }
                }
            }
        }
    )

    specs = configured_component_instance_specs(
        document,
        definitions={
            "com.k-si.deckr.controller": controller,
            "com.k-si.deckr.action_provider_runtime.python": provider_runtime,
        },
    )

    assert [
        (spec.component_id, spec.instance_id, dict(spec.config), spec.runtime_name)
        for spec in specs
    ] == [
        (
            "com.k-si.deckr.controller",
            "main",
            {"log_level": "debug"},
            "com.k-si.deckr.controller:main",
        ),
        (
            "com.k-si.deckr.action_provider_runtime.python",
            "clock-main",
            {"provider_id": "clock"},
            "com.k-si.deckr.action_provider_runtime.python:clock-main",
        ),
    ]


def test_unknown_component_id_is_plan_error() -> None:
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "missing": {
                            "component": "com.example.missing",
                            "instance_id": "missing",
                        }
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Unknown Deckr component id"):
        configured_component_instance_specs(document, definitions={})


def test_unknown_generic_instance_field_is_plan_error() -> None:
    component = _component("com.example.worker")
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "worker": {
                            "component": "com.example.worker",
                            "instance_id": "worker",
                            "provider_id": "not-generic",
                        }
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Unknown component instance field"):
        configured_component_instance_specs(
            document,
            definitions={"com.example.worker": component},
        )


def test_duplicate_endpoint_id_is_plan_error() -> None:
    component = _component("com.example.worker", endpoints=("service",))
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "one": {
                            "component": "com.example.worker",
                            "instance_id": "one",
                            "endpoints": {"service": "shared"},
                        },
                        "two": {
                            "component": "com.example.worker",
                            "instance_id": "two",
                            "endpoints": {"service": "shared"},
                        },
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Duplicate Deckr endpoint id"):
        configured_component_instance_specs(
            document,
            definitions={"com.example.worker": component},
        )


def test_instance_source_generates_component_instances() -> None:
    component = _component("com.example.worker", endpoints=("service",))

    def load_source(
        context: ComponentInstanceSourceContext,
    ) -> tuple[ComponentInstanceDefinition, ...]:
        assert context.source_config["allow"] == ["worker"]
        return (
            ComponentInstanceDefinition(
                component_id="com.example.worker",
                instance_id="generated-worker",
                endpoints={"service": "worker-service"},
                config={"value": "from-source"},
            ),
        )

    source = ComponentInstanceSourceDefinition(
        source_id="com.example.worker.source",
        load=load_source,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instance_sources": [
                        {
                            "id": "worker_source",
                            "source": "com.example.worker.source",
                            "allow": ["worker"],
                        }
                    ]
                }
            }
        }
    )

    specs = configured_component_instance_specs(
        document,
        definitions={"com.example.worker": component},
        instance_source_definitions={"com.example.worker.source": source},
    )

    assert [(spec.instance_id, dict(spec.config)) for spec in specs] == [
        ("generated-worker", {"value": "from-source"})
    ]


@pytest.mark.asyncio
async def test_start_components_passes_lane_registry_to_component() -> None:
    seen: dict[str, object] = {}

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.example.action_runtime",
            consumes=("actions",),
            publishes=("actions",),
        ),
        factory=lambda context: (
            seen.setdefault("lane", context.require_lane("actions"))
            and _DummyComponent(name=context.runtime_name)
        ),
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "main": {
                            "component": "com.example.action_runtime",
                            "instance_id": "main",
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"com.example.action_runtime": definition},
    )
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        await component_host.component_manager.wait_for_state(
            "com.example.action_runtime:main",
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
async def test_start_components_passes_current_state_and_endpoints() -> None:
    seen: dict[str, object] = {}

    def factory(context):
        seen["state"] = context.state()
        seen["endpoint"] = context.require_endpoint_id("controller")
        return _DummyComponent(name=context.runtime_name)

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.k-si.deckr.controller",
            endpoint_slots=("controller",),
        ),
        factory=factory,
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "controller": {
                            "component": "com.k-si.deckr.controller",
                            "instance_id": "main",
                            "endpoints": {"controller": "controller-main"},
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"com.k-si.deckr.controller": definition},
    )
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan):
        assert seen["state"] is deckr.state()
        assert seen["endpoint"] == "controller-main"


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
                "components": {
                    "instances": {
                        "worker": {
                            "component": "acme.worker",
                            "instance_id": "main",
                        }
                    }
                },
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
