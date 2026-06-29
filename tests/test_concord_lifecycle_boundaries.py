from __future__ import annotations

import ast
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
_LOW_LEVEL_ON_BEACON = {
    "_advertise",
    "_refresh",
    "_withdraw",
    "refresh",
    "advertiser",
}
_DIRECT_LIFECYCLE_CONSTRUCTORS = {
    "BeaconAdvertisementLease",
    "ConcordParticipant",
    "ConcordParticipantLease",
}


def test_production_code_uses_concord_lifecycle_apis() -> None:
    workspace = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for path in _production_python_files(workspace):
        if path.name in {"concord.py", "beacon.py"} and path.parent.name == "deckr":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if _direct_manager_construction(node):
                violations.append(f"{path.relative_to(workspace)}:{node.lineno}")
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if name in _LOW_LEVEL_ALWAYS or (
                name in _LOW_LEVEL_ON_CONCORD
                and "concord" in _receiver_name(node.func.value).lower()
            ):
                violations.append(
                    f"{path.relative_to(workspace)}:{node.lineno} .{name}()"
                )
            if (
                name in _LOW_LEVEL_ON_BEACON
                and "beacon" in _receiver_name(node.func.value).lower()
            ):
                violations.append(
                    f"{path.relative_to(workspace)}:{node.lineno} .{name}()"
                )
    assert violations == []


def test_removed_concord_names_are_not_public() -> None:
    import deckr.concord as concord

    removed = {
        "ConcordCoordinator",
        "ConcordService",
        "ConcordParticipantManager",
        "ConcordContractNotification",
        "ConcordContractEvent",
        "CONCORD_CONTRACT_STORE_POLICY",
        "CONCORD_MAINTENANCE_STORE_POLICY",
        "CONCORD_TOKEN_STORE_POLICY",
    }
    assert all(not hasattr(concord, name) for name in removed)
    assert hasattr(concord, "Concord")
    assert hasattr(concord, "CONCORD_CONTRACT_BUCKET_POLICY")


def test_removed_service_runtime_helpers_are_not_public() -> None:
    import deckr.services as services
    import deckr.services.runtime as runtime

    removed = {
        "AuthorizationDecision",
        "ServiceAdvertiser",
        "ServiceCommandChannel",
        "ServiceUseAuthorizer",
        "ServiceUseLeaseManager",
    }
    assert all(not hasattr(runtime, name) for name in removed)
    assert all(not hasattr(services, name) for name in removed)


def test_removed_state_api_is_not_public() -> None:
    import importlib

    from deckr.lanes import MessageBus
    from deckr.runtime import Deckr
    from deckr.substrates.nats import NatsSubstrate
    from deckr.substrates.supervised_nats import SupervisedNatsSubstrate

    for owner in (Deckr, MessageBus, NatsSubstrate, SupervisedNatsSubstrate):
        assert not hasattr(owner, "state")

    try:
        importlib.import_module("deckr.state")
    except ModuleNotFoundError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("deckr.state should not be importable")


def test_python_code_does_not_import_removed_state_module() -> None:
    workspace = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for path in _repo_python_files(workspace):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "deckr.state":
                        violations.append(f"{path.relative_to(workspace)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "deckr.state":
                    violations.append(f"{path.relative_to(workspace)}:{node.lineno}")
                if node.module == "deckr" and any(
                    alias.name == "state" for alias in node.names
                ):
                    violations.append(f"{path.relative_to(workspace)}:{node.lineno}")
    assert violations == []


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


def _repo_python_files(workspace: Path) -> tuple[Path, ...]:
    roots = tuple(
        root
        for root in (
            workspace / "src",
            workspace / "tests",
            workspace / "scripts",
        )
        if root.is_dir()
    )
    files: list[Path] = []
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
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


def _direct_manager_construction(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _DIRECT_LIFECYCLE_CONSTRUCTORS
    )
