from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from message_bus_mocks import mock_deckr

from deckr.components import (
    BaseComponent,
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
from deckr.contracts.lanes import MessageContract
from deckr.contracts.messages import entity_subject
from deckr.core.config import ConfigDocument
from deckr.lanes import Lane
from deckr.launcher import build_runtime_substrate
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.nats_kv import KvBucketPolicy


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


class _EndpointContextComponent(BaseComponent):
    def __init__(self, name: str, context, seen: dict[str, object]) -> None:
        super().__init__(name=name)
        self._context = context
        self._seen = seen

    async def start(self, ctx) -> None:
        ctx.start_task(self._run, ctx)

    async def stop(self) -> None:
        return

    async def _run(self, ctx) -> None:
        async with self._context.open_endpoint(
            "controller",
            session_id="controller-session",
            metadata={"custom": "value"},
        ) as endpoint:
            self._seen["endpoint"] = endpoint
            self._seen["endpoint_address"] = endpoint.address
            self._seen["endpoint_metadata"] = endpoint.metadata
            await ctx.report_ready()
            await ctx.stopping.wait()


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


@asynccontextmanager
async def _running_components(document: ConfigDocument):
    plan = resolve_component_host_plan(document)
    async with mock_deckr(
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
    lane_contracts: tuple[MessageContract, ...] = (),
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


def test_component_dependencies_are_unknown_component_fields() -> None:
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
                                    "kind": "feature",
                                    "mode": "required",
                                    "feature_id": "dev.deckr.sonos.service",
                                    "endpoint": "service:sonos-home",
                                    "provider_id": "not-generic",
                                }
                            },
                        }
                    }
                }
            }
        }
    )

    with pytest.raises(
        ValueError,
        match=(
            r"Unknown component instance field\(s\) in "
            r"deckr\.components\.instances\.worker: dependencies"
        ),
    ):
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
    async with mock_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        await component_host.component_manager.wait_for_state(
            "com.example.action_runtime:main",
            ComponentState.RUNNING,
        )
        async with deckr.endpoint("controller:main") as controller:
            await controller.send(
                lane="actions",
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
async def test_start_components_passes_kv_bucket_and_endpoints() -> None:
    seen: dict[str, object] = {}
    policy = KvBucketPolicy(bucket="test_component_context_v1", ttl_seconds=None)

    def factory(context):
        seen["bucket"] = context.kv_bucket(policy)
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
    async with mock_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan):
        assert seen["bucket"] is deckr.kv_bucket(policy)
        assert seen["endpoint"] == "controller-main"


@pytest.mark.asyncio
async def test_start_components_passes_slot_endpoint_opener_and_protocols() -> None:
    seen: dict[str, object] = {}

    def factory(context):
        seen["slot_address"] = context.endpoint_address("controller")
        seen["beacon"] = context.require_beacon()
        seen["concord"] = context.require_concord()
        with pytest.raises(KeyError):
            context.open_endpoint("service")
        return _EndpointContextComponent(context.runtime_name, context, seen)

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

    async with mock_deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan) as component_host:
        await _wait_for_readiness(
            component_host.component_manager,
            "dev.deckr.controller:main",
            ReadinessState.READY,
        )
        assert seen["beacon"] is deckr.beacon
        assert seen["concord"] is deckr.concord
        assert str(seen["slot_address"]) == "controller:controller-main"
        assert str(seen["endpoint_address"]) == "controller:controller-main"
        assert seen["endpoint_metadata"] == {
            "componentId": "dev.deckr.controller",
            "instanceId": "main",
            "runtimeName": "dev.deckr.controller:main",
            "endpointSlot": "controller",
            "custom": "value",
        }


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
                MessageContract(
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


@pytest.mark.parametrize(
    "field",
    [
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
