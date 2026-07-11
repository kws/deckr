from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_Boundary = tuple[Path, str, str, str]

_STATE_STREAM_OWNERS = {
    (
        Path("deckr/src/deckr/core/util/anyio.py"),
        "CoalescedStateBroadcaster",
    ),
    (Path("deckr/src/deckr/substrates/nats_kv.py"), "NatsKvMaterializedBucket"),
    (Path("deckr/src/deckr/beacon.py"), "Beacon"),
    (Path("deckr/src/deckr/beacon.py"), "_BeaconSubscriber"),
    (Path("deckr/src/deckr/concord.py"), "Concord"),
    (Path("deckr/src/deckr/concord.py"), "_ConcordSubscriber"),
    (Path("deckr/src/deckr/concord.py"), "ConcordParticipant"),
    (Path("deckr/src/deckr/services/views.py"), "ServiceViewStore"),
    (
        Path("deckr/src/deckr/services/subscriptions.py"),
        "SharedResourceSubscriptionManager",
    ),
    (
        Path("deckr-controller/src/deckr/controller/config/_materialized.py"),
        "MaterializedDeviceConfigService",
    ),
    (
        Path("deckr-controller/src/deckr/controller/config/_service.py"),
        "FileBackedDeviceConfigService",
    ),
}


@dataclass(frozen=True, slots=True)
class _TemporaryBoundary:
    phase: str
    reason: str
    count: int = 1


# These are the deliberately bounded state-fanout primitives left after Phase 3.
# Keeping exact semantic call sites here makes accidental replacement with an
# unbounded registry or queue visible in the same audit as temporary debt.
_PERMANENT_BOUNDED_BOUNDARIES: dict[_Boundary, int] = {
    (
        Path("deckr/src/deckr/core/util/anyio.py"),
        "CoalescedStateBroadcaster.__init__",
        "self._registrations",
        "subscriber_registry",
    ): 1,
    (
        Path("deckr/src/deckr/core/util/anyio.py"),
        "CoalescedStateBroadcaster.subscribe",
        "send, receive",
        "bounded_state_stream",
    ): 1,
    (
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryWatcher",
        "pending",
        "typescript_task_or_reply_route_map",
    ): 1,
    (
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryStateStore",
        "watchers",
        "typescript_task_or_reply_route_map",
    ): 1,
    (
        Path("deckr/src/deckr/substrates/nats.py"),
        "NatsSubstrate.request",
        "send, receive",
        "bounded_memory_stream",
    ): 1,
    (
        Path("deckr/src/deckr/substrates/supervised_nats.py"),
        "NatsServerSupervisor.__init__",
        "self._logs",
        "bounded_queue",
    ): 1,
    (
        Path("deckr-driver-virtual/src/deckr/drivers/virtual/_state.py"),
        "VirtualDeck.__init__",
        "self._input_history",
        "bounded_queue",
    ): 1,
}


# Phase 0 freezes each unsafe creator by semantic call site. These are not
# approved APIs: every entry names the implementation phase that removes or
# bounds it, and the test fails when an entry disappears without its metadata
# being removed too.
_TEMPORARY_BOUNDARIES: dict[_Boundary, _TemporaryBoundary] = {
    (
        Path("deckr/src/deckr/components/_runner.py"),
        "ComponentManager.__init__",
        "self._running",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap live components and their owned work",
    ),
    (
        Path("deckr/src/deckr/components/_runner.py"),
        "ComponentManager.__init__",
        "self._lifecycle_by_name",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound component lifecycle identities with component admission",
    ),
    (
        Path("deckr/src/deckr/concord.py"),
        "ConcordParticipant.__init__",
        "self._managed",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 4",
        reason="move managed contracts under bounded pointer-scoped ownership",
    ),
    (
        Path("deckr/src/deckr/concord.py"),
        "ConcordParticipant.__init__",
        "self._leases",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 4",
        reason="move participant leases under bounded contract ownership",
    ),
    (
        Path("deckr/src/deckr/services/client.py"),
        "DeckrServices.__init__",
        "self._active_service_use_leases",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="cap and close active service-use lease owners",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_actions.py"
        ),
        "DeckrAction.__init__",
        "self.pages",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap action-owned dynamic page sessions",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_actions.py"
        ),
        "DeckrAction.__init__",
        "self._deckr_bindings",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap action-owned control bindings",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_actions.py"
        ),
        "DeckrAction.__init__",
        "self._deckr_contexts",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap mounted action contexts and close them with their owner",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_host.py"
        ),
        "_ManagedDeckrActionHost.__init__",
        "self._root_contexts",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap managed root contexts under provider ownership",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._instances",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap active action instances under provider ownership",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._contexts",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap runtime action contexts under provider ownership",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._bindings",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap runtime binding owners and their command routes",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._pages",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap runtime dynamic page sessions",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._managed_contracts_by_pointer",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="cap managed service contracts under provider-lease ownership",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_actions/_service.py"),
        "ControllerActionService.__init__",
        "self._runtime_leases",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap provider runtime leases under controller ownership",
    ),
    (
        Path(
            "deckr-controller/src/deckr/controller/"
            "_bindings/_action_lifecycle.py"
        ),
        "ActionInstanceLifecycleService.__init__",
        "self._action_instances",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="cap controller-owned action instances",
    ),
    (
        Path(
            "deckr-controller/src/deckr/controller/"
            "_bindings/_action_lifecycle.py"
        ),
        "ActionInstanceLifecycleService.__init__",
        "self._action_instance_providers",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="bound action-to-provider ownership routes",
    ),
    (
        Path(
            "deckr-controller/src/deckr/controller/"
            "_bindings/_action_lifecycle.py"
        ),
        "ActionInstanceLifecycleService.__init__",
        "self._action_instance_provider_sessions",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5A",
        reason="bound action-to-provider-session ownership routes",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_bindings/_attachments.py"),
        "ControlAttachmentState.__init__",
        "self.binding_leases",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap retained binding lease identities",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_bindings/_attachments.py"),
        "ControlAttachmentState.__init__",
        "self.binding_by_context",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound active context-to-binding routes",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_controller_service.py"),
        "ControllerService.__init__",
        "self._device_disconnect_events",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="bound device-disconnect wait routes and terminal cleanup",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_hardware/_routes.py"),
        "DeviceRouteRegistry.__init__",
        "self._devices_by_config",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="cap live device routes under managed hardware ownership",
    ),
    (
        Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_factory.py"),
        "Zigbee2MqttHardwareManager.__init__",
        "self._runtimes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="cap MQTT device runtimes under hardware ownership",
    ),
    (
        Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_factory.py"),
        "Zigbee2MqttHardwareManager.__init__",
        "self._runtime_by_topic",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="bound MQTT topic-to-runtime routes",
    ),
    (
        Path("deckr-driver-mqtt/src/deckr/drivers/mqtt/_factory.py"),
        "Zigbee2MqttHardwareManager._reconcile_discovered_devices",
        "self._runtime_by_topic",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="keep MQTT route replacement within the runtime admission bound",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
        "SonosServiceComponent.__init__",
        "self._zone_event_by_name",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="bound retained zone event identities under provider ownership",
    ),
    (
        Path("deckr/src/deckr/components/_runner.py"),
        "ComponentManager.__init__",
        "self._event_send, self._event_receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="replace oversized component ingress with explicit admission",
    ),
    (
        Path("deckr/src/deckr/lanes.py"),
        "EndpointSession.__init__",
        "self._subscriptions",
        "listener_or_sender_registry",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap endpoint subscription registrations and own their shutdown",
    ),
    (
        Path("deckr/src/deckr/substrates/nats.py"),
        "NatsSubstrate.subscribe",
        "send, receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="enforce the lane capacity invariant and typed overload closure",
    ),
    (
        Path("deckr-driver-elgato/src/deckr/drivers/elgato/_device.py"),
        "ElgatoDockDevice.__init__",
        "self._event_send, self._event_receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="move hardware input admission under the managed device owner",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_discovery.py"),
        "discover_mirabox_devices",
        "send_stream, receive_stream",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="make hardware connection event overload explicit",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_discovery.py"),
        "discover_mirabox_devices",
        "discovery_send, discovery_receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="bound discovery work under the managed hardware context",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_discovery.py"),
        "device_loop",
        "command_send, command_receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="make per-device command overload explicit",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_factory.py"),
        "MiraboxDeviceFactory.__init__",
        "self._command_streams",
        "listener_or_sender_registry",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="cap and close every per-device command sender route",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_transport.py"),
        "_AsyncHidTransport.__init__",
        "self._send_stream, self._receive_stream",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="make HID input overload explicit",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_transport.py"),
        "_AsyncHidTransport.__init__",
        "self._senders",
        "listener_or_sender_registry",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="cap HID listeners and define slow-sender closure",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_transport.py"),
        "_AsyncHidTransport.subscribe",
        "send, receive",
        "bounded_memory_stream",
    ): _TemporaryBoundary(
        phase="Phase 5C",
        reason="make HID subscriber overload explicit",
    ),
    (
        Path("deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"),
        "OpenHabServiceComponent.__init__",
        "self._subscriptions",
        "listener_or_sender_registry",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="cap retained resource subscriptions under the provider lease",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
        "SonosServiceComponent.__init__",
        "self._subscriptions",
        "listener_or_sender_registry",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="cap retained zone subscriptions under the provider lease",
    ),
    (
        Path("deckr/typescript/deckr/src/beacon.ts"),
        "BeaconService",
        "advertisements",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="enforce the shared Beacon admission envelope in TypeScript",
    ),
    (
        Path("deckr/typescript/deckr/src/services.ts"),
        "ServiceViewStoreWriter",
        "revisions",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound retained protected-view revision identities",
    ),
    (
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryStateStore",
        "entries",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="define admission for retained in-memory state identities",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "actions",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap registered plugin actions under host ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "actionByInstanceKey",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap active action-instance routes under host ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "contextById",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap active context routes under host ownership",
    ),
    (
        Path("deckr/src/deckr/services/subscriptions.py"),
        "SharedResourceSubscriptionManager.__init__",
        "self._subscribers",
        "subscriber_registry",
    ): _TemporaryBoundary(
        phase="Phase 5B",
        reason="cap logical sessions under provider-lease-scoped ownership",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_runtime_dispatch.py"
        ),
        "_RuntimeMessageDispatchMixin._action_dispatch_lane",
        "send, receive",
        "memory_stream_without_positive_capacity",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="replace infinite per-key dispatch lanes with bounded keyed workers",
    ),
    (
        Path("deckr/src/deckr/substrates/nats.py"),
        "NatsSubstrate.__init__",
        "self._reply_subjects",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound in-flight request routes and clean every terminal path",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/runtime.py"
        ),
        "PythonActionProvider.__init__",
        "self._action_dispatch_lanes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap keyed workers and evict idle dispatch lanes",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_host.py"
        ),
        "_ComponentScopedTasks.__init__",
        "self._scopes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="move component work under the managed action owner",
    ),
    (
        Path(
            "deckr-action-provider-runtime-python/"
            "src/deckr/action_provider_runtime/_sdk_tasks.py"
        ),
        "_PageTaskScope.__init__",
        "self._scopes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="move page work under the managed action owner",
    ),
    (
        Path("deckr-controller/src/deckr/controller/_actions/_service.py"),
        "ControllerActionService.__init__",
        "self._service_watch_scopes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="replace per-provider watch scopes with the managed provider owner",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_factory.py"),
        "MiraboxDeviceFactory.__init__",
        "self._command_streams",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="move command routes to bounded managed hardware ownership",
    ),
    (
        Path("deckr-driver-mirabox/src/deckr/drivers/mirabox/_discovery.py"),
        "discover_mirabox_devices",
        "command_streams",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="move command routes to bounded managed hardware ownership",
    ),
    (
        Path("deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"),
        "OpenHabServiceComponent.__init__",
        "self._refresh_inflight",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound provider work through managed command ownership",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
        "SonosServiceComponent.__init__",
        "self._zone_watch_scopes",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="replace per-zone tasks with provider-scoped managed work",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
        "SonosServiceComponent.__init__",
        "self._resolver_inflight",
        "task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound provider work through managed command ownership",
    ),
    (
        Path("deckr/typescript/deckr/src/services.ts"),
        "ServiceUseLeaseManager",
        "leases",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound managed service owners and their retained resources",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "managed",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="move managed agreement state under bounded pointer-scoped ownership",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "leases",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="move participant leases under bounded agreement ownership",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "timers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="replace detached timer tracking with managed cancellation scopes",
    ),
    (
        Path("deckr-adapter-elgato-node/src/elgato/runtime.ts"),
        "ElgatoPluginRuntime",
        "pendingMessages",
        "typescript_array_queue_without_capacity",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound the plugin command queue with explicit overload",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "endpoints",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound managed endpoint-session routing",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "handlers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound managed lane subscription handlers",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "providers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap the managed provider worker registry",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "actionByProviderAndId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap provider action routing under managed provider ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "pendingSettingsByMessageId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="bound reply routes and clean every terminal path",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "providerByInstanceId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap provider-scoped managed ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "providerByRuntime",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap provider runtime ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimeByRegistrationUuid",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap managed plugin runtime ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimeBySocket",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap managed plugin runtime socket routes",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimes",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 8",
        reason="cap managed plugin runtime workers",
    ),
}


def test_production_queue_and_task_boundaries_are_inventoried() -> None:
    workspace = Path(__file__).resolve().parents[2]
    production_files = _production_python_files(workspace)
    observed: Counter[_Boundary] = Counter()
    for path in production_files:
        relative = path.relative_to(workspace)
        tree = ast.parse(path.read_text(), filename=str(path))
        visitor = _QueueTaskBoundaryVisitor(relative)
        visitor.visit(tree)
        observed.update(visitor.boundaries)

    typescript_files = _production_typescript_files(workspace)
    for path in typescript_files:
        relative = path.relative_to(workspace)
        observed.update(_typescript_queue_task_boundaries(relative, path.read_text()))

    available_paths = {
        path.relative_to(workspace) for path in (*production_files, *typescript_files)
    }
    allowed = Counter(
        {
            boundary: metadata.count
            for boundary, metadata in _TEMPORARY_BOUNDARIES.items()
            if boundary[0] in available_paths
        }
    )
    allowed.update(
        {
            boundary: count
            for boundary, count in _PERMANENT_BOUNDED_BOUNDARIES.items()
            if boundary[0] in available_paths
        }
    )
    unexpected = observed - allowed
    stale_allowlist = allowed - observed
    assert not unexpected and not stale_allowlist, _format_boundary_diff(
        unexpected=unexpected,
        stale_allowlist=stale_allowlist,
    )


def test_queue_task_audit_detects_each_forbidden_boundary_shape() -> None:
    tree = ast.parse(
        """
class Example:
    def __init__(self):
        self.omitted = anyio.create_memory_object_stream()
        self.zero = anyio.create_memory_object_stream(max_buffer_size=0)
        self.infinite = anyio.create_memory_object_stream(inf)
        self.queue = Queue()
        self.history = deque()
        self.subscribers = SubscribableQueue(maxsize=10)
        self.scheduled = ScheduledQueue()
        self.reply_routes = {}
        self.tasks = {}
        self.bounded = anyio.create_memory_object_stream(capacity)
        self.window = deque(maxlen=capacity)
        self.commands = Queue(maxsize=10)
"""
    )
    visitor = _QueueTaskBoundaryVisitor(Path("deckr-example/src/example.py"))
    visitor.visit(tree)

    assert visitor.boundaries == Counter(
        {
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.omitted",
                "memory_stream_without_positive_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.zero",
                "memory_stream_without_positive_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.infinite",
                "memory_stream_without_positive_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.queue",
                "queue_without_positive_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.history",
                "queue_without_positive_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.subscribers",
                "SubscribableQueue",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.scheduled",
                "ScheduledQueue",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.reply_routes",
                "task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.tasks",
                "task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.bounded",
                "bounded_memory_stream",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.window",
                "bounded_queue",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.commands",
                "bounded_queue",
            ): 1,
        }
    )


def test_typescript_queue_task_audit_detects_forbidden_field_shapes() -> None:
    source = """
class Example {
  private queue: Command[] = [];
  private readonly pendingReplies = new Map<string, PendingReply>();
  private readonly workers = new Set<Worker>();
  private readonly catalog = new Map<string, Descriptor>();
  defaultPublicQueue: Command[] = [];
  typedCatalog: Map<string, Descriptor> = new Map<string, Descriptor>();
  defaultPublicObservers = new Set<Observer>();

  method() {
    const local = new Map<string, Descriptor>();
  }
}
"""

    assert _typescript_queue_task_boundaries(
        Path("deckr-example/src/example.ts"), source
    ) == Counter(
        {
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "queue",
                "typescript_array_queue_without_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "pendingReplies",
                "typescript_task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "workers",
                "typescript_task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "catalog",
                "typescript_task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "defaultPublicQueue",
                "typescript_array_queue_without_capacity",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "typedCatalog",
                "typescript_task_or_reply_route_map",
            ): 1,
            (
                Path("deckr-example/src/example.ts"),
                "Example",
                "defaultPublicObservers",
                "typescript_task_or_reply_route_map",
            ): 1,
        }
    )


def test_queue_task_audit_detects_bounded_state_streams_and_registries() -> None:
    tree = ast.parse(
        """
class NatsKvMaterializedBucket:
    def __init__(self):
        self._subscribers: set[object] = set()

    async def subscribe(self):
        send, receive = anyio.create_memory_object_stream(max_buffer_size=capacity)
"""
    )
    visitor = _QueueTaskBoundaryVisitor(
        Path("deckr/src/deckr/substrates/nats_kv.py")
    )
    visitor.visit(tree)

    assert visitor.boundaries == Counter(
        {
            (
                Path("deckr/src/deckr/substrates/nats_kv.py"),
                "NatsKvMaterializedBucket.__init__",
                "self._subscribers",
                "subscriber_registry",
            ): 1,
            (
                Path("deckr/src/deckr/substrates/nats_kv.py"),
                "NatsKvMaterializedBucket.subscribe",
                "send, receive",
                "bounded_state_stream",
            ): 1,
        }
    )


def test_queue_task_audit_detects_typed_delivery_registries_after_rename() -> None:
    tree = ast.parse(
        """
class Example:
    def __init__(self):
        self.targets: set[anyio.abc.ObjectSendStream[bytes]] = set()
        self.observers: set[StateListener] = set()
        self.opaque: dict[str, anyio.CancelScope] = {}
"""
    )
    visitor = _QueueTaskBoundaryVisitor(Path("deckr-example/src/example.py"))
    visitor.visit(tree)

    assert visitor.boundaries == Counter(
        {
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.targets",
                "listener_or_sender_registry",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.observers",
                "listener_or_sender_registry",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                "Example.__init__",
                "self.opaque",
                "task_or_reply_route_map",
            ): 1,
        }
    )


class _QueueTaskBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._scope: list[str] = []
        self._assigned_calls: set[int] = set()
        self.boundaries: Counter[_Boundary] = Counter()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        owner = ", ".join(_target_name(target) for target in node.targets)
        self._inspect_assigned_value(node.value, owner=owner, annotation="")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        owner = _target_name(node.target)
        annotation = ast.unparse(node.annotation)
        if node.value is not None:
            self._inspect_assigned_value(
                node.value,
                owner=owner,
                annotation=annotation,
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if id(node) not in self._assigned_calls:
            self._inspect_queue_call(node, owner="<expression>")
        self.generic_visit(node)

    def _visit_callable(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _inspect_assigned_value(
        self,
        value: ast.AST,
        *,
        owner: str,
        annotation: str,
    ) -> None:
        if isinstance(value, ast.Call):
            self._assigned_calls.add(id(value))
            self._inspect_queue_call(value, owner=owner)
        if _is_mapping_initializer(value) and _is_task_or_reply_route_map(
            owner,
            annotation=annotation,
        ):
            self._record(owner, "task_or_reply_route_map")
        if _is_collection_initializer(value):
            if _is_subscriber_registry(owner) or (
                self._is_state_owner_scope() and _is_registration_registry(owner)
            ):
                self._record(owner, "subscriber_registry")
            elif _is_listener_or_sender_registry(owner, annotation=annotation):
                self._record(owner, "listener_or_sender_registry")
        if (
            self._is_state_owner_scope()
            and owner.rsplit(".", 1)[-1].lower().startswith("pending_")
            and _is_list_initializer(value)
        ):
            self._record(owner, "state_queue_without_capacity")

    def _inspect_queue_call(self, node: ast.Call, *, owner: str) -> None:
        name = _callable_leaf_name(node.func)
        if name == "create_memory_object_stream":
            capacity = _call_capacity(node, keyword="max_buffer_size")
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "memory_stream_without_positive_capacity")
            elif self._is_state_owner_scope():
                self._record(owner, "bounded_state_stream")
            else:
                self._record(owner, "bounded_memory_stream")
            return
        if name in {"Queue", "PriorityQueue", "LifoQueue"}:
            capacity = _call_capacity(node, keyword="maxsize")
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "queue_without_positive_capacity")
            else:
                self._record(owner, "bounded_queue")
            return
        if name == "deque":
            capacity = _call_capacity(node, keyword="maxlen", position=1)
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "queue_without_positive_capacity")
            else:
                self._record(owner, "bounded_queue")
            return
        if name in {"SubscribableQueue", "ScheduledQueue"}:
            self._record(owner, name)

    def _record(self, owner: str, kind: str) -> None:
        scope = ".".join(self._scope) if self._scope else "<module>"
        self.boundaries[(self._path, scope, owner, kind)] += 1

    def _is_state_owner_scope(self) -> bool:
        return any(
            self._path == path and owner in self._scope
            for path, owner in _STATE_STREAM_OWNERS
        )


def _callable_leaf_name(node: ast.AST) -> str:
    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _call_capacity(
    node: ast.Call,
    *,
    keyword: str,
    position: int = 0,
) -> ast.AST | None:
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return node.args[position] if len(node.args) > position else None


def _is_explicit_positive_capacity(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return (
            isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
            and math.isfinite(node.value)
            and node.value > 0
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Constant) and isinstance(
            node.operand.value, int | float
        ):
            value = node.operand.value
            if isinstance(node.op, ast.USub):
                value = -value
            return math.isfinite(value) and value > 0
    if isinstance(node, ast.Name) and node.id.lower() in {"inf", "infinity"}:
        return False
    if isinstance(node, ast.Attribute) and node.attr.lower() in {"inf", "infinity"}:
        return False
    # A named capacity is explicit; its owning constructor must enforce its
    # positive runtime invariant. This guard catches omitted, zero, negative,
    # and infinite production capacity at the creator boundary.
    return not (
        isinstance(node, ast.Call)
        and _callable_leaf_name(node.func) == "float"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value).lower()
        in {
            "inf",
            "+inf",
            "-inf",
            "infinity",
            "+infinity",
            "-infinity",
            "nan",
        }
    )


def _is_mapping_initializer(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return True
    if not isinstance(node, ast.Call):
        return False
    if _callable_leaf_name(node.func) in {"dict", "defaultdict"}:
        return True
    return _callable_leaf_name(node.func) == "field" and any(
        item.arg == "default_factory"
        and _callable_leaf_name(item.value) in {"dict", "defaultdict"}
        for item in node.keywords
    )


def _is_collection_initializer(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.DictComp, ast.Set, ast.SetComp)):
        return True
    if not isinstance(node, ast.Call):
        return False
    name = _callable_leaf_name(node.func)
    if name in {"dict", "defaultdict", "set"}:
        return True
    return name == "field" and any(
        item.arg == "default_factory"
        and _callable_leaf_name(item.value) in {"dict", "defaultdict", "set"}
        for item in node.keywords
    )


def _is_list_initializer(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.ListComp)):
        return True
    if not isinstance(node, ast.Call):
        return False
    name = _callable_leaf_name(node.func)
    if name == "list":
        return True
    return name == "field" and any(
        item.arg == "default_factory" and _callable_leaf_name(item.value) == "list"
        for item in node.keywords
    )


def _is_subscriber_registry(owner: str) -> bool:
    return owner.rsplit(".", 1)[-1].lower() in {"subscribers", "_subscribers"}


def _is_registration_registry(owner: str) -> bool:
    return owner.rsplit(".", 1)[-1].lower() in {"registrations", "_registrations"}


def _is_listener_or_sender_registry(owner: str, *, annotation: str) -> bool:
    name = owner.rsplit(".", 1)[-1].lower()
    if name in {
        "listeners",
        "_listeners",
        "senders",
        "_senders",
        "subscriptions",
        "_subscriptions",
    }:
        return True
    # Type-aware matching keeps renamed delivery registries visible. These are
    # capabilities/registrations rather than ordinary data collections.
    type_names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation)
    return any(
        type_name.lower().endswith(("listener", "sendstream", "subscription"))
        for type_name in type_names
    )


def _is_task_or_reply_route_map(owner: str, *, annotation: str) -> bool:
    name = owner.rsplit(".", 1)[-1].lower()
    retained_owner_type_suffixes = (
        "binding",
        "cancelscope",
        "context",
        "event",
        "inflight",
        "instance",
        "lease",
        "route",
        "runningcomponent",
        "runtime",
        "scope",
        "session",
        "task",
        "watcher",
        "worker",
    )
    is_instance_member = owner.startswith("self.")
    annotation_type_names = tuple(
        type_name.lower()
        for type_name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation)
    )
    return (
        (
            is_instance_member
            and any(
                type_name.startswith("managed")
                or type_name.endswith(retained_owner_type_suffixes)
                for type_name in annotation_type_names
            )
        )
        or "dispatch_lane" in name
        or "command_stream" in name
        or "reply" in name
        or "scope" in name
        or "task" in name
        or "worker" in name
        or (
            is_instance_member
            and any(
                owner_token in name
                for owner_token in (
                    "context",
                    "inflight",
                    "instance",
                    "lease",
                    "managed",
                    "route",
                    "runtime",
                    "session",
                )
            )
        )
    )


_TYPESCRIPT_CLASS = re.compile(
    r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_TYPESCRIPT_ARRAY_FIELD = re.compile(
    r"(?m)^[ \t]+(?!(?:const|let|var)\b)"
    r"(?:(?:abstract|declare|override|private|protected|public|readonly|static)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)[!?]?\s*(?::[^;\n=]+)?\s*=\s*\[\]\s*;"
)
_TYPESCRIPT_MAP_FIELD = re.compile(
    r"(?m)^[ \t]+(?!(?:const|let|var)\b)"
    r"(?:(?:abstract|declare|override|private|protected|public|readonly|static)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)[!?]?\s*(?::[^;\n=]+)?\s*=\s*"
    r"new\s+(?:Map|Set)\b"
)
def _typescript_queue_task_boundaries(
    path: Path,
    source: str,
) -> Counter[_Boundary]:
    boundaries: Counter[_Boundary] = Counter()
    for match in _TYPESCRIPT_ARRAY_FIELD.finditer(source):
        boundaries[
            (
                path,
                _typescript_class_at(source, match.start()),
                match.group("name"),
                "typescript_array_queue_without_capacity",
            )
        ] += 1
    for match in _TYPESCRIPT_MAP_FIELD.finditer(source):
        name = match.group("name")
        boundaries[
            (
                path,
                _typescript_class_at(source, match.start()),
                name,
                "typescript_task_or_reply_route_map",
            )
        ] += 1
    return boundaries


def _typescript_class_at(source: str, position: int) -> str:
    scope = "<module>"
    for match in _TYPESCRIPT_CLASS.finditer(source, 0, position):
        scope = match.group("name")
    return scope


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _target_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, (ast.Tuple, ast.List)):
        return ", ".join(_target_name(item) for item in node.elts)
    return ast.unparse(node)


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


def _production_typescript_files(workspace: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    excluded = {"__tests__", "dist", "node_modules", "test", "tests"}
    for child in workspace.iterdir():
        if not child.name.startswith("deckr"):
            continue
        for path in child.rglob("*.ts"):
            relative_parts = path.relative_to(child).parts
            if "src" not in relative_parts or excluded.intersection(relative_parts):
                continue
            files.append(path)
    return tuple(sorted(files))


def _format_boundary_diff(
    *,
    unexpected: Counter[_Boundary],
    stale_allowlist: Counter[_Boundary],
) -> str:
    lines: list[str] = []
    if unexpected:
        lines.append("unexpected queue/task boundaries:")
        lines.extend(_format_boundaries(unexpected))
    if stale_allowlist:
        lines.append("stale queue/task allowlist entries:")
        lines.extend(_format_boundaries(stale_allowlist))
    return "\n".join(lines)


def _format_boundaries(boundaries: Counter[_Boundary]) -> list[str]:
    return [
        f"  {path}:{scope}: {owner}: {kind} x{count}"
        for (path, scope, owner, kind), count in sorted(boundaries.items())
    ]
