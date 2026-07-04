from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from deckr.beacon import (
    beacon_advertisement_key,
    parse_beacon_advertisement_key,
)
from deckr.concord import (
    canonical_json_bytes,
    canonical_json_hash,
    concord_contract_key,
    concord_participant_token_key,
    parse_concord_contract_key,
    parse_concord_participant_token_key,
)
from deckr.contracts.artifacts import (
    contract_bundle_path,
    contract_manifest,
    read_contract_artifact,
)
from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.messages import DeckrMessage
from deckr.substrates.nats import _headers_for, _subject_for


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


def test_contract_fixtures_validate_against_declared_schemas() -> None:
    root = _bundle_root()
    manifest = _json(root / "manifest.json")

    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "fixture":
            continue
        schema = _json(root / artifact["schemaPath"])
        fixture = _json(root / artifact["path"])
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(fixture), key=str)
        if artifact["valid"]:
            assert errors == [], artifact["path"]
        else:
            assert errors, artifact["path"]


@pytest.mark.parametrize(
    ("schema_path", "fixture_path"),
    [
        (
            "schemas/actions/actions.v1.schema.json",
            "fixtures/valid/actions/settings-request.v1.json",
        ),
        (
            "schemas/hardware/hardware-messages.v1.schema.json",
            "fixtures/valid/hardware/control-input.v1.json",
        ),
        (
            "schemas/services/services.v1.schema.json",
            "fixtures/valid/services/service-command.v1.json",
        ),
    ],
)
def test_protected_lane_schemas_reject_null_contract(
    schema_path: str,
    fixture_path: str,
) -> None:
    root = _bundle_root()
    schema = _json(root / schema_path)
    fixture = _json(root / fixture_path)
    fixture["contract"] = None

    errors = list(Draft202012Validator(schema).iter_errors(fixture))

    assert errors


def test_key_token_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "key-tokens.v1.json")

    for case in vector["cases"]:
        assert encode_key_token(case["raw"]) == case["token"]
        assert decode_key_token(case["token"]) == case["raw"]


def test_beacon_concord_key_vectors_match_python_helpers_and_parsers() -> None:
    vector = _json(_bundle_root() / "vectors" / "beacon-concord-keys.v1.json")

    for case in vector["cases"]:
        helper = case["helper"]
        inputs = case["input"]
        if helper == "beacon_advertisement_key":
            key = beacon_advertisement_key(
                feature_id=inputs["featureId"],
                advertisement_id=inputs["advertisementId"],
            )
            parsed = parse_beacon_advertisement_key(key)
            assert parsed is not None
            parsed_value = {
                "featureId": parsed[0],
                "advertisementId": parsed[1],
            }
        elif helper == "concord_contract_key":
            key = concord_contract_key(
                contract_id=inputs["contractId"],
                generation=inputs["generation"],
            )
            parsed = parse_concord_contract_key(key)
            assert parsed is not None
            parsed_value = {"contractId": parsed[0], "generation": parsed[1]}
        elif helper == "concord_participant_token_key":
            key = concord_participant_token_key(
                contract_id=inputs["contractId"],
                generation=inputs["generation"],
                participant=inputs["participant"],
            )
            parsed = parse_concord_participant_token_key(key)
            assert parsed is not None
            parsed_value = {
                "contractId": parsed[0],
                "generation": parsed[1],
                "participant": str(parsed[2]),
            }
        else:
            raise AssertionError(f"Unknown Beacon/Concord key helper {helper!r}")

        assert key == case["key"]
        assert parsed_value == inputs


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


def test_concord_terms_hash_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "concord-terms-hash.v1.json")

    for case in vector["cases"]:
        value = json.loads(case["canonicalJson"])
        assert canonical_json_bytes(value).decode("utf-8") == case["canonicalJson"]
        assert canonical_json_hash(value) == case["hash"]


def test_nats_lane_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "nats-lane.v1.json")

    for case in vector["cases"]:
        message = DeckrMessage.from_dict(_json(_bundle_root() / case["fixture"]))

        assert _subject_for(message) == case["subject"]
        assert dict(_headers_for(message)) == case["headers"]
