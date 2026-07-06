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
