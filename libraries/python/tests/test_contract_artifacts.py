from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
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
from deckr.contracts.nats import (
    DECKR_NATS_HEADERS,
    LANE_SUBJECT_PREFIX,
    LANE_SUBJECT_TEMPLATE,
    LANE_SUBSCRIBE_TEMPLATE,
    NATS_BINDING_PATH,
    NATS_BINDING_SCHEMA_ID,
    REQUIRED_DECKR_NATS_HEADERS,
    lane_message_headers,
    lane_message_payload,
    lane_message_subject,
    lane_subscribe_subject,
)
from deckr.services.state import (
    parse_service_catalog_key,
    parse_service_status_key,
    parse_service_view_key,
    service_catalog_key,
    service_status_key,
    service_view_key,
)
from deckr.state import (
    DEFAULT_DISCOVERY_STATE_STORE_NAME,
    DEFAULT_LEASE_STATE_STORE_NAME,
    DEFAULT_STATE_LEASE_TTL_SECONDS,
    DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS,
    decode_key_token,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    parse_device_claim_key,
    parse_hardware_inventory_key,
    parse_presence_endpoint_key,
    presence_endpoint_key,
)

REQUIRED_STATIC_GROUP_IDS = {
    "artifacts.manifest",
    "schemas.fixtures",
    "vectors.keys",
    "vectors.identity",
    "messages.actions",
    "messages.hardware",
    "messages.services",
    "runtime.lane",
    "substrate.nats",
}

_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _RFC3339_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _bundle_root() -> Path:
    return contract_bundle_root()


def _generator_module():
    script_path = (
        deckr_repo_root() / "scripts" / "generate_contract_artifacts.py"
    )
    spec = spec_from_file_location("deckr_generate_contract_artifacts", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _generate_contract_artifacts(output_root: Path) -> None:
    _generator_module().generate_contract_artifacts(output_root)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file()))


def _authoring_root() -> Path:
    return deckr_repo_root() / "contract" / "authoring" / "v1"


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
    assert any(
        artifact["kind"] == "binding" and artifact["path"] == NATS_BINDING_PATH
        for artifact in manifest["artifacts"]
    )
    assert read_contract_artifact("manifest.json") == (
        _bundle_root() / "manifest.json"
    ).read_text(encoding="utf-8")
    with contract_bundle_path() as bundle:
        assert (bundle / "index.html").exists()
        assert (bundle / "asyncapi.json").exists()


def test_nats_binding_artifact_matches_core_helpers() -> None:
    binding = _json(_bundle_root() / NATS_BINDING_PATH)
    lane_messages = binding["laneMessages"]
    buckets = binding["currentState"]["buckets"]

    assert binding["schema"] == NATS_BINDING_SCHEMA_ID
    assert lane_messages["subjectRoot"] == LANE_SUBJECT_PREFIX
    assert lane_messages["publishSubjectTemplate"] == LANE_SUBJECT_TEMPLATE
    assert lane_messages["subscribeSubjectTemplate"] == LANE_SUBSCRIBE_TEMPLATE
    assert [header["name"] for header in lane_messages["headers"]] == list(
        DECKR_NATS_HEADERS
    )
    assert [
        header["name"] for header in lane_messages["headers"] if header["required"]
    ] == list(REQUIRED_DECKR_NATS_HEADERS)
    assert lane_messages["lanes"]["hardware_messages"][
        "subscribeSubject"
    ] == lane_subscribe_subject("hardware_messages")
    assert buckets["lease"]["name"] == DEFAULT_LEASE_STATE_STORE_NAME
    assert buckets["lease"]["brokerTtlSeconds"] == DEFAULT_STATE_LEASE_TTL_SECONDS
    assert (
        buckets["lease"]["renewalIntervalSeconds"]
        == DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS
    )
    assert buckets["discovery"]["name"] == DEFAULT_DISCOVERY_STATE_STORE_NAME
    assert buckets["discovery"]["brokerTtlSeconds"] is None


def test_contract_authoring_inputs_validate_against_their_schemas() -> None:
    root = _authoring_root()

    for name in ("schema-metadata", "coverage"):
        schema = _json(root / f"{name}.schema.json")
        payload = _json(root / f"{name}.json")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)


def test_schema_metadata_overlays_resolve_and_document_every_schema() -> None:
    generator = _generator_module()
    root = _bundle_root()
    metadata = _json(_authoring_root() / "schema-metadata.json")["schemas"]
    manifest = _json(root / "manifest.json")
    schema_paths = {
        artifact["path"]
        for artifact in manifest["artifacts"]
        if artifact["kind"] == "schema"
    }

    assert set(metadata) == schema_paths
    for schema_path in sorted(schema_paths):
        root_overlay = metadata[schema_path].get("")
        assert root_overlay is not None, schema_path
        assert root_overlay.get("description"), schema_path
        enriched = generator._apply_schema_metadata_overlay(
            schema_path,
            _json(root / schema_path),
            metadata,
        )
        assert enriched["description"] == root_overlay["description"]


def test_schema_metadata_overlays_cannot_change_validation_shape() -> None:
    generator = _generator_module()
    schema = {"type": "object", "properties": {"lane": {"type": "string"}}}

    try:
        generator._apply_schema_metadata_overlay(
            "schemas/example.schema.json",
            schema,
            {"schemas/example.schema.json": {"": {"type": "object"}}},
        )
    except ValueError:
        return
    raise AssertionError("schema metadata overlay accepted validation-changing key")


def test_contract_coverage_matrix_references_existing_artifacts() -> None:
    coverage = _json(_authoring_root() / "coverage.json")
    manifest = _json(_bundle_root() / "manifest.json")
    artifacts_by_kind = {
        "binding": set(),
        "schema": set(),
        "validFixture": set(),
        "invalidFixture": set(),
        "vector": set(),
    }
    for artifact in manifest["artifacts"]:
        if artifact["kind"] == "binding":
            artifacts_by_kind["binding"].add(artifact["path"])
        elif artifact["kind"] == "schema":
            artifacts_by_kind["schema"].add(artifact["path"])
        elif artifact["kind"] == "vector":
            artifacts_by_kind["vector"].add(artifact["path"])
        elif artifact["kind"] == "fixture" and artifact["valid"]:
            artifacts_by_kind["validFixture"].add(artifact["path"])
        elif artifact["kind"] == "fixture":
            artifacts_by_kind["invalidFixture"].add(artifact["path"])

    covered = {
        "binding": set(),
        "schema": set(),
        "validFixture": set(),
        "invalidFixture": set(),
        "vector": set(),
    }
    for surface in coverage["surfaces"]:
        assert set(surface["conformanceGroups"]) <= REQUIRED_STATIC_GROUP_IDS
        assert set(surface["bindings"]) <= artifacts_by_kind["binding"]
        assert set(surface["schemas"]) <= artifacts_by_kind["schema"]
        assert set(surface["validFixtures"]) <= artifacts_by_kind["validFixture"]
        assert set(surface["invalidFixtures"]) <= artifacts_by_kind["invalidFixture"]
        assert set(surface["vectors"]) <= artifacts_by_kind["vector"]
        covered["binding"].update(surface["bindings"])
        covered["schema"].update(surface["schemas"])
        covered["validFixture"].update(surface["validFixtures"])
        covered["invalidFixture"].update(surface["invalidFixtures"])
        covered["vector"].update(surface["vectors"])

    assert artifacts_by_kind == covered


def test_asyncapi_artifact_indexes_core_lane_contracts() -> None:
    root = _bundle_root()
    asyncapi = _json(root / "asyncapi.json")

    assert asyncapi["asyncapi"] == "3.0.0"
    assert asyncapi["defaultContentType"] == "application/json"
    assert asyncapi["servers"]["deckrNats"]["protocol"] == "nats"
    assert asyncapi["x-deckr-nats-binding"] == NATS_BINDING_PATH

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


def test_contract_browser_previews_nats_binding() -> None:
    html = (_bundle_root() / "index.html").read_text(encoding="utf-8")

    assert "NATS Binding" in html
    assert NATS_BINDING_PATH in html
    assert "vectors/nats-lane.v1.json" in html
    assert LANE_SUBJECT_TEMPLATE in html
    assert LANE_SUBSCRIBE_TEMPLATE.replace(">", "&gt;") in html


def test_asyncapi_embeds_enriched_schema_metadata() -> None:
    asyncapi = _json(_bundle_root() / "asyncapi.json")
    metadata = _json(_authoring_root() / "schema-metadata.json")["schemas"]

    for component in asyncapi["components"]["schemas"].values():
        schema_path = component["x-deckr-schema-path"]
        expected = metadata[schema_path][""]["description"]
        assert component["schema"]["description"] == expected


def test_contract_fixtures_validate_against_declared_schemas() -> None:
    root = _bundle_root()
    manifest = _json(root / "manifest.json")

    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "fixture":
            continue
        schema = _json(root / artifact["schemaPath"])
        fixture = _json(root / artifact["path"])
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(
            schema,
            format_checker=_FORMAT_CHECKER,
        )
        errors = sorted(validator.iter_errors(fixture), key=str)
        if artifact["valid"]:
            assert errors == [], artifact["path"]
        else:
            assert errors, artifact["path"]


def test_key_token_vectors_match_python_helpers() -> None:
    vector = _json(_bundle_root() / "vectors" / "key-tokens.v1.json")

    for case in vector["cases"]:
        if not case.get("valid", True):
            try:
                decode_key_token(case["encoded"])
            except Exception:
                continue
            raise AssertionError(f"{case['id']}: invalid key token accepted")
        assert encode_key_token(case["raw"]) == case["encoded"]
        assert decode_key_token(case["encoded"]) == case["decoded"]


def test_state_key_vectors_match_python_helpers_and_parsers() -> None:
    vector = _json(_bundle_root() / "vectors" / "state-keys.v1.json")

    for case in vector["cases"]:
        helper = case["helper"]
        if not case.get("valid", True):
            if helper == "parse_presence_endpoint_key":
                parser = parse_presence_endpoint_key
            elif helper == "parse_hardware_inventory_key":
                parser = parse_hardware_inventory_key
            elif helper == "parse_device_claim_key":
                parser = parse_device_claim_key
            elif helper == "parse_action_provider_catalog_key":
                parser = parse_action_provider_catalog_key
            elif helper == "parse_service_catalog_key":
                parser = parse_service_catalog_key
            elif helper == "parse_service_status_key":
                parser = parse_service_status_key
            elif helper == "parse_service_view_key":
                parser = parse_service_view_key
            elif helper == "parse_settings_target_key":
                parser = parse_settings_target_key
            else:
                raise AssertionError(f"Unknown state-key parser {helper!r}")
            try:
                parsed = parser(case["key"])
            except Exception:
                continue
            assert parsed is None, case["id"]
            continue
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

        assert lane_message_subject(message) == case["subject"]
        assert dict(lane_message_headers(message)) == case["headers"]
        assert json.loads(lane_message_payload(message)) == json.loads(case["payloadUtf8"])


def test_contract_vectors_include_invalid_cases() -> None:
    root = _bundle_root()
    invalid_cases = []
    for path in ("vectors/key-tokens.v1.json", "vectors/state-keys.v1.json"):
        vector = _json(root / path)
        invalid_cases.extend(
            f"{path}:{case['id']}"
            for case in vector["cases"]
            if case.get("valid") is False
        )
    identity = _json(root / "vectors" / "identity.v1.json")
    invalid_cases.extend(
        f"vectors/identity.v1.json:{case['id']}"
        for case in identity["endpointCases"]
        if case.get("valid") is False
    )

    assert invalid_cases
