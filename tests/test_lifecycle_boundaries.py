from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_WatchBoundary = tuple[Path, str, str, str]


# These are the authority-state subscriptions intentionally owned by the Phase 3
# materialized views and their public facades. In particular, the source aliases
# are explicit: renaming ``source`` must update this reviewed inventory rather
# than silently evading a receiver-name pattern.
_APPROVED_AUTHORITY_STATE_BOUNDARIES: dict[_WatchBoundary, str] = {
    (
        Path("deckr/src/deckr/_beacon/_view.py"),
        "BeaconView.run",
        "self.source",
        "subscribe",
    ): "Phase 3 Beacon materialized-view source",
    (
        Path("deckr/src/deckr/_beacon/_view.py"),
        "BeaconView.subscribe",
        "self._broadcaster",
        "subscribe",
    ): "Phase 3 Beacon coalesced semantic view",
    (
        Path("deckr/src/deckr/_concord/_store.py"),
        "_MaterializedSourceAdapter.subscribe",
        "self._bucket",
        "subscribe",
    ): "Phase 3 Concord materialized-store adapter",
    (
        Path("deckr/src/deckr/_concord/_view.py"),
        "ConcordView._contract_loop",
        "self.contract_source",
        "subscribe",
    ): "Phase 3 Concord contract source",
    (
        Path("deckr/src/deckr/_concord/_view.py"),
        "ConcordView._token_loop",
        "self.token_source",
        "subscribe",
    ): "Phase 3 Concord participant-token source",
    (
        Path("deckr/src/deckr/_concord/_view.py"),
        "ConcordView.subscribe",
        "self._broadcaster",
        "subscribe",
    ): "Phase 3 Concord coalesced semantic view",
    (
        Path("deckr/src/deckr/beacon.py"),
        "Beacon.watch",
        "self._view",
        "subscribe",
    ): "Phase 3 Beacon public watch facade",
    (
        Path("deckr/src/deckr/concord.py"),
        "Concord.watch",
        "self._view",
        "subscribe",
    ): "Phase 3 Concord public watch facade",
    (
        Path("deckr/src/deckr/concord.py"),
        "ConcordParticipant.watch",
        "self._state",
        "subscribe",
    ): "Phase 3 participant coalesced state",
    (
        Path("deckr/src/deckr/concord.py"),
        "ConcordParticipant.watch_loop",
        "self._concord._view",
        "subscribe",
    ): "Phase 3 participant Concord view source",
}


# Every other production ``watch``/``subscribe`` call is still exact-inventoried.
# This deliberately includes transports and package-owned state: a new raw
# authority alias cannot bypass the guard merely by choosing an unfamiliar name.
_APPROVED_OTHER_WATCH_BOUNDARIES = Counter[_WatchBoundary](
    {
        (
            Path("deckr/src/deckr/beacon.py"),
            "BeaconDirectory._event_loop",
            "self._beacon",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/components/_runner.py"),
            "ComponentManager.wait_for_state",
            "self",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/components/_runner.py"),
            "ComponentManager.subscribe",
            "self._lifecycle_state",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/components/_runner.py"),
            "ComponentManager.subscribe_status",
            "self._status_state",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/hardware/runtime.py"),
            "HardwareManagerRuntime._command_subscription_loop",
            "self.endpoint",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/hardware/runtime.py"),
            "HardwareManagerRuntime._contract_event_loop",
            "self._claim_manager",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/lanes.py"),
            "EndpointSession.subscribe",
            "self._message_bus",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/services/client.py"),
            "DeckrServices.watch_view",
            "store",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/services/client.py"),
            "_ServiceManagedServiceViewAccess.watch",
            "self._store",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/services/client.py"),
            "_LeaseManagedServiceViewAccess.watch",
            "super()",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/services/subscriptions.py"),
            "SharedResourceSubscriptionManager.open_session",
            "self._state",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/services/views.py"),
            "ServiceViewStore.watch",
            "self._state",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/services/views.py"),
            "ServiceViewStore._event_loop",
            "self._bucket",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/services/views.py"),
            "ManagedServiceViewAccess.watch",
            "self._store",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/nats.py"),
            "NatsSubstrate.request",
            "self._nc",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/nats.py"),
            "NatsSubstrate.subscribe",
            "self._nc",
            "subscribe",
        ): 2,
        (
            Path("deckr/src/deckr/substrates/nats_kv.py"),
            "NatsJsonKvBucket.watch",
            "kv",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/nats_kv.py"),
            "NatsKvMaterializedBucket.subscribe",
            "self._broadcaster",
            "subscribe",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/nats_kv.py"),
            "NatsKvMaterializedBucket._consume_one_watch",
            "self._bucket",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/nats_kv.py"),
            "kv_keys_by_watch",
            "kv",
            "watch",
        ): 1,
        (
            Path("deckr/src/deckr/substrates/supervised_nats.py"),
            "SupervisedNatsSubstrate.subscribe",
            "self._connected_nats()",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/_runtime_dispatch.py"
            ),
            "_RuntimeMessageDispatchMixin._message_subscription_loop",
            "endpoint",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            "PythonActionProvider._service_contract_event_loop",
            "participant",
            "watch",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_controller_service.py"),
            "ControllerService._actions_subscription_loop",
            "self._endpoint",
            "subscribe",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_controller_service.py"),
            "ControllerService._device_lifecycle",
            "self._config_service",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-controller/src/deckr/controller/_hardware/_service.py"
            ),
            "ControllerHardwareService._input_loop",
            "self._endpoint",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-controller/src/deckr/controller/_hardware/_service.py"
            ),
            "ControllerHardwareService._claim_event_loop",
            "self._concord",
            "watch",
        ): 1,
        (
            Path(
                "deckr-controller/src/deckr/controller/config/_materialized.py"
            ),
            "MaterializedDeviceConfigService._subscribe_impl",
            "self._state",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-controller/src/deckr/controller/config/_materialized.py"
            ),
            "MaterializedDeviceConfigService._watch_loop",
            "self._bucket",
            "watch",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/config/_service.py"),
            "FileBackedDeviceConfigService._subscribe_impl",
            "self._state",
            "subscribe",
        ): 1,
        (
            Path("deckr-controller-atc/src/deckr_controller_atc/controller.py"),
            "AtcRadarService._hardware_input_loop",
            "endpoint",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-elgato/src/deckr/drivers/elgato/_discovery.py"),
            "ElgatoDeviceSupervisor._forward_input",
            "device",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_device.py"),
            "MiraBoxDockDevice.subscribe",
            "self.transport",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-driver-mirabox/src/deckr/drivers/mirabox/_discovery.py"
            ),
            "_forward_device_events",
            "device",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_actions_cli.py"),
            "_read_bridge_devices",
            "client",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_actions_cli.py"),
            "_read_bridge_device_metadata",
            "client",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_actions_cli.py"),
            "_inspect",
            "client",
            "subscribe",
        ): 2,
        (
            Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_factory.py"),
            "Zigbee2MqttHardwareManager._mqtt_discovery_loop",
            "client",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_factory.py"),
            "Zigbee2MqttHardwareManager._handle_bridge_devices_payload",
            "client",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-virtual/src/deckr/drivers/virtual/_state.py"),
            "VirtualDeck.subscribe",
            "self._state",
            "subscribe",
        ): 1,
        (
            Path("deckr-driver-virtual/src/deckr/drivers/virtual/_web.py"),
            "create_app.events",
            "deck",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"
            ),
            "OpenHabServiceComponent._message_loop",
            "self._endpoint",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"
            ),
            "OpenHabServiceComponent._service_contract_event_loop",
            "participant",
            "watch",
        ): 1,
        (
            Path("deckr-plugin-sonos/src/deckr/plugins/sonos/_soco_remote.py"),
            "SoCoSonosRemoteZone._subscribe",
            "self._events",
            "subscribe",
        ): 1,
        (
            Path("deckr-plugin-sonos/src/deckr/plugins/sonos/_soco_remote.py"),
            "_connect_speaker_sync",
            "speaker.avTransport",
            "subscribe",
        ): 1,
        (
            Path("deckr-plugin-sonos/src/deckr/plugins/sonos/_soco_remote.py"),
            "_connect_speaker_sync",
            "speaker.renderingControl",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"
            ),
            "SonosServiceComponent._message_loop",
            "self._endpoint",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"
            ),
            "SonosServiceComponent._zone_event_snapshots",
            "self._zone_events",
            "subscribe",
        ): 1,
        (
            Path(
                "deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"
            ),
            "SonosServiceComponent._service_contract_event_loop",
            "participant",
            "watch",
        ): 1,
    }
)


def test_production_watch_and_subscription_boundaries_are_exactly_inventoried() -> (
    None
):
    workspace = Path(__file__).resolve().parents[2]
    production_files = _production_python_files(workspace)
    observed: Counter[_WatchBoundary] = Counter()
    for path in production_files:
        relative = path.relative_to(workspace)
        observed.update(_watch_boundaries(relative, path.read_text()))

    available_paths = {path.relative_to(workspace) for path in production_files}
    expected = Counter(
        {
            boundary: 1
            for boundary in _APPROVED_AUTHORITY_STATE_BOUNDARIES
            if boundary[0] in available_paths
        }
    )
    expected.update(
        {
            boundary: count
            for boundary, count in _APPROVED_OTHER_WATCH_BOUNDARIES.items()
            if boundary[0] in available_paths
        }
    )
    unexpected = observed - expected
    stale_allowlist = expected - observed
    assert not unexpected and not stale_allowlist, _format_boundary_diff(
        unexpected=unexpected,
        stale_allowlist=stale_allowlist,
    )


def test_watch_inventory_does_not_depend_on_authority_receiver_names() -> None:
    source = """
class Example:
    async def run(self):
        await arbitrary_alias.subscribe()
        await renamed_source.watch()
        await self.contract_source.subscribe()
"""
    path = Path("deckr-example/src/example.py")
    assert _watch_boundaries(path, source) == Counter(
        {
            (path, "Example.run", "arbitrary_alias", "subscribe"): 1,
            (path, "Example.run", "renamed_source", "watch"): 1,
            (path, "Example.run", "self.contract_source", "subscribe"): 1,
        }
    )


class _WatchBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._scope: list[str] = []
        self.boundaries: Counter[_WatchBoundary] = Counter()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "subscribe",
            "watch",
        }:
            scope = ".".join(self._scope) if self._scope else "<module>"
            self.boundaries[
                (
                    self._path,
                    scope,
                    ast.unparse(node.func.value),
                    node.func.attr,
                )
            ] += 1
        self.generic_visit(node)

    def _visit_callable(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()


def _watch_boundaries(path: Path, source: str) -> Counter[_WatchBoundary]:
    visitor = _WatchBoundaryVisitor(path)
    visitor.visit(ast.parse(source, filename=str(path)))
    return visitor.boundaries


def _production_python_files(workspace: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for child in workspace.iterdir():
        src = child / "src"
        if not child.name.startswith("deckr") or not src.is_dir():
            continue
        files.extend(
            path
            for path in src.rglob("*.py")
            if "tests" not in path.parts
            and "testing" not in path.parts
            and "__pycache__" not in path.parts
        )
    return tuple(sorted(files))


def _format_boundary_diff(
    *,
    unexpected: Counter[_WatchBoundary],
    stale_allowlist: Counter[_WatchBoundary],
) -> str:
    lines: list[str] = []
    if unexpected:
        lines.append("unexpected watch/subscription boundaries:")
        lines.extend(_format_boundaries(unexpected))
    if stale_allowlist:
        lines.append("stale watch/subscription allowlist entries:")
        lines.extend(_format_boundaries(stale_allowlist))
    return "\n".join(lines)


def _format_boundaries(boundaries: Counter[_WatchBoundary]) -> list[str]:
    return [
        f"  {path}:{scope}: {receiver}.{operation}(...) x{count}"
        for (path, scope, receiver, operation), count in sorted(boundaries.items())
    ]
