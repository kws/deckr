from __future__ import annotations

import ast
from pathlib import Path

_LOW_LEVEL_ALWAYS = {
    "_attach",
    "_contract_record",
    "_create_contract",
    "_find_contracts",
    "_participant_lease",
    "_refresh_token",
    "attach",
    "create_contract",
    "participant_lease",
    "refresh_token",
}
_LOW_LEVEL_ON_CONCORD = {"_cancel", "_validate", "cancel", "validate"}
_LOW_LEVEL_ON_BEACON = {
    "_advertise",
    "_refresh",
    "_withdraw",
    "refresh",
    "advertiser",
}
_DIRECT_LIFECYCLE_CONSTRUCTORS = {
    "BeaconAdvertisementLease",
    "ConcordParticipantManager",
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


def _direct_manager_construction(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _DIRECT_LIFECYCLE_CONSTRUCTORS
    )
