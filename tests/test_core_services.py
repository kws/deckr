from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from deckr.components import (
    BaseComponent,
    ComponentCardinality,
    ComponentDefinition,
    ComponentHostPlan,
    ComponentInstanceSpec,
    ComponentManifest,
    ResolvedLaneSet,
    configured_component_instance_specs,
    resolve_component_host_plan,
    resolve_component_instance_specs,
    start_components,
)
from deckr.contracts.lanes import DeliverySemantics, LaneContract, LaneRoutePolicy
from deckr.contracts.messages import (
    DeckrMessage,
    controller_address,
    controllers_broadcast,
    endpoint_address,
    endpoint_target,
    entity_subject,
    host_address,
    message_targets_endpoint,
)
from deckr.core.config import ConfigDocument
from deckr.hardware.messages import hardware_message_schema
from deckr.pluginhost.messages import (
    HOST_ONLINE,
    SettingsBody,
    plugin_body_dict,
    plugin_host_subject,
    plugin_message,
)
from deckr.runtime import Deckr
from deckr.transports.websocket import component as websocket_transport_component


def _core_wire_schemas() -> dict[str, dict]:
    return {
        "deckr.message.plugin_messages.v1": DeckrMessage.schema_dict(),
        "deckr.message.hardware_messages.v1": hardware_message_schema(),
    }


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
        route_expiry_interval=0.01,
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


def test_resolve_component_specs_do_not_inherit_parent_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        lambda component_id: host,
    )

    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {
                        "enabled": False,
                        "instances": {
                            "main": {},
                        },
                    }
                }
            }
        }
    )

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.plugin_hosts.python"],
    )

    assert len(specs) == 1
    assert dict(specs[0].raw_config) == {}


def test_resolve_component_specs_skip_unconfigured_singletons(
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
        "deckr.components._host.load_component_definition",
        lambda component_id: controller,
    )

    document = _document({"deckr": {}})

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller"],
    )

    assert specs == []


def test_configured_component_specs_rejects_uninstalled_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.plugin_hosts.python",
            config_prefix="deckr.plugin_hosts.python",
            cardinality=ComponentCardinality.MULTI_INSTANCE,
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )

    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.plugin_hosts.python"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: host,
    )

    document = _document(
        {
            "deckr": {
                "controller": {"id": "controller-main"},
                "drivers": {
                    "mirabox": {"manager_id": "mirabox-main"},
                },
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {"host_id": "python"},
                        }
                    }
                },
            }
        }
    )

    with pytest.raises(ValueError) as exc_info:
        configured_component_instance_specs(document)

    message = str(exc_info.value)
    assert "deckr.controller" in message
    assert "deckr.drivers.mirabox" in message


def test_configured_component_specs_rejects_uninstalled_multi_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deckr.components._host.available_component_ids", lambda: [])

    document = _document(
        {
            "deckr": {
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {"host_id": "python"},
                        }
                    }
                },
            }
        }
    )

    with pytest.raises(ValueError, match="deckr.plugin_hosts.python"):
        configured_component_instance_specs(document)


@pytest.mark.asyncio
async def test_start_components_provides_runtime_lanes_and_exact_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def controller_factory(context):
        seen["controller_config"] = dict(context.raw_config)
        seen["controller_has_document"] = hasattr(context, "document")
        seen["plugin_lane"] = context.require_lane("plugin_messages")
        seen["hardware_lane"] = context.require_lane("hardware_messages")
        return _DummyComponent(name=context.runtime_name)

    def host_factory(context):
        seen["host_config"] = dict(context.raw_config)
        seen["host_has_document"] = hasattr(context, "document")
        seen["host_lane"] = context.require_lane("plugin_messages")
        return _DummyComponent(name=context.runtime_name)

    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.controller", "deckr.plugin_hosts.python"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: {
            "deckr.controller": ComponentDefinition(
                manifest=ComponentManifest(
                    component_id="deckr.controller",
                    config_prefix="deckr.controller",
                    consumes=("hardware_messages", "plugin_messages"),
                    publishes=("plugin_messages",),
                ),
                factory=controller_factory,
            ),
            "deckr.plugin_hosts.python": ComponentDefinition(
                manifest=ComponentManifest(
                    component_id="deckr.plugin_hosts.python",
                    config_prefix="deckr.plugin_hosts.python",
                    consumes=("plugin_messages",),
                    publishes=("plugin_messages",),
                    cardinality=ComponentCardinality.MULTI_INSTANCE,
                ),
                factory=host_factory,
            ),
        }[component_id],
    )

    document = _document(
        {
            "deckr": {
                "controller": {"log_level": "debug"},
                "plugin_hosts": {
                    "python": {
                        "instances": {
                            "main": {"host_id": "python"},
                        }
                    }
                },
            }
        }
    )
    async with _running_components(document) as (result, _deckr):
        assert [component.name for component in result.components] == [
            "deckr.controller",
            "deckr.plugin_hosts.python:main",
        ]
        assert result.lane_names == ("hardware_messages", "plugin_messages")
        assert seen["controller_config"] == {"log_level": "debug"}
        assert seen["host_config"] == {"host_id": "python"}
        assert seen["controller_has_document"] is False
        assert seen["host_has_document"] is False
        assert seen["plugin_lane"] is seen["host_lane"]
        assert seen["plugin_lane"] is not None
        assert seen["hardware_lane"] is not None


@pytest.mark.asyncio
async def test_manual_component_definitions_use_same_hosting_path() -> None:
    seen: dict[str, object] = {}
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=("plugin_messages",),
        ),
        factory=lambda context: seen.setdefault(
            "component",
            _DummyComponent(name=context.runtime_name),
        ),
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})

    plan = resolve_component_host_plan(
        document,
        definitions={definition.manifest.component_id: definition},
    )
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        route_expiry_interval=0.01,
    ) as deckr, start_components(deckr, plan) as host:
        assert [component.name for component in host.components] == [
            "deckr.acme.metrics"
        ]


@pytest.mark.asyncio
async def test_manual_component_specs_use_same_hosting_path() -> None:
    seen: dict[str, object] = {}

    def factory(context):
        seen["config"] = dict(context.raw_config)
        return _DummyComponent(name=context.runtime_name)

    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.manual.component",
            config_prefix="deckr.manual.component",
            consumes=("plugin_messages",),
        ),
        factory=factory,
    )
    spec = ComponentInstanceSpec(
        component_id=definition.manifest.component_id,
        instance_id="default",
        runtime_name="deckr.manual.component",
        raw_config={"value": 1},
        definition=definition,
        lanes=ResolvedLaneSet(consumes=("plugin_messages",)),
    )
    plan = ComponentHostPlan.from_specs((spec,), base_dir=Path.cwd())

    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        route_expiry_interval=0.01,
    ) as deckr, start_components(deckr, plan) as host:
        assert [component.name for component in host.components] == [
            "deckr.manual.component"
        ]
        assert seen["config"] == {"value": 1}


@pytest.mark.asyncio
async def test_component_host_rejects_mismatched_runtime_contract() -> None:
    lane = "acme.metrics.events"
    plan_contract = LaneContract(lane=lane, schema_id="acme.metrics.events.v1")
    runtime_contract = LaneContract(lane=lane, schema_id="acme.metrics.events.v2")
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            lane_contracts=(plan_contract,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    plan = resolve_component_host_plan(
        document,
        definitions={definition.manifest.component_id: definition},
    )

    async with Deckr(
        lane_contracts=(runtime_contract,),
        lanes=(lane,),
        route_expiry_interval=0.01,
    ) as deckr:
        with pytest.raises(ValueError, match="does not match"):
            async with start_components(deckr, plan):
                pass


@pytest.mark.asyncio
async def test_component_host_requires_entered_deckr_runtime() -> None:
    plan = ComponentHostPlan.from_specs((), base_dir=Path.cwd())
    deckr = Deckr(lane_contracts=plan.lane_contracts, lanes=plan.lane_names)

    with pytest.raises(RuntimeError, match="must be entered"):
        async with start_components(deckr, plan):
            pass


@pytest.mark.asyncio
async def test_component_host_plan_uses_manifest_lane_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            publishes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    async with _running_components(document) as (result, _deckr):
        bus = result.get_lane(lane)
        assert bus is not None
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_worker", "one"),
            lane=lane,
            client_id="websocket:worker",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )

        assert accepted is not None


@pytest.mark.asyncio
async def test_duplicate_extension_lane_contracts_must_be_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    contract = LaneContract(
        lane=lane,
        schema_id="acme.metrics.events.v1",
        route_policy=LaneRoutePolicy(
            remote_claim_endpoint_families=frozenset({"acme_worker"}),
        ),
    )
    first = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics.one",
            config_prefix="deckr.acme.metrics.one",
            consumes=(lane,),
            lane_contracts=(contract,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    second = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics.two",
            config_prefix="deckr.acme.metrics.two",
            publishes=(lane,),
            lane_contracts=(contract,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    definitions = {
        first.manifest.component_id: first,
        second.manifest.component_id: second,
    }
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definitions[component_id],
    )
    document = _document(
        {
            "deckr": {
                "acme": {
                    "metrics": {
                        "one": {"enabled": True},
                        "two": {"enabled": True},
                    }
                }
            }
        }
    )
    async with _running_components(document) as (result, _deckr):
        assert result.get_lane(lane) is not None


@pytest.mark.asyncio
async def test_conflicting_duplicate_extension_lane_contracts_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    first = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics.one",
            config_prefix="deckr.acme.metrics.one",
            consumes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    second = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics.two",
            config_prefix="deckr.acme.metrics.two",
            publishes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v2",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    definitions = {
        first.manifest.component_id: first,
        second.manifest.component_id: second,
    }
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definitions[component_id],
    )
    document = _document(
        {
            "deckr": {
                "acme": {
                    "metrics": {
                        "one": {"enabled": True},
                        "two": {"enabled": True},
                    }
                }
            }
        }
    )
    with pytest.raises(ValueError, match="Duplicate lane contract"):
        resolve_component_host_plan(document)


@pytest.mark.asyncio
async def test_unsupported_lane_delivery_profile_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    delivery=DeliverySemantics(guarantee="at_least_once"),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    with pytest.raises(ValueError, match="only at-most-once"):
        resolve_component_host_plan(document)


@pytest.mark.asyncio
async def test_unsupported_deployment_delivery_profile_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document(
        {
            "deckr": {
                "acme": {"metrics": {"enabled": True}},
                "lane_contracts": {
                    lane: {
                        "schema_id": "acme.metrics.events.v1",
                        "delivery": {
                            "replay": "broker",
                        },
                    }
                },
            }
        }
    )
    with pytest.raises(ValueError, match="replay delivery is not implemented"):
        resolve_component_host_plan(document)


@pytest.mark.asyncio
async def test_extension_transport_binding_schema_must_match_resolved_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    metrics = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    definitions = {
        "deckr.acme.metrics": metrics,
        "deckr.transports.websocket": websocket_transport_component,
    }
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definitions[component_id],
    )
    document = _document(
        {
            "deckr": {
                "acme": {"metrics": {"enabled": True}},
                "transports": {
                    "websocket": {
                        "instances": {
                            "main": {
                                "mode": "server",
                                "bindings": {
                                    "metrics": {
                                        "lane": lane,
                                        "schema_id": "acme.metrics.events.v2",
                                        "path": "/metrics",
                                    }
                                },
                            }
                        }
                    }
                },
            }
        }
    )
    with pytest.raises(ValueError, match="must match resolved lane contract"):
        resolve_component_host_plan(document)


@pytest.mark.asyncio
async def test_extension_transport_binding_requires_resolved_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.transports.websocket"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: websocket_transport_component,
    )
    document = _document(
        {
            "deckr": {
                "transports": {
                    "websocket": {
                        "instances": {
                            "main": {
                                "mode": "server",
                                "bindings": {
                                    "metrics": {
                                        "lane": lane,
                                        "schema_id": "acme.metrics.events.v1",
                                        "path": "/metrics",
                                    }
                                },
                            }
                        }
                    }
                }
            }
        }
    )
    with pytest.raises(ValueError, match="no resolved lane contract"):
        resolve_component_host_plan(document)


@pytest.mark.asyncio
async def test_deployment_lane_contract_enables_extension_route_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            publishes=(lane,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document(
        {
            "deckr": {
                "acme": {"metrics": {"enabled": True}},
                "lane_contracts": {
                    lane: {
                        "schema_id": "acme.metrics.events.v1",
                        "route_policy": {
                            "remote_claim_endpoint_families": ["acme_worker"],
                        },
                    }
                },
            }
        }
    )
    async with _running_components(document) as (result, _deckr):
        bus = result.get_lane(lane)
        assert bus is not None
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_worker", "one"),
            lane=lane,
            client_id="websocket:worker",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )

        assert accepted is not None


@pytest.mark.asyncio
async def test_deployment_lane_contract_can_narrow_manifest_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            publishes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset(
                            {"acme_worker", "acme_observer"}
                        ),
                        allowed_sender_families=frozenset(
                            {"acme_worker", "acme_observer"}
                        ),
                        allowed_recipient_families=frozenset(
                            {"acme_worker", "acme_observer"}
                        ),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document(
        {
            "deckr": {
                "acme": {"metrics": {"enabled": True}},
                "lane_contracts": {
                    lane: {
                        "route_policy": {
                            "remote_claim_endpoint_families": ["acme_worker"],
                            "allowed_sender_families": ["acme_worker"],
                            "allowed_recipient_families": ["acme_worker"],
                        },
                    }
                },
            }
        }
    )
    async with _running_components(document) as (result, _deckr):
        bus = result.get_lane(lane)
        assert bus is not None
        accepted = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_worker", "one"),
            lane=lane,
            client_id="websocket:worker",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )
        rejected = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_observer", "one"),
            lane=lane,
            client_id="websocket:observer",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )

        assert accepted is not None
        assert rejected is None


@pytest.mark.asyncio
async def test_deployment_lane_contract_must_not_widen_manifest_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            publishes=(lane,),
            lane_contracts=(
                LaneContract(
                    lane=lane,
                    schema_id="acme.metrics.events.v1",
                    route_policy=LaneRoutePolicy(
                        remote_claim_endpoint_families=frozenset({"acme_worker"}),
                    ),
                ),
            ),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document(
        {
            "deckr": {
                "acme": {"metrics": {"enabled": True}},
                "lane_contracts": {
                    lane: {
                        "route_policy": {
                            "remote_claim_endpoint_families": [
                                "acme_worker",
                                "acme_observer",
                            ],
                        },
                    }
                },
            }
        }
    )
    with pytest.raises(ValueError, match="must not widen"):
        resolve_component_host_plan(document)


def test_extension_lane_without_contract_rejects_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    definition = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.acme.metrics",
            config_prefix="deckr.acme.metrics",
            consumes=(lane,),
            publishes=(lane,),
        ),
        factory=lambda context: _DummyComponent(name=context.runtime_name),
    )
    monkeypatch.setattr(
        "deckr.components._host.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.components._host.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    plan = resolve_component_host_plan(document)

    with pytest.raises(ValueError, match="require lane contracts"):
        Deckr(lane_contracts=plan.lane_contracts, lanes=plan.lane_names)


def test_deployment_lane_contract_registers_extension_lane_without_component() -> None:
    lane = "acme.metrics.events"
    document = _document(
        {
            "deckr": {
                "lane_contracts": {
                    lane: {
                        "schema_id": "acme.metrics.events.v1",
                        "route_policy": {
                            "remote_claim_endpoint_families": ["acme_worker"],
                        },
                    }
                }
            }
        }
    )

    plan = resolve_component_host_plan(document, definitions={})
    deckr = Deckr(lane_contracts=plan.lane_contracts, lanes=plan.lane_names)

    assert lane in plan.lane_names
    assert deckr.bus(lane).lane == lane
    assert deckr.lane_contracts.contract_for(lane).schema_id == (
        "acme.metrics.events.v1"
    )


def _mode_component_definitions() -> dict[str, ComponentDefinition]:
    def factory(context):
        return _DummyComponent(name=context.runtime_name)

    def transport_lanes(
        *,
        manifest,
        raw_config,
        instance_id,
    ) -> ResolvedLaneSet:
        del manifest, instance_id
        lanes = tuple(
            sorted(
                str(binding["lane"])
                for binding in raw_config.get("bindings", {}).values()
            )
        )
        return ResolvedLaneSet(consumes=lanes, publishes=lanes)

    definitions = [
        ComponentDefinition(
            manifest=ComponentManifest(
                component_id="deckr.controller",
                config_prefix="deckr.controller",
                consumes=("hardware_messages", "plugin_messages"),
                publishes=("hardware_messages", "plugin_messages"),
            ),
            factory=factory,
        ),
        ComponentDefinition(
            manifest=ComponentManifest(
                component_id="deckr.plugin_hosts.python",
                config_prefix="deckr.plugin_hosts.python",
                consumes=("plugin_messages",),
                publishes=("plugin_messages",),
                cardinality=ComponentCardinality.MULTI_INSTANCE,
            ),
            factory=factory,
        ),
        ComponentDefinition(
            manifest=ComponentManifest(
                component_id="deckr.drivers.elgato",
                config_prefix="deckr.drivers.elgato",
                consumes=("hardware_messages",),
                publishes=("hardware_messages",),
            ),
            factory=factory,
        ),
        ComponentDefinition(
            manifest=ComponentManifest(
                component_id="deckr.transports.mqtt",
                config_prefix="deckr.transports.mqtt",
                cardinality=ComponentCardinality.MULTI_INSTANCE,
            ),
            factory=factory,
            resolve_lanes=transport_lanes,
        ),
    ]
    return {definition.manifest.component_id: definition for definition in definitions}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "component_names"),
    [
        (
            _document(
                {
                    "deckr": {
                        "controller": {"id": "controller-main"},
                        "plugin_hosts": {"python": {"instances": {"main": {}}}},
                        "drivers": {"elgato": {"manager_id": "desk"}},
                        "transports": {
                            "mqtt": {
                                "instances": {
                                    "main": {
                                        "bindings": {
                                            "plugin": {"lane": "plugin_messages"},
                                            "hardware": {"lane": "hardware_messages"},
                                        }
                                    }
                                }
                            }
                        },
                    }
                }
            ),
            {
                "deckr.controller",
                "deckr.plugin_hosts.python:main",
                "deckr.drivers.elgato",
                "deckr.transports.mqtt:main",
            },
        ),
        (
            _document(
                {
                    "deckr": {
                        "plugin_hosts": {"python": {"instances": {"main": {}}}},
                        "transports": {
                            "mqtt": {
                                "instances": {
                                    "plugin": {
                                        "bindings": {
                                            "plugin": {"lane": "plugin_messages"},
                                        }
                                    }
                                }
                            }
                        },
                    }
                }
            ),
            {"deckr.plugin_hosts.python:main", "deckr.transports.mqtt:plugin"},
        ),
        (
            _document(
                {
                    "deckr": {
                        "drivers": {"elgato": {"manager_id": "desk"}},
                        "transports": {
                            "mqtt": {
                                "instances": {
                                    "hardware": {
                                        "bindings": {
                                            "hardware": {"lane": "hardware_messages"},
                                        }
                                    }
                                }
                            }
                        },
                    }
                }
            ),
            {"deckr.drivers.elgato", "deckr.transports.mqtt:hardware"},
        ),
    ],
)
async def test_runtime_modes_are_ordinary_component_composition(
    document: ConfigDocument,
    component_names: set[str],
) -> None:
    plan = resolve_component_host_plan(
        document,
        definitions=_mode_component_definitions(),
    )

    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        route_expiry_interval=0.01,
    ) as deckr, start_components(deckr, plan) as host:
        assert {component.name for component in host.components} == component_names
        assert set(deckr.lanes.names) == {"hardware_messages", "plugin_messages"}


@pytest.mark.asyncio
async def test_embedded_manual_runtime_uses_deckr_without_discovery() -> None:
    async with Deckr(route_expiry_interval=0.01) as deckr:
        client_id = await deckr.bus("plugin_messages").claim_local_endpoint(
            host_address("embedded")
        )

        assert client_id == "local:plugin_messages:host:embedded"


def test_deckr_message_is_pydantic_and_schema_exportable() -> None:
    message = plugin_message(
        sender=host_address("python"),
        recipient=controllers_broadcast(),
        message_type=HOST_ONLINE,
        body={},
        subject=plugin_host_subject("python"),
    )

    payload = message.to_dict()

    assert payload["sender"] == "host:python"
    assert payload["recipient"] == {
        "targetType": "broadcast",
        "scope": "controllers",
        "endpointFamily": "controller",
    }
    assert payload["messageId"]
    assert payload["protocolVersion"] == "1"
    assert payload["lane"] == "plugin_messages"
    assert payload["messageType"] == HOST_ONLINE
    assert DeckrMessage.from_dict(payload) == message
    assert plugin_body_dict(message) == {}

    schemas = _core_wire_schemas()
    assert "deckr.message.plugin_messages.v1" in schemas
    assert "deckr.message.hardware_messages.v1" in schemas


def test_plugin_body_settings_are_immutable_json() -> None:
    source_payload = {"value": {"nested": [1]}}
    body = SettingsBody(settings=source_payload)

    source_payload["value"] = {"nested": [2]}

    assert body.settings == {"value": {"nested": (1,)}}
    with pytest.raises(TypeError):
        body.settings["added"] = True  # type: ignore[index]
    assert body.to_dict()["settings"] == {"value": {"nested": [1]}}


def test_deckr_message_routing_uses_targets_not_legacy_strings() -> None:
    controller_message = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("controller-main"),
        message_type=HOST_ONLINE,
        body={},
        subject=entity_subject("plugin_host", hostId="python"),
    )
    assert message_targets_endpoint(
        controller_message, controller_address("controller-main")
    )
    assert not message_targets_endpoint(
        controller_message, controller_address("controller-other")
    )

    host_message = plugin_message(
        sender=controller_address("controller-main"),
        recipient=endpoint_target(host_address("python")),
        message_type="requestActions",
        body={},
        subject=entity_subject("plugin_actions"),
    )
    assert message_targets_endpoint(host_message, host_address("python"))

    with pytest.raises(ValueError):
        endpoint_target("python")


def test_context_id_routing_extractors_are_removed() -> None:
    from deckr.pluginhost import messages

    assert not hasattr(messages, "extract_device_id")
    assert not hasattr(messages, "extract_slot_id")
    assert not hasattr(messages, "extract_controller_id")
