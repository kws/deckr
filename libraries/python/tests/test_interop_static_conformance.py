from __future__ import annotations

import json
import subprocess
import sys

from jsonschema import Draft202012Validator
from repo_paths import deckr_repo_root


def test_static_conformance_reports_validate_for_python_and_rust(tmp_path) -> None:
    repo_root = deckr_repo_root()
    python_report_path = tmp_path / "python-report.json"
    rust_report_path = tmp_path / "rust-report.json"
    script_path = repo_root / "interop" / "runners" / "python" / "static_conformance.py"

    python_result = subprocess.run(
        [sys.executable, str(script_path), "--output", str(python_report_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert python_result.returncode == 0, python_result.stderr or python_result.stdout

    rust_result = subprocess.run(
        [
            "cargo",
            "run",
            "--manifest-path",
            str(repo_root / "libraries" / "rust" / "Cargo.toml"),
            "--bin",
            "deckr-rust-static-conformance",
            "--",
            "--output",
            str(rust_report_path),
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rust_result.returncode == 0, rust_result.stderr or rust_result.stdout

    python_report = json.loads(python_report_path.read_text(encoding="utf-8"))
    rust_report = json.loads(rust_report_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (repo_root / "interop" / "report.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(python_report)
    validator.validate(rust_report)

    assert python_report["roles"] == ["validator"]
    assert rust_report["roles"] == ["validator"]
    assert python_report["summary"]["failed"] == 0
    assert rust_report["summary"]["failed"] == 0
    assert all(group["status"] == "passed" for group in python_report["groups"])
    assert all(group["status"] == "passed" for group in rust_report["groups"])
    assert [group["id"] for group in python_report["groups"]] == [
        group["id"] for group in rust_report["groups"]
    ]
