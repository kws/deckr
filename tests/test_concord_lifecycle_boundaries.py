from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_LOW_LEVEL_ALWAYS = {
    "_contract_record",
    "_create_contract",
    "_participant_lease",
    "_refresh_token",
    "create_contract",
    "participant_lease",
    "refresh_token",
}
_LOW_LEVEL_ON_CONCORD = {"_attach", "_cancel", "_validate"}
_RAW_CONCORD_SURFACE = {
    "attach",
    "cancel",
    "contract_record",
    "contracts",
    "maintenance_cancel_contract",
    "maintenance_delete_cancelled_contract",
    "validate",
    "validate_exact",
}
_LOW_LEVEL_ON_BEACON = {
    "_advertise",
    "_refresh",
    "_withdraw",
    "refresh",
    "advertiser",
}
_DIRECT_LIFECYCLE_CONSTRUCTORS = {
    "Beacon",
    "BeaconAdvertisementLease",
    "Concord",
    "ConcordParticipant",
    "ConcordParticipantLease",
}

_CallSite = tuple[Path, int, str, str, str]

# These are the core-owned construction/startup points. They are implementation
# boundaries, not exceptions available to component implementors.
_APPROVED_CORE_LIFECYCLE_CALLS: Counter[_CallSite] = Counter(
    {
        (
            Path("deckr/src/deckr/runtime.py"),
            172,
            "Deckr.__aenter__",
            "Beacon",
            "construct",
        ): 1,
        (
            Path("deckr/src/deckr/testing/concord.py"),
            41,
            "_runtime_concord",
            "Concord",
            "construct",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
            174,
            "Deckr.__aenter__",
            "Concord",
            "construct",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
            173,
            "Deckr.__aenter__",
            "self._beacon",
            "start",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
            178,
            "Deckr.__aenter__",
            "self._concord",
            "start",
        ): 1,
    }
)

# Phase 0 freezes these known raw Concord call sites while their owners move to
# managed recovery/agreement APIs. Keeping semantic call-site identities and
# exact counts prevents this allowlist from granting a whole file an escape
# hatch. A removed call must also be removed here.
_TEMPORARY_RAW_CONCORD_CALLS: Counter[_CallSite] = Counter(
    {
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            376,
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "contracts",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            381,
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "contract_record",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            389,
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "validate",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            404,
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "cancel",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_hardware/_validity.py"),
            40,
            "validate_owned_claim",
            "concord",
            "validate",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_hardware/_validity.py"),
            49,
            "validate_owned_claim",
            "concord",
            "validate",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_hardware/_claims.py"),
            315,
            "HardwareClaimCoordinator._revoke_terminal_owned_claim",
            "self._concord",
            "validate_exact",
        ): 1,
        (
            Path("deckr-controller-atc/src/deckr_controller_atc/controller.py"),
            526,
            "AtcRadarService._render_live_targets",
            "concord",
            "validate_exact",
        ): 1,
        # Phase 5 replaces provider-side authorization through the raw
        # participant validator with the managed provider capability.
        (
            Path("deckr/src/deckr/services/runtime.py"),
            804,
            "authorize_service_message",
            "participant",
            "validate",
        ): 1,
        (
            Path("deckr/src/deckr/services/client.py"),
            833,
            "ManagedServiceContract._context",
            "self._participant",
            "validate",
        ): 1,
        # Phase 5 moves these participant-manager lifecycle operations behind
        # the managed hardware/provider owners.
        (
            Path("deckr/src/deckr/hardware/runtime.py"),
            220,
            "HardwareManagerRuntime.start",
            "self._claim_manager",
            "start",
        ): 1,
        (
            Path("deckr/src/deckr/hardware/runtime.py"),
            607,
            "HardwareManagerRuntime._cancel_claims_for_device",
            "self._claim_manager",
            "cancel",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            332,
            "PythonActionProvider._start_action_runtime_service",
            "self._service_use_manager",
            "start",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            771,
            "PythonActionProvider._withdraw_action_runtime_service",
            "participant",
            "cancel",
        ): 1,
        (
            Path("deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"),
            205,
            "OpenHabServiceComponent.start",
            "self._service_participant",
            "start",
        ): 1,
        (
            Path("deckr-plugin-openhab/src/deckr/plugins/openhab/openhabservice.py"),
            955,
            "OpenHabServiceComponent._cancel_subscription_contract",
            "participant",
            "cancel",
        ): 1,
        (
            Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
            250,
            "SonosServiceComponent.start",
            "self._service_participant",
            "start",
        ): 1,
        (
            Path("deckr-plugin-sonos/src/deckr/plugins/sonos/sonosservice.py"),
            1406,
            "SonosServiceComponent._cancel_subscription_contract",
            "participant",
            "cancel",
        ): 1,
    }
)


def test_production_code_uses_managed_lifecycle_routes() -> None:
    workspace = Path(__file__).resolve().parents[2]
    observed: Counter[_CallSite] = Counter()
    production_files = _production_python_files(workspace)
    for path in production_files:
        relative = path.relative_to(workspace)
        if relative in {
            Path("deckr/src/deckr/beacon.py"),
            Path("deckr/src/deckr/_concord/_maintenance.py"),
            Path("deckr/src/deckr/concord.py"),
        }:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        visitor = _LifecycleCallVisitor(relative)
        visitor.visit(tree)
        observed.update(visitor.calls)

    available_paths = {path.relative_to(workspace) for path in production_files}
    allowed = Counter(
        {
            callsite: count
            for callsite, count in (
                _APPROVED_CORE_LIFECYCLE_CALLS + _TEMPORARY_RAW_CONCORD_CALLS
            ).items()
            if callsite[0] in available_paths
        }
    )
    unexpected = observed - allowed
    stale_allowlist = allowed - observed
    assert not unexpected and not stale_allowlist, _format_callsite_diff(
        unexpected=unexpected,
        stale_allowlist=stale_allowlist,
    )


def test_lifecycle_guard_tracks_typed_and_assigned_aliases_by_line() -> None:
    tree = ast.parse(
        """
async def direct(runtime: Concord) -> None:
    await runtime.cancel(contract, participant)

async def assigned(concord: Concord) -> None:
    runtime = concord
    await runtime.validate_exact(contract)

async def untyped(runtime) -> None:
    await runtime.cancel(contract, participant)

class Owner:
    def __init__(self, concord: Concord) -> None:
        self.runtime = concord
    def start(self, task_group) -> None:
        self.runtime.start(task_group)
"""
    )
    visitor = _LifecycleCallVisitor(Path("deckr-example/src/example.py"))
    visitor.visit(tree)

    assert visitor.calls == Counter(
        {
            (
                Path("deckr-example/src/example.py"),
                3,
                "direct",
                "runtime",
                "cancel",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                7,
                "assigned",
                "runtime",
                "validate_exact",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                10,
                "untyped",
                "runtime",
                "cancel",
            ): 1,
            (
                Path("deckr-example/src/example.py"),
                16,
                "Owner.start",
                "self.runtime",
                "start",
            ): 1,
        }
    )


def _production_python_files(workspace: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for child in workspace.iterdir():
        if not child.name.startswith("deckr"):
            continue
        for dirname in ("src",):
            root = child / dirname
            if root.is_dir():
                roots.append(root)
    files: list[Path] = []
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*.py")
            if "tests" not in path.parts and "__pycache__" not in path.parts
        )
    return tuple(sorted(files))


def test_workspace_tests_do_not_construct_three_store_concord() -> None:
    workspace = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted(workspace.glob("deckr*/tests/**/*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 3:
                continue
            name = _receiver_name(node.func)
            if name.rsplit(".", 1)[-1] == "Concord":
                offenders.append(f"{path.relative_to(workspace)}:{node.lineno}")
    assert not offenders, "direct three-store Concord test construction: " + ", ".join(
        offenders
    )


def test_production_does_not_import_deckr_testing() -> None:
    workspace = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in _production_python_files(workspace):
        relative = path.relative_to(workspace)
        if relative.parts[:4] == ("deckr", "src", "deckr", "testing"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "deckr.testing"
            ) or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith("deckr.testing") for alias in node.names
                )
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, "production imports deckr.testing: " + ", ".join(offenders)


def test_legacy_core_memory_kv_module_is_removed() -> None:
    deckr_root = Path(__file__).resolve().parents[1]
    assert not (deckr_root / "tests" / "memory_kv_bucket.py").exists()
    offenders: list[Path] = []
    for path in sorted((deckr_root / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom) and node.module == "memory_kv_bucket"
            for node in ast.walk(tree)
        ):
            offenders.append(path.relative_to(deckr_root))
    assert not offenders


def test_concord_conflict_handlers_do_not_classify_exception_text() -> None:
    workspace = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in _production_python_files(workspace):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative = path.relative_to(workspace)
        for handler in (
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ):
            if not _exception_type_names(handler.type) & {"ConcordConflict"}:
                continue
            exception_name = handler.name
            if exception_name is None:
                continue
            aliases: set[str] = set()
            for node in ast.walk(handler):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if isinstance(target, ast.Name) and _is_exception_text(
                    node.value,
                    {exception_name, *aliases},
                ):
                    aliases.add(target.id)
            for condition in _handler_conditions(handler):
                if _condition_classifies_exception_text(
                    condition,
                    exception_name=exception_name,
                    text_aliases=aliases,
                ):
                    offenders.append(f"{relative}:{condition.lineno}")
    assert not offenders, "Concord conflict text classification: " + ", ".join(
        offenders
    )


def _exception_type_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return {
            name
            for item in node.elts
            for name in _exception_type_names(item)
        }
    name = _receiver_name(node)
    return {name.rsplit(".", 1)[-1]} if name else set()


def _handler_conditions(handler: ast.ExceptHandler) -> tuple[ast.AST, ...]:
    return tuple(
        node.test
        for node in ast.walk(handler)
        if isinstance(node, (ast.If, ast.While, ast.IfExp))
    )


def _is_exception_text(node: ast.AST, aliases: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in aliases
    )


def _condition_classifies_exception_text(
    condition: ast.AST,
    *,
    exception_name: str,
    text_aliases: set[str],
) -> bool:
    aliases = {exception_name, *text_aliases}
    for node in ast.walk(condition):
        if _is_exception_text(node, aliases):
            return True
        if isinstance(node, ast.Name) and node.id in text_aliases:
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"startswith", "endswith", "search", "match"}
            and any(
                isinstance(child, ast.Name) and child.id in aliases
                for child in ast.walk(node)
            )
        ):
            return True
    return False


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _receiver_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


def _lifecycle_constructor_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        return None
    return name if name in _DIRECT_LIFECYCLE_CONSTRUCTORS else None


def _is_lifecycle_receiver(receiver: str, lifecycle: str) -> bool:
    return any(
        component.lstrip("_").lower() == lifecycle
        for component in receiver.split(".")
    )


class _LifecycleCallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._scope: list[str] = []
        self._lifecycle_aliases: list[dict[str, str]] = [{}]
        self._class_lifecycle_attributes: list[dict[str, str]] = []
        self.calls: Counter[_CallSite] = Counter()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self._class_lifecycle_attributes.append({})
        self.generic_visit(node)
        self._class_lifecycle_attributes.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        lifecycle = self._lifecycle_kind(node.value)
        if lifecycle is not None:
            for target in node.targets:
                self._remember_target(target, lifecycle)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        lifecycle = self._annotation_lifecycle(node.annotation)
        if lifecycle is None and node.value is not None:
            lifecycle = self._lifecycle_kind(node.value)
        if lifecycle is not None:
            self._remember_target(node.target, lifecycle)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        constructor = _lifecycle_constructor_name(node)
        if constructor is not None:
            self._record(constructor, "construct", node.lineno)

        if isinstance(node.func, ast.Attribute):
            operation = node.func.attr
            receiver = _receiver_name(node.func.value)
            lifecycle = self._lifecycle_kind(node.func.value)
            is_concord = lifecycle == "concord"
            is_beacon = lifecycle == "beacon"
            if (
                operation in _LOW_LEVEL_ALWAYS
                or (is_concord and operation in _LOW_LEVEL_ON_CONCORD)
                or (
                    operation in _RAW_CONCORD_SURFACE
                    and (is_concord or _looks_like_raw_concord_call(node, operation))
                )
                or (is_beacon and operation in _LOW_LEVEL_ON_BEACON)
                or (
                    operation in _LOW_LEVEL_ON_BEACON
                    and _looks_like_raw_beacon_call(node, operation)
                )
                or ((is_concord or is_beacon) and operation == "start")
            ):
                self._record(receiver, operation, node.lineno)
        self.generic_visit(node)

    def _visit_callable(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        aliases: dict[str, str] = {}
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            lifecycle = self._annotation_lifecycle(argument.annotation)
            if lifecycle is not None:
                aliases[argument.arg] = lifecycle
        if node.args.vararg is not None:
            lifecycle = self._annotation_lifecycle(node.args.vararg.annotation)
            if lifecycle is not None:
                aliases[node.args.vararg.arg] = lifecycle
        if node.args.kwarg is not None:
            lifecycle = self._annotation_lifecycle(node.args.kwarg.annotation)
            if lifecycle is not None:
                aliases[node.args.kwarg.arg] = lifecycle
        self._lifecycle_aliases.append(aliases)
        self.generic_visit(node)
        self._lifecycle_aliases.pop()
        self._scope.pop()

    def _record(self, receiver: str, operation: str, lineno: int) -> None:
        scope = ".".join(self._scope) if self._scope else "<module>"
        self.calls[(self._path, lineno, scope, receiver, operation)] += 1

    def _lifecycle_kind(self, node: ast.AST) -> str | None:
        name = _receiver_name(node)
        if _is_lifecycle_receiver(name, "concord"):
            return "concord"
        if _is_lifecycle_receiver(name, "beacon"):
            return "beacon"
        root = name.split(".", 1)[0]
        for aliases in reversed(self._lifecycle_aliases):
            lifecycle = aliases.get(name) or aliases.get(root)
            if lifecycle is not None:
                return lifecycle
        for aliases in reversed(self._class_lifecycle_attributes):
            lifecycle = aliases.get(name)
            if lifecycle is not None:
                return lifecycle
        if isinstance(node, ast.Call):
            constructor = _lifecycle_constructor_name(node)
            if constructor is not None:
                return "beacon" if constructor.startswith("Beacon") else "concord"
        if isinstance(node, ast.Attribute) and node.attr in {"beacon", "concord"}:
            return node.attr
        return None

    @staticmethod
    def _annotation_lifecycle(node: ast.AST | None) -> str | None:
        if node is None:
            return None
        names = {
            name.rsplit(".", 1)[-1]
            for child in ast.walk(node)
            if (name := _receiver_name(child))
        }
        if names & {"Beacon", "BeaconAdvertisementLease"}:
            return "beacon"
        if names & {"Concord", "ConcordParticipant", "ConcordParticipantLease"}:
            return "concord"
        return None

    def _remember_target(self, node: ast.AST, lifecycle: str) -> None:
        if isinstance(node, ast.Name):
            self._lifecycle_aliases[-1][node.id] = lifecycle
        elif isinstance(node, ast.Attribute) and self._class_lifecycle_attributes:
            self._class_lifecycle_attributes[-1][_receiver_name(node)] = lifecycle
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                self._remember_target(item, lifecycle)


def _format_callsite_diff(
    *,
    unexpected: Counter[_CallSite],
    stale_allowlist: Counter[_CallSite],
) -> str:
    lines: list[str] = []
    if unexpected:
        lines.append("unexpected raw lifecycle calls:")
        lines.extend(_format_callsites(unexpected))
    if stale_allowlist:
        lines.append("stale lifecycle allowlist entries:")
        lines.extend(_format_callsites(stale_allowlist))
    return "\n".join(lines)


def _format_callsites(calls: Counter[_CallSite]) -> list[str]:
    return [
        f"  {path}:{lineno}:{scope}: {receiver}.{operation}() x{count}"
        for (path, lineno, scope, receiver, operation), count in sorted(calls.items())
    ]


def _looks_like_raw_concord_call(node: ast.Call, operation: str) -> bool:
    # Most raw Concord method names are protocol-specific. ``cancel`` is also
    # common on task scopes and managed agreements, but the raw facade requires
    # both a contract and participant positional argument.
    return operation != "cancel" or len(node.args) >= 2


def _looks_like_raw_beacon_call(node: ast.Call, operation: str) -> bool:
    # Device/lease refresh methods take no arguments. The raw Beacon facade
    # refresh route takes the advertisement handle to mutate.
    return operation != "refresh" or bool(node.args)
