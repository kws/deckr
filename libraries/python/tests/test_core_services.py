from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from memory_lane_substrate import memory_deckr

from deckr.components import (
    BaseComponent,
    ComponentCardinality,
    ComponentDefinition,
    ComponentInstanceDefinition,
    ComponentInstanceSourceContext,
    ComponentInstanceSourceDefinition,
    ComponentManifest,
    ComponentState,
    ReadinessState,
    configured_component_instance_specs,
    resolve_component_host_plan,
    start_components,
)
from deckr.contracts.lanes import LaneContract
from deckr.contracts.messages import SERVICES_LANE, entity_subject, service_address
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.launcher import build_runtime_substrate
from deckr.services.state import (
    ServiceCatalog,
    ServiceStatus,
    ServiceStatusValue,
    service_catalog_key,
    service_status_key,
)
from deckr.substrates.nats import NatsSubstrate


class _DummyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


class _ReadyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        await ctx.report_ready()

    async def stop(self) -> None:
        return


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def _now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def _running_components(document: ConfigDocument):
    plan = resolve_component_host_plan(document)
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        yield component_host, deckr


async def _wait_for_readiness(
    manager,
    runtime_name: str,
    readiness: ReadinessState,
):
    with anyio.fail_after(3):
        while True:
            status = manager.get_component_status(runtime_name)
            if status is not None and status.readiness_state == readiness:
                return status
            await anyio.sleep(0.05)


async def _wait_for_status(manager, runtime_name: str, predicate):
    with anyio.fail_after(3):
        while True:
            status = manager.get_component_status(runtime_name)
            if status is not None and predicate(status):
                return status
            await anyio.sleep(0.05)


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
        "dev.deckr.controller",
        consumes=("hardware_messages", "actions"),
        publishes=("actions",),
        endpoints=("controller",),
    )
    provider_runtime = _component(
        "dev.deckr.action_provider_runtime.python",
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
                            "component": "dev.deckr.controller",
                            "instance_id": "main",
                            "endpoints": {"controller": "controller-main"},
                            "config": {"log_level": "debug"},
                        },
                        "python_clock": {
                            "component": (
                                "dev.deckr.action_provider_runtime.python"
                            ),
                            "instance_id": "clock-main",
                            "endpoints": {"action_provider": "python-dev.deckr.clock"},
                            "config": {"provider_id": "dev.deckr.clock"},
                        },
                    }
                }
            }
        }
    )

    specs = configured_component_instance_specs(
        document,
        definitions={
            "dev.deckr.controller": controller,
            "dev.deckr.action_provider_runtime.python": provider_runtime,
        },
    )

    assert [
        (spec.component_id, spec.instance_id, dict(spec.config), spec.runtime_name)
        for spec in specs
    ] == [
        (
            "dev.deckr.controller",
            "main",
            {"log_level": "debug"},
            "dev.deckr.controller:main",
        ),
        (
            "dev.deckr.action_provider_runtime.python",
            "clock-main",
            {"provider_id": "dev.deckr.clock"},
            "dev.deckr.action_provider_runtime.python:clock-main",
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


def test_component_dependencies_are_planned_from_generic_wrapper() -> None:
    component = _component("com.example.worker")
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "worker": {
                            "component": "com.example.worker",
                            "instance_id": "worker",
                            "dependencies": {
                                "sonos_home": {
                                    "kind": "service",
                                    "mode": "required",
                                    "endpoint": "service:sonos-home",
                                    "namespace": "dev.deckr.sonos.service",
                                },
                                "controller_main": {
                                    "kind": "endpoint",
                                    "mode": "observed",
                                    "lane": "actions",
                                    "endpoint": "controller:controller-main",
                                },
                            },
                        }
                    }
                }
            }
        }
    )

    specs = configured_component_instance_specs(
        document,
        definitions={"com.example.worker": component},
    )

    dependencies = specs[0].dependencies
    assert sorted(dependencies) == ["controller_main", "sonos_home"]
    assert dependencies["sonos_home"].lane == "services"
    assert dependencies["sonos_home"].endpoint == service_address("sonos-home")
    assert dependencies["sonos_home"].namespace == "dev.deckr.sonos.service"


def test_component_dependencies_reject_unknown_fields() -> None:
    component = _component("com.example.worker")
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "worker": {
                            "component": "com.example.worker",
                            "instance_id": "worker",
                            "dependencies": {
                                "sonos_home": {
                                    "kind": "service",
                                    "mode": "required",
                                    "endpoint": "service:sonos-home",
                                    "namespace": "dev.deckr.sonos.service",
                                    "provider_id": "not-generic",
                                }
                            },
                        }
                    }
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Unknown dependency field"):
        configured_component_instance_specs(
            document,
            definitions={"com.example.worker": component},
        )


def test_presence_dependency_cycles_are_reported_not_rejected() -> None:
    worker = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="com.example.worker",
            endpoint_slots=("service",),
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "one": {
                            "component": "com.example.worker",
                            "instance_id": "one",
                            "endpoints": {"service": "one"},
                            "dependencies": {
                                "two": {
                                    "kind": "service",
                                    "mode": "required",
                                    "endpoint": "service:two",
                                    "namespace": "com.example.worker",
                                }
                            },
                        },
                        "two": {
                            "component": "com.example.worker",
                            "instance_id": "two",
                            "endpoints": {"service": "two"},
                            "dependencies": {
                                "one": {
                                    "kind": "service",
                                    "mode": "required",
                                    "endpoint": "service:one",
                                    "namespace": "com.example.worker",
                                }
                            },
                        },
                    }
                }
            }
        }
    )

    plan = resolve_component_host_plan(
        document,
        definitions={"com.example.worker": worker},
    )

    messages = [event.message for event in plan.report.events]
    assert any("presence dependency cycle" in message for message in messages)


def test_instance_source_generates_component_instances() -> None:
    component = _component("com.example.worker", endpoints=("service",))

    def load_source(
        context: ComponentInstanceSourceContext,
    ) -> tuple[ComponentInstanceDefinition, ...]:
        assert context.source_config["allow"] == ["worker"]
        context.report(
            "selected worker",
            component_id="com.example.worker",
            instance_id="generated-worker",
        )
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
    plan = resolve_component_host_plan(
        document,
        definitions={"com.example.worker": component},
        instance_source_definitions={"com.example.worker.source": source},
    )
    assert any(
        event.source_id == "com.example.worker.source"
        and event.component_id == "com.example.worker"
        and event.instance_id == "generated-worker"
        and event.message == "selected worker"
        for event in plan.report.events
    )


def test_duplicate_instance_source_declaration_id_is_plan_error() -> None:
    source = ComponentInstanceSourceDefinition(
        source_id="com.example.worker.source",
        load=lambda context: (),
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instance_sources": [
                        {
                            "id": "workers",
                            "source": "com.example.worker.source",
                        },
                        {
                            "id": "workers",
                            "source": "com.example.worker.source",
                        },
                    ]
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Duplicate Deckr component instance source"):
        resolve_component_host_plan(
            document,
            definitions={},
            instance_source_definitions={"com.example.worker.source": source},
        )


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
            component_id="dev.deckr.controller",
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
                            "component": "dev.deckr.controller",
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
        definitions={"dev.deckr.controller": definition},
    )
    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan):
        assert seen["state"] is deckr.state()
        assert seen["endpoint"] == "controller-main"


@pytest.mark.asyncio
async def test_required_service_dependency_controls_effective_readiness() -> None:
    definition = ComponentDefinition(
        manifest=ComponentManifest(component_id="com.example.worker"),
        factory=lambda context: _ReadyComponent(name=context.runtime_name),
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "worker": {
                            "component": "com.example.worker",
                            "instance_id": "worker",
                            "dependencies": {
                                "sonos_home": {
                                    "kind": "service",
                                    "mode": "required",
                                    "endpoint": "service:sonos-home",
                                    "namespace": "dev.deckr.sonos.service",
                                }
                            },
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"com.example.worker": definition},
    )

    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        manager = component_host.component_manager
        unready = await _wait_for_readiness(
            manager,
            "com.example.worker:worker",
            ReadinessState.UNREADY,
        )
        assert unready.readiness_reasons == ("dependency.sonos_home.unsatisfied",)

        endpoint = service_address("sonos-home")
        async with deckr.lane(SERVICES_LANE).register_endpoint(endpoint) as service:
            discovery = deckr.state("deckr_discovery_v1")
            await discovery.put(
                service_catalog_key("sonos-home"),
                ServiceCatalog(
                    serviceId="sonos-home",
                    serviceEndpoint=endpoint,
                    serviceNamespace="dev.deckr.sonos.service",
                    sessionId=service.session_id,
                    supportedOperations=("play",),
                    timestamp=_now(),
                ),
            )
            await discovery.put(
                service_status_key("sonos-home"),
                ServiceStatus(
                    serviceId="sonos-home",
                    serviceEndpoint=endpoint,
                    serviceNamespace="dev.deckr.sonos.service",
                    sessionId=service.session_id,
                    status=ServiceStatusValue.AVAILABLE,
                    timestamp=_now(),
                ),
            )

            ready = await _wait_for_readiness(
                manager,
                "com.example.worker:worker",
                ReadinessState.READY,
            )
            assert ready.readiness_reasons == ()

            await discovery.put(
                service_status_key("sonos-home"),
                ServiceStatus(
                    serviceId="sonos-home",
                    serviceEndpoint=endpoint,
                    serviceNamespace="dev.deckr.sonos.service",
                    sessionId=service.session_id,
                    status=ServiceStatusValue.DEGRADED,
                    timestamp=_now(),
                ),
            )
            degraded = await _wait_for_readiness(
                manager,
                "com.example.worker:worker",
                ReadinessState.UNREADY,
            )
            assert degraded.readiness_reasons == ("dependency.sonos_home.degraded",)

        lost = await _wait_for_status(
            manager,
            "com.example.worker:worker",
            lambda item: item.readiness_state == ReadinessState.UNREADY
            and item.readiness_reasons == ("dependency.sonos_home.unsatisfied",),
        )
        assert lost.readiness_reasons == ("dependency.sonos_home.unsatisfied",)


@pytest.mark.asyncio
async def test_optional_service_dependency_reports_without_blocking_readiness() -> None:
    definition = ComponentDefinition(
        manifest=ComponentManifest(component_id="com.example.worker"),
        factory=lambda context: _ReadyComponent(name=context.runtime_name),
    )
    document = _document(
        {
            "deckr": {
                "components": {
                    "instances": {
                        "worker": {
                            "component": "com.example.worker",
                            "instance_id": "worker",
                            "dependencies": {
                                "sonos_home": {
                                    "kind": "service",
                                    "mode": "optional",
                                    "endpoint": "service:sonos-home",
                                    "namespace": "dev.deckr.sonos.service",
                                }
                            },
                        }
                    }
                }
            }
        }
    )
    plan = resolve_component_host_plan(
        document,
        definitions={"com.example.worker": definition},
    )

    async with memory_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        del deckr
        status = await _wait_for_status(
            component_host.component_manager,
            "com.example.worker:worker",
            lambda item: item.readiness_state == ReadinessState.READY
            and "dependencies" in item.diagnostics,
        )

    dependency = status.diagnostics["dependencies"]["sonos_home"]
    assert dependency["mode"] == "optional"
    assert dependency["state"] == "unsatisfied"


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
