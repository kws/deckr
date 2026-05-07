from __future__ import annotations

import json
import subprocess
import sys

from jsonschema import Draft202012Validator
from repo_paths import deckr_repo_root


def test_python_static_conformance_report_validates(tmp_path) -> None:
    repo_root = deckr_repo_root()
    report_path = tmp_path / "report.json"
    script_path = repo_root / "interop" / "runners" / "python" / "static_conformance.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--output", str(report_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (repo_root / "interop" / "report.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    assert report["roles"] == ["validator"]
    assert report["summary"]["failed"] == 0
    assert all(group["status"] == "passed" for group in report["groups"])
