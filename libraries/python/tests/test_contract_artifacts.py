from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from repo_paths import contract_bundle_root, deckr_repo_root

from deckr.actions.messages import parse_settings_target_key, settings_target_key
from deckr.actions.state import (
    action_provider_catalog_key,
    parse_action_provider_catalog_key,
)
from deckr.contracts.artifacts import (
    contract_bundle_path,
    contract_manifest,
    read_contract_artifact,
)
from deckr.contracts.messages import DeckrMessage
from deckr.services.state import (
    parse_service_catalog_key,
    parse_service_status_key,
    parse_service_view_key,
    service_catalog_key,
    service_status_key,
    service_view_key,
)
from deckr.state import (
    decode_key_token,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)
from deckr.substrates.nats import _headers_for, _payload_for, _subject_for


def _bundle_root() -> Path:
    return contract_bundle_root()


def _generate_contract_artifacts(output_root: Path) -> None:
    script_path = (
        deckr_repo_root() / "scripts" / "generate_contract_artifacts.py"
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
    assert any(
        artifact["kind"] == "spec" and artifact["path"] == "asyncapi.json"
        for artifact in manifest["artifacts"]
    )
    assert read_contract_artifact("manifest.json") == (
        _bundle_root() / "manifest.json"
    ).read_text(encoding="utf-8")
    with contract_bundle_path() as bundle:
        assert (bundle / "index.html").exists()
        assert (bundle / "asyncapi.json").exists()


def test_asyncapi_artifact_indexes_core_lane_contracts() -> None:
    root = _bundle_root()
    asyncapi = _json(root / "asyncapi.json")

    assert asyncapi["asyncapi"] == "3.0.0"
    assert asyncapi["defaultContentType"] == "application/json"
    assert asyncapi["servers"]["deckrNats"]["protocol"] == "nats"

    expected_lanes = {
        "actionsLane": (
            "actions",
            "ActionsLaneEnvelope",
            "schemas/actions/actions.v1.schema.json",
        ),
        "hardwareMessagesLane": (
            "hardware_messages",
            "HardwareMessagesLaneEnvelope",
            "schemas/hardware/hardware-messages.v1.schema.json",
        ),
        "servicesLane": (
            "services",
            "ServicesLaneEnvelope",
            "schemas/services/services.v1.schema.json",
        ),
    }
    for channel_name, (lane, schema_component, schema_path) in expected_lanes.items():
        channel = asyncapi["channels"][channel_name]
        channel_message_name = next(iter(channel["messages"]))
        channel_message_ref = channel["messages"][channel_message_name]["$ref"]
        message_component = channel_message_ref.rsplit("/", 1)[1]
        message = asyncapi["components"]["messages"][message_component]
        schema = asyncapi["components"]["schemas"][schema_component]

        assert channel["x-deckr-lane"] == lane
        assert channel["x-deckr-schema-path"] == schema_path
        assert message["payload"] == {"$ref": f"#/components/schemas/{schema_component}"}
        assert schema["schemaFormat"] == "application/schema+json;version=draft-07"
        assert schema["x-deckr-schema-path"] == schema_path
        assert schema["schema"]["$id"] == _json(root / schema_path)["$id"]
        assert schema["schema"]["$schema"] == "http://json-schema.org/draft-07/schema#"
        assert (
            schema["schema"]["x-deckr-canonical-schema-dialect"]
            == "https://json-schema.org/draft/2020-12/schema"
        )

        operation_refs = [
            message_ref
            for operation in asyncapi["operations"].values()
            if operation["channel"] == {"$ref": f"#/channels/{channel_name}"}
            for message_ref in operation["messages"]
        ]
        assert {
            "$ref": f"#/channels/{channel_name}/messages/{channel_message_name}"
        } in operation_refs


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


def test_key_token_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "key-tokens.v1.json")

    for case in vector["cases"]:
        assert encode_key_token(case["raw"]) == case["encoded"]
        assert decode_key_token(case["encoded"]) == case["decoded"]


def test_state_key_vectors_match_python_helpers_and_parsers() -> None:
    vector = _json(_bundle_root() / "vectors" / "state-keys.v1.json")

    for case in vector["cases"]:
        helper = case["helper"]
        inputs = case["input"]
        if helper == "presence_endpoint_key":
            key = presence_endpoint_key(
                lane=inputs["lane"],
                endpoint=inputs["endpoint"],
            )
            parsed = parse_presence_endpoint_key(key)
            parsed_value = {
                "lane": parsed[0],
                "endpoint": str(parsed[1]),
            }
        elif helper == "hardware_inventory_key":
            key = hardware_inventory_key(inputs["managerId"])
            parsed_value = {"managerId": parse_hardware_inventory_key(key)}
        elif helper == "device_claim_key":
            key = device_claim_key(
                manager_id=inputs["managerId"],
                device_id=inputs["deviceId"],
            )
            parsed = parse_device_claim_key(key)
            parsed_value = {"managerId": parsed[0], "deviceId": parsed[1]}
        elif helper == "action_provider_catalog_key":
            key = action_provider_catalog_key(inputs["providerInstanceId"])
            parsed_value = {
                "providerInstanceId": parse_action_provider_catalog_key(key)
            }
        elif helper == "service_catalog_key":
            key = service_catalog_key(inputs["serviceId"])
            parsed_value = {"serviceId": parse_service_catalog_key(key)}
        elif helper == "service_status_key":
            key = service_status_key(inputs["serviceId"])
            parsed_value = {"serviceId": parse_service_status_key(key)}
        elif helper == "service_view_key":
            key = service_view_key(
                inputs["serviceId"],
                inputs["serviceNamespace"],
                *inputs["tokens"],
            )
            parsed = parse_service_view_key(key)
            parsed_value = {
                "serviceId": parsed[0],
                "serviceNamespace": parsed[1],
                "tokens": list(parsed[2]),
            }
        elif helper == "settings_target_key":
            parsed = parse_settings_target_key(case["key"])
            assert parsed is not None
            key = settings_target_key(parsed)
            parsed_value = {"target": parsed.to_dict()}
        else:
            raise AssertionError(f"Unknown state-key helper {helper!r}")

        assert key == case["key"]
        assert parsed_value == case["parsed"]


def test_nats_lane_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "nats-lane.v1.json")

    for case in vector["cases"]:
        message = DeckrMessage.from_dict(_json(_bundle_root() / case["fixture"]))

        assert _subject_for(message) == case["subject"]
        assert dict(_headers_for(message)) == case["headers"]
        assert json.loads(_payload_for(message)) == json.loads(case["payloadUtf8"])
