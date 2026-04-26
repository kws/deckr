from __future__ import annotations

from pathlib import Path

import anyio
import pytest

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
from deckr.core.component import BaseComponent, ComponentManager
from deckr.core.components import (
    ComponentActivationResult,
    ComponentCardinality,
    ComponentDefinition,
    ComponentManifest,
    activate_components,
    resolve_component_instance_specs,
)
from deckr.core.config import ConfigDocument
from deckr.hardware.events import hardware_message_schema
from deckr.pluginhost.messages import (
    HOST_ONLINE,
    PluginMessageBody,
    plugin_host_subject,
    plugin_message,
    plugin_payload,
)
from deckr.transports.websocket import component as websocket_transport_component


def _core_wire_schemas() -> dict[str, dict]:
    return {
        "deckr.message.plugin_messages.v1": DeckrMessage.schema_dict(),
        "deckr.message.hardware_events.v1": hardware_message_schema(),
    }


class _DummyComponent(BaseComponent):
    async def start(self, ctx) -> None:
        return

    async def stop(self) -> None:
        return


def _document(raw: dict) -> ConfigDocument:
    return ConfigDocument(raw=raw, source_path=None, base_dir=Path.cwd())


def test_resolve_component_specs_includes_singleton_and_multi_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ComponentDefinition(
        manifest=ComponentManifest(
            component_id="deckr.controller",
            config_prefix="deckr.controller",
            consumes=("hardware_events", "plugin_messages"),
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
        "deckr.core.components.load_component_definition",
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
        "deckr.core.components.load_component_definition",
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
        "deckr.core.components.load_component_definition",
        lambda component_id: controller,
    )

    document = _document({"deckr": {}})

    specs = resolve_component_instance_specs(
        document,
        discovered_component_ids=["deckr.controller"],
    )

    assert specs == []


@pytest.mark.asyncio
async def test_activate_components_provides_prebuilt_lanes_and_exact_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def controller_factory(context):
        seen["controller_config"] = dict(context.raw_config)
        seen["controller_has_document"] = hasattr(context, "document")
        seen["plugin_lane"] = context.require_lane("plugin_messages")
        seen["hardware_lane"] = context.require_lane("hardware_events")
        return _DummyComponent(name=context.runtime_name)

    def host_factory(context):
        seen["host_config"] = dict(context.raw_config)
        seen["host_has_document"] = hasattr(context, "document")
        seen["host_lane"] = context.require_lane("plugin_messages")
        return _DummyComponent(name=context.runtime_name)

    monkeypatch.setattr(
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.controller", "deckr.plugin_hosts.python"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: {
            "deckr.controller": ComponentDefinition(
                manifest=ComponentManifest(
                    component_id="deckr.controller",
                    config_prefix="deckr.controller",
                    consumes=("hardware_events", "plugin_messages"),
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result: ComponentActivationResult = await activate_components(
            document,
            component_manager,
        )

        assert [component.name for component in result.components] == [
            "deckr.controller",
            "deckr.plugin_hosts.python:main",
        ]
        assert result.lane_names == ("hardware_events", "plugin_messages")
        assert seen["controller_config"] == {"log_level": "debug"}
        assert seen["host_config"] == {"host_id": "python"}
        assert seen["controller_has_document"] is False
        assert seen["host_has_document"] is False
        assert seen["plugin_lane"] is seen["host_lane"]
        assert seen["plugin_lane"] is not None
        assert seen["hardware_lane"] is not None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_activate_components_uses_manifest_lane_contracts(
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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)
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
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)
        assert result.get_lane(lane) is not None
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="Duplicate lane contract"):
            await activate_components(document, component_manager)
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="only at-most-once"):
            await activate_components(document, component_manager)
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="replay delivery is not implemented"):
            await activate_components(document, component_manager)
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: sorted(definitions),
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="must match resolved lane contract"):
            await activate_components(document, component_manager)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_extension_transport_binding_requires_resolved_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = "acme.metrics.events"
    monkeypatch.setattr(
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.transports.websocket"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="no resolved lane contract"):
            await activate_components(document, component_manager)
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)
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
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)
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
        tg.cancel_scope.cancel()


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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
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
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        with pytest.raises(ValueError, match="must not widen"):
            await activate_components(document, component_manager)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_extension_lane_without_contract_rejects_remote_claim_in_runtime(
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
        "deckr.core.components.available_component_ids",
        lambda: ["deckr.acme.metrics"],
    )
    monkeypatch.setattr(
        "deckr.core.components.load_component_definition",
        lambda component_id: definition,
    )
    document = _document({"deckr": {"acme": {"metrics": {"enabled": True}}}})
    component_manager = ComponentManager()

    async with anyio.create_task_group() as tg:
        tg.start_soon(component_manager.run)
        await anyio.sleep(0.01)

        result = await activate_components(document, component_manager)
        bus = result.get_lane(lane)
        assert bus is not None
        rejected = await bus.route_table.claim_endpoint(
            endpoint=endpoint_address("acme_worker", "one"),
            lane=lane,
            client_id="websocket:worker",
            client_kind="remote",
            transport_kind="websocket",
            transport_id="ws-main",
            claim_source="message_sender",
        )

        assert rejected is None
        tg.cancel_scope.cancel()


def test_deckr_message_is_pydantic_and_schema_exportable() -> None:
    message = plugin_message(
        sender=host_address("python"),
        recipient=controllers_broadcast(),
        message_type=HOST_ONLINE,
        payload={"hostId": "python"},
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
    assert plugin_payload(message) == {"hostId": "python"}

    schemas = _core_wire_schemas()
    assert "deckr.message.plugin_messages.v1" in schemas
    assert "deckr.message.hardware_events.v1" in schemas


def test_plugin_body_payload_is_immutable_json() -> None:
    source_payload = {"value": {"nested": [1]}}
    body = PluginMessageBody(payload=source_payload)

    source_payload["value"] = {"nested": [2]}

    assert body.payload == {"value": {"nested": (1,)}}
    with pytest.raises(TypeError):
        body.payload["added"] = True  # type: ignore[index]
    assert body.to_dict()["payload"] == {"value": {"nested": [1]}}


def test_deckr_message_routing_uses_targets_not_legacy_strings() -> None:
    controller_message = plugin_message(
        sender=host_address("python"),
        recipient=controller_address("controller-main"),
        message_type=HOST_ONLINE,
        payload={"hostId": "python"},
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
        payload={},
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
