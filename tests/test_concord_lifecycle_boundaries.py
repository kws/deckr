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

_CallSite = tuple[Path, str, str, str]

# These are the core-owned construction/startup points. They are implementation
# boundaries, not exceptions available to component implementors.
_APPROVED_CORE_LIFECYCLE_CALLS: Counter[_CallSite] = Counter(
    {
        (
            Path("deckr/src/deckr/runtime.py"),
            "Deckr.__aenter__",
            "Beacon",
            "construct",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
            "Deckr.__aenter__",
            "Concord",
            "construct",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
            "Deckr.__aenter__",
            "self._beacon",
            "start",
        ): 1,
        (
            Path("deckr/src/deckr/runtime.py"),
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
        # Phase 2 replaces this three-store Concord construction with the
        # separate typed ConcordMaintenance capability.
        (
            Path("deckr/src/deckr/concord_reaper.py"),
            "component_factory",
            "Concord",
            "construct",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "contracts",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "contract_record",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "validate",
        ): 1,
        (
            Path(
                "deckr-action-provider-runtime-python/"
                "src/deckr/action_provider_runtime/runtime.py"
            ),
            "PythonActionProvider._cancel_stale_service_use_contracts",
            "self._concord",
            "cancel",
        ): 1,
        (
            Path("deckr-controller/src/deckr/controller/_hardware/_validity.py"),
            "validate_owned_claim",
            "concord",
            "validate",
        ): 2,
        (
            Path("deckr-controller/src/deckr/controller/_hardware/_claims.py"),
            "HardwareClaimCoordinator._revoke_terminal_owned_claim",
            "self._concord",
            "validate_exact",
        ): 1,
        (
            Path("deckr-controller-atc/src/deckr_controller_atc/controller.py"),
            "AtcRadarService._render_live_targets",
            "concord",
            "validate_exact",
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
    return lifecycle in receiver.lower()


class _LifecycleCallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._scope: list[str] = []
        self.calls: Counter[_CallSite] = Counter()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node)

    def visit_Call(self, node: ast.Call) -> None:
        constructor = _lifecycle_constructor_name(node)
        if constructor is not None:
            self._record(constructor, "construct")

        if isinstance(node.func, ast.Attribute):
            operation = node.func.attr
            receiver = _receiver_name(node.func.value)
            is_concord = _is_lifecycle_receiver(receiver, "concord")
            is_beacon = _is_lifecycle_receiver(receiver, "beacon")
            if (
                operation in _LOW_LEVEL_ALWAYS
                or (is_concord and operation in _LOW_LEVEL_ON_CONCORD)
                or (is_concord and operation in _RAW_CONCORD_SURFACE)
                or (is_beacon and operation in _LOW_LEVEL_ON_BEACON)
                or ((is_concord or is_beacon) and operation == "start")
            ):
                self._record(receiver, operation)
        self.generic_visit(node)

    def _visit_callable(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _record(self, receiver: str, operation: str) -> None:
        scope = ".".join(self._scope) if self._scope else "<module>"
        self.calls[(self._path, scope, receiver, operation)] += 1


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
        f"  {path}:{scope}: {receiver}.{operation}() x{count}"
        for (path, scope, receiver, operation), count in sorted(calls.items())
    ]
