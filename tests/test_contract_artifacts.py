from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from deckr.concord import (
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr.contracts.artifacts import (
    contract_bundle_path,
    contract_manifest,
    read_contract_artifact,
)


def _bundle_root() -> Path:
    return Path(__file__).resolve().parents[1] / "contract" / "v1"


def _generate_contract_artifacts(output_root: Path) -> None:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_contract_artifacts.py"
    )
    spec = spec_from_file_location("deckr_generate_contract_artifacts", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.generate_contract_artifacts(output_root)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file()))


def test_contract_artifacts_are_generated_idempotently(tmp_path: Path) -> None:
    generated = tmp_path / "v1"

    _generate_contract_artifacts(generated)

    checked = _bundle_root()
    assert _files(generated) == _files(checked)
    for relative_path in _files(checked):
        assert (generated / relative_path).read_bytes() == (
            checked / relative_path
        ).read_bytes(), relative_path


def test_contract_manifest_is_available_through_public_helper() -> None:
    manifest = contract_manifest()

    assert manifest["bundle"] == "deckr-contract-v1"
    assert read_contract_artifact("manifest.json") == (
        _bundle_root() / "manifest.json"
    ).read_text(encoding="utf-8")
    with contract_bundle_path() as bundle:
        assert (bundle / "index.html").exists()


def test_concord_key_parsers_reject_non_positive_generations() -> None:
    assert parse_concord_contract_key("contracts.hardware-contract.0.meta") is None
    assert parse_concord_contract_key("contracts.hardware-contract.-1.meta") is None
    assert (
        parse_concord_participant_token_key(
            "contracts.hardware-contract.0.participants.controller-main"
        )
        is None
    )
    assert (
        parse_concord_participant_token_key(
            "contracts.hardware-contract.-1.participants.controller-main"
        )
        is None
    )


