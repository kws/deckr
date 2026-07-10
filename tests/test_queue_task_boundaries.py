from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_Boundary = tuple[Path, str, str, str]


@dataclass(frozen=True, slots=True)
class _TemporaryBoundary:
    phase: str
    reason: str
    count: int = 1


# Phase 0 freezes each unsafe creator by semantic call site. These are not
# approved APIs: every entry names the implementation phase that removes or
# bounds it, and the test fails when an entry disappears without its metadata
# being removed too.
_TEMPORARY_BOUNDARIES: dict[_Boundary, _TemporaryBoundary] = {
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
        Path("deckr/src/deckr/components/_runner.py"),
        "ComponentManager.__init__",
        "self._subscribers",
        "SubscribableQueue",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="replace lifecycle fanout with the bounded current-state broadcaster",
    ),
    (
        Path("deckr/src/deckr/components/_runner.py"),
        "ComponentManager.__init__",
        "self._status_subscribers",
        "SubscribableQueue",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="replace status fanout with the bounded current-state broadcaster",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
        "SonosServiceComponent.__init__",
        "self._zone_events",
        "SubscribableQueue",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="move zone state events to bounded resnapshot-capable fanout",
    ),
    (
        Path("deckr-plugin-sonos/src/deckr/plugins/sonos/_soco_remote.py"),
        "SoCoSonosRemoteZone.__init__",
        "self._subscribers",
        "SubscribableQueue",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="move remote zone state to bounded resnapshot-capable fanout",
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
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryWatcher",
        "queue",
        "typescript_array_queue_without_capacity",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="replace unbounded state delivery with capped resnapshot wakeups",
    ),
    (
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryWatcher",
        "waits",
        "typescript_array_queue_without_capacity",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="replace queued state waiters with one bounded wakeup owner",
    ),
    (
        Path("deckr/typescript/deckr/src/state.ts"),
        "MemoryStateStore",
        "watchers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 3",
        reason="move state subscribers to the capped current-state broadcaster",
    ),
    (
        Path("deckr/typescript/deckr/src/services.ts"),
        "ServiceUseLeaseManager",
        "leases",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound managed service owners and their retained resources",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "managed",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 4",
        reason="move managed agreement state under bounded pointer-scoped ownership",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "leases",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 4",
        reason="move participant leases under bounded agreement ownership",
    ),
    (
        Path("deckr/typescript/deckr/src/concord.ts"),
        "ConcordParticipantManager",
        "timers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 4",
        reason="replace detached timer tracking with managed cancellation scopes",
    ),
    (
        Path("deckr-adapter-elgato-node/src/elgato/runtime.ts"),
        "ElgatoPluginRuntime",
        "pendingMessages",
        "typescript_array_queue_without_capacity",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound the plugin command queue with explicit overload",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "endpoints",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound managed endpoint-session routing",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "handlers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound managed lane subscription handlers",
    ),
    (
        Path("deckr-adapter-elgato-node/src/bridge/nats/NatsBridge.ts"),
        "NatsBridge",
        "providers",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap the managed provider worker registry",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "actionByProviderAndId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap provider action routing under managed provider ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "pendingSettingsByMessageId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="bound reply routes and clean every terminal path",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "providerByInstanceId",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap provider-scoped managed ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "providerByRuntime",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap provider runtime ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimeByRegistrationUuid",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap managed plugin runtime ownership",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimeBySocket",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
        reason="cap managed plugin runtime socket routes",
    ),
    (
        Path("deckr-adapter-elgato-node/src/host/ElgatoBridgeHost.ts"),
        "ElgatoBridgeHost",
        "runtimes",
        "typescript_task_or_reply_route_map",
    ): _TemporaryBoundary(
        phase="Phase 5",
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
        }
    )


def test_typescript_queue_task_audit_detects_forbidden_field_shapes() -> None:
    source = """
class Example {
  private queue: Command[] = [];
  private readonly pendingReplies = new Map<string, PendingReply>();
  private readonly workers = new Set<Worker>();
  private readonly catalog = new Map<string, Descriptor>();
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

    def _inspect_queue_call(self, node: ast.Call, *, owner: str) -> None:
        name = _callable_leaf_name(node.func)
        if name == "create_memory_object_stream":
            capacity = _call_capacity(node, keyword="max_buffer_size")
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "memory_stream_without_positive_capacity")
            return
        if name in {"Queue", "PriorityQueue", "LifoQueue"}:
            capacity = _call_capacity(node, keyword="maxsize")
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "queue_without_positive_capacity")
            return
        if name == "deque":
            capacity = _call_capacity(node, keyword="maxlen", position=1)
            if capacity is None or not _is_explicit_positive_capacity(capacity):
                self._record(owner, "queue_without_positive_capacity")
            return
        if name in {"SubscribableQueue", "ScheduledQueue"}:
            self._record(owner, name)

    def _record(self, owner: str, kind: str) -> None:
        scope = ".".join(self._scope) if self._scope else "<module>"
        self.boundaries[(self._path, scope, owner, kind)] += 1


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


def _is_task_or_reply_route_map(owner: str, *, annotation: str) -> bool:
    name = owner.rsplit(".", 1)[-1].lower()
    lowered_annotation = annotation.lower()
    return (
        "cancelscope" in lowered_annotation
        or "dispatch_lane" in name
        or "command_stream" in name
        or "inflight" in name
        or "reply" in name
        or "scope" in name
        or "task" in name
        or "worker" in name
    )


_TYPESCRIPT_CLASS = re.compile(
    r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_TYPESCRIPT_ARRAY_FIELD = re.compile(
    r"(?m)^\s*(?:private|protected|public)\s+(?:readonly\s+)?"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^;\n]+)?\s*=\s*\[\]\s*;"
)
_TYPESCRIPT_MAP_FIELD = re.compile(
    r"(?m)^[ \t]*(?:private|protected|public)[ \t]+(?:readonly[ \t]+)?"
    r"(?P<name>[A-Za-z_$][\w$]*)[ \t]*=[ \t]*new[ \t]+(?:Map|Set)\b"
)
_TYPESCRIPT_TASK_ROUTE_TOKENS = (
    "endpoint",
    "handler",
    "inflight",
    "lease",
    "managed",
    "pending",
    "provider",
    "queue",
    "reply",
    "route",
    "runtime",
    "scope",
    "subscription",
    "task",
    "timer",
    "wait",
    "watcher",
    "worker",
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
        lowered = name.lower()
        if not any(token in lowered for token in _TYPESCRIPT_TASK_ROUTE_TOKENS):
            continue
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
            if "tests" not in path.parts and "__pycache__" not in path.parts
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
