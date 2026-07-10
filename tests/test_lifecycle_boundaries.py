from __future__ import annotations

import re
from pathlib import Path

_APPROVED_AUTHORITY_STORE_OWNERS = {
    Path("deckr/src/deckr/beacon.py"),
    Path("deckr/src/deckr/concord.py"),
}

_FORBIDDEN_PATTERNS = (
    (
        re.compile(r"\.watch\(\s*['\"]advertisements\.by_feature\."),
        "direct Beacon authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*beacon_feature_prefix\("),
        "direct Beacon authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*concord_contracts_prefix\("),
        "direct Concord authority state watch",
    ),
    (
        re.compile(r"\.watch\(\s*['\"]contracts\."),
        "direct Concord authority state watch",
    ),
    (
        re.compile(r"\._(?:contract|token)_state\.watch\("),
        "direct Concord authority state watch",
    ),
    (
        re.compile(
            r"\b_?(?:advertisements?|contracts?|tokens?)"
            r"(?:_state|_bucket)\.(?:watch|subscribe)\s*\("
        ),
        "direct Beacon/Concord authority bucket subscription",
    ),
)


def test_production_code_does_not_watch_authority_stores_directly() -> None:
    workspace = Path(__file__).resolve().parents[2]
    failures: list[str] = []
    for path in _production_python_files(workspace):
        relative = path.relative_to(workspace)
        if relative in _APPROVED_AUTHORITY_STORE_OWNERS:
            continue
        text = path.read_text()
        for pattern, description in _FORBIDDEN_PATTERNS:
            for match in pattern.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                failures.append(f"{relative}:{line_number}: {description}")
    assert not failures, "\n".join(failures)


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

