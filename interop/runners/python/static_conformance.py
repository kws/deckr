from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from deckr.actions.messages import (
    action_body,
    context_subject,
    parse_settings_target_key,
    settings_target_key,
)
from deckr.actions.state import (
    action_provider_catalog_key,
    parse_action_provider_catalog_key,
)
from deckr.contracts.artifacts import contract_bundle_path, contract_manifest
from deckr.contracts.lanes import CORE_LANE_CONTRACTS
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    SERVICES_LANE,
    DeckrMessage,
    message_is_expired,
    message_targets_endpoint,
    parse_endpoint_address,
)
from deckr.contracts.nats import (
    lane_message_headers,
    lane_message_payload,
    lane_message_subject,
)
from deckr.hardware.descriptors import CapabilityRef, DeviceRef
from deckr.hardware.messages import (
    hardware_body_from_message,
    hardware_subject_for_capability,
)
from deckr.lanes import validate_message_for_contract
from deckr.services.messages import service_body
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


REQUIRED_GROUP_IDS = (
    "artifacts.manifest",
    "schemas.fixtures",
    "vectors.keys",
    "vectors.identity",
    "messages.actions",
    "messages.hardware",
    "messages.services",
    "runtime.lane",
    "substrate.nats",
)


@dataclass
class GroupResult:
    id: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    diagnostics: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failed:
            return "failed"
        if self.passed == 0 and self.skipped:
            return "skipped"
        return "passed"

    def pass_check(self) -> None:
        self.passed += 1

    def fail_check(self, diagnostic: str) -> None:
        self.failed += 1
        self.diagnostics.append(diagnostic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "diagnostics": self.diagnostics,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract-root",
        type=Path,
        default=_default_contract_root(),
        help="Path to the Deckr contract bundle root.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the conformance report to this path instead of stdout.",
    )
    args = parser.parse_args(argv)

    report = run_conformance(args.contract_root)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    return 1 if report["summary"]["failed"] else 0


def run_conformance(contract_root: Path) -> dict[str, Any]:
    root = contract_root.resolve()
    manifest = _json(root / "manifest.json")
    groups = [
        _check_manifest(root, manifest),
        _check_fixtures(root, manifest),
        _check_key_vectors(root),
        _check_identity_vectors(root),
        _check_lane_messages(
            root,
            manifest,
            group_id="messages.actions",
            schema_path="schemas/actions/actions.v1.schema.json",
            lane=ACTIONS_LANE,
            body_checker=action_body,
        ),
        _check_lane_messages(
            root,
            manifest,
            group_id="messages.hardware",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            lane=HARDWARE_MESSAGES_LANE,
            body_checker=hardware_body_from_message,
        ),
        _check_lane_messages(
            root,
            manifest,
            group_id="messages.services",
            schema_path="schemas/services/services.v1.schema.json",
            lane=SERVICES_LANE,
            body_checker=service_body,
        ),
        _check_lane_runtime_vectors(root),
        _check_nats_lane_vectors(root),
    ]
    summary = {
        "passed": sum(group.passed for group in groups),
        "failed": sum(group.failed for group in groups),
        "skipped": sum(group.skipped for group in groups),
    }
    summary["status"] = "failed" if summary["failed"] else "passed"
    return {
        "schema": "dev.deckr.interop.report.v1",
        "contractVersion": str(manifest["contractVersion"]),
        "specVersion": str(manifest["specVersion"]),
        "implementation": {
            "language": "python",
            "package": "deckr",
            "version": _package_version(),
        },
        "roles": ["validator"],
        "groups": [group.to_dict() for group in groups],
        "summary": summary,
    }


def _check_manifest(root: Path, manifest: dict[str, Any]) -> GroupResult:
    group = GroupResult("artifacts.manifest")
    try:
        package_manifest = dict(contract_manifest())
        if package_manifest == manifest:
            group.pass_check()
        else:
            group.fail_check("Package helper manifest differs from contract root")
    except Exception as exc:
        group.fail_check(f"Package helper manifest failed: {exc}")

    try:
        with contract_bundle_path() as bundle:
            if (bundle / "manifest.json").exists():
                group.pass_check()
            else:
                group.fail_check("Package helper bundle path lacks manifest.json")
    except Exception as exc:
        group.fail_check(f"Package helper bundle path failed: {exc}")

    if manifest.get("bundle") == "deckr-contract-v1":
        group.pass_check()
    else:
        group.fail_check(f"Unexpected bundle id: {manifest.get('bundle')!r}")
    if root.joinpath("asyncapi.json").exists():
        group.pass_check()
    else:
        group.fail_check("Missing asyncapi.json")
    if [group["id"] for group in _planned_groups()] == list(REQUIRED_GROUP_IDS):
        group.pass_check()
    else:
        group.fail_check("Static runner group ids drifted from required ids")
    return group


def _check_fixtures(root: Path, manifest: dict[str, Any]) -> GroupResult:
    group = GroupResult("schemas.fixtures")
    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "fixture":
            continue
        schema = _json(root / artifact["schemaPath"])
        fixture = _json(root / artifact["path"])
        valid = bool(artifact["valid"])
        try:
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            errors = sorted(validator.iter_errors(fixture), key=str)
        except Exception as exc:
            group.fail_check(f"{artifact['path']}: validation failed: {exc}")
            continue
        if valid and errors:
            group.fail_check(f"{artifact['path']}: expected valid, got errors")
        elif not valid and not errors:
            group.fail_check(f"{artifact['path']}: expected invalid, got no errors")
        else:
            group.pass_check()
    return group


def _check_key_vectors(root: Path) -> GroupResult:
    group = GroupResult("vectors.keys")
    vector = _json(root / "vectors" / "key-tokens.v1.json")
    for case in vector["cases"]:
        encoded = encode_key_token(case["raw"])
        decoded = decode_key_token(case["encoded"])
        if encoded != case["encoded"]:
            group.fail_check(f"{case['raw']!r}: encoded as {encoded!r}")
        elif decoded != case["decoded"]:
            group.fail_check(f"{case['encoded']!r}: decoded as {decoded!r}")
        else:
            group.pass_check()

    vector = _json(root / "vectors" / "state-keys.v1.json")
    for case in vector["cases"]:
        try:
            key, parsed = _state_key_case(case)
        except Exception as exc:
            group.fail_check(f"{case['id']}: helper failed: {exc}")
            continue
        if key != case["key"]:
            group.fail_check(f"{case['id']}: key {key!r} != {case['key']!r}")
        elif parsed != case["parsed"]:
            group.fail_check(f"{case['id']}: parsed {parsed!r} != {case['parsed']!r}")
        else:
            group.pass_check()
    return group


def _state_key_case(case: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    helper = case["helper"]
    inputs = case["input"]
    if helper == "presence_endpoint_key":
        key = presence_endpoint_key(
            lane=inputs["lane"],
            endpoint=inputs["endpoint"],
        )
        parsed = parse_presence_endpoint_key(key)
        return key, {"lane": parsed[0], "endpoint": str(parsed[1])}
    if helper == "hardware_inventory_key":
        key = hardware_inventory_key(inputs["managerId"])
        return key, {"managerId": parse_hardware_inventory_key(key)}
    if helper == "device_claim_key":
        key = device_claim_key(
            manager_id=inputs["managerId"],
            device_id=inputs["deviceId"],
        )
        parsed = parse_device_claim_key(key)
        return key, {"managerId": parsed[0], "deviceId": parsed[1]}
    if helper == "action_provider_catalog_key":
        key = action_provider_catalog_key(inputs["providerInstanceId"])
        return key, {
            "providerInstanceId": parse_action_provider_catalog_key(key),
        }
    if helper == "service_catalog_key":
        key = service_catalog_key(inputs["serviceId"])
        return key, {"serviceId": parse_service_catalog_key(key)}
    if helper == "service_status_key":
        key = service_status_key(inputs["serviceId"])
        return key, {"serviceId": parse_service_status_key(key)}
    if helper == "service_view_key":
        key = service_view_key(
            inputs["serviceId"],
            inputs["serviceNamespace"],
            *inputs["tokens"],
        )
        parsed = parse_service_view_key(key)
        return key, {
            "serviceId": parsed[0],
            "serviceNamespace": parsed[1],
            "tokens": list(parsed[2]),
        }
    if helper == "settings_target_key":
        target = parse_settings_target_key(case["key"])
        if target is None:
            raise ValueError("settings target key did not parse")
        key = settings_target_key(target)
        return key, {"target": target.to_dict()}
    raise ValueError(f"Unknown state-key helper {helper!r}")


def _check_identity_vectors(root: Path) -> GroupResult:
    group = GroupResult("vectors.identity")
    vector = _json(root / "vectors" / "identity.v1.json")
    for case in vector["endpointCases"]:
        try:
            endpoint = parse_endpoint_address(case["input"])
        except Exception as exc:
            if case["valid"]:
                group.fail_check(f"{case['id']}: endpoint rejected: {exc}")
            else:
                group.pass_check()
            continue
        if not case["valid"]:
            group.fail_check(f"{case['id']}: invalid endpoint accepted")
        elif endpoint.family != case["family"] or endpoint.endpoint_id != case["endpointId"]:
            group.fail_check(f"{case['id']}: parsed as {endpoint!s}")
        else:
            group.pass_check()

    for case in vector["subjectCases"]:
        try:
            subject = _subject_case(case)
        except Exception as exc:
            group.fail_check(f"{case['id']}: helper failed: {exc}")
            continue
        if subject != case["subject"]:
            group.fail_check(f"{case['id']}: subject {subject!r}")
        else:
            group.pass_check()
    return group


def _subject_case(case: dict[str, Any]) -> dict[str, Any]:
    helper = case["helper"]
    inputs = case["input"]
    if helper == "context_subject":
        return context_subject(
            inputs["contextId"],
            provider_instance_id=inputs.get("providerInstanceId"),
            provider_id=inputs.get("providerId"),
            config_id=inputs.get("configId"),
            action_instance_id=inputs.get("actionInstanceId"),
            binding_id=inputs.get("bindingId"),
        ).model_dump(by_alias=True, exclude_none=True, mode="json")
    if helper == "hardware_subject_for_capability":
        ref = CapabilityRef(
            deviceRef=DeviceRef(**inputs["deviceRef"]),
            controlId=inputs.get("controlId"),
            capabilityId=inputs["capabilityId"],
        )
        return hardware_subject_for_capability(ref).model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
    raise ValueError(f"Unknown subject helper {helper!r}")


def _check_lane_messages(
    root: Path,
    manifest: dict[str, Any],
    *,
    group_id: str,
    schema_path: str,
    lane: str,
    body_checker,
) -> GroupResult:
    group = GroupResult(group_id)
    for artifact in manifest["artifacts"]:
        if (
            artifact["kind"] != "fixture"
            or artifact["schemaPath"] != schema_path
            or not artifact["valid"]
        ):
            continue
        try:
            message = DeckrMessage.from_dict(_json(root / artifact["path"]))
            validate_message_for_contract(message, CORE_LANE_CONTRACTS[lane])
            body_checker(message)
        except Exception as exc:
            group.fail_check(f"{artifact['path']}: message helper rejected: {exc}")
        else:
            group.pass_check()
    return group


def _check_lane_runtime_vectors(root: Path) -> GroupResult:
    group = GroupResult("runtime.lane")
    vector = _json(root / "vectors" / "lane-runtime.v1.json")
    for case in vector["cases"]:
        try:
            payload = case.get("message") or _json(root / case["fixture"])
            message = DeckrMessage.from_dict(payload)
            now = _parse_datetime(case["now"])
            expired = message_is_expired(message, now=now)
            targets_endpoint = message_targets_endpoint(message, case["endpoint"])
            validate_message_for_contract(message, CORE_LANE_CONTRACTS[message.lane])
            session_matches = (
                message.recipient_session_id is None
                or message.recipient_session_id == case["endpointSessionId"]
            )
            deliverable = (not expired) and targets_endpoint and session_matches
        except Exception as exc:
            group.fail_check(f"{case['id']}: helper failed: {exc}")
            continue
        if expired != case["expired"]:
            group.fail_check(f"{case['id']}: expired {expired!r}")
        elif targets_endpoint != case["targetsEndpoint"]:
            group.fail_check(f"{case['id']}: targetsEndpoint {targets_endpoint!r}")
        elif deliverable != case["deliverable"]:
            group.fail_check(f"{case['id']}: deliverable {deliverable!r}")
        else:
            group.pass_check()
    return group


def _check_nats_lane_vectors(root: Path) -> GroupResult:
    group = GroupResult("substrate.nats")
    vector = _json(root / "vectors" / "nats-lane.v1.json")
    for case in vector["cases"]:
        try:
            message = DeckrMessage.from_dict(_json(root / case["fixture"]))
            subject = lane_message_subject(message)
            headers = dict(lane_message_headers(message))
            payload = lane_message_payload(message).decode("utf-8")
        except Exception as exc:
            group.fail_check(f"{case['id']}: helper failed: {exc}")
            continue
        if subject != case["subject"]:
            group.fail_check(f"{case['id']}: subject {subject!r}")
        elif headers != case["headers"]:
            group.fail_check(f"{case['id']}: headers {headers!r}")
        elif payload != case["payloadUtf8"]:
            group.fail_check(f"{case['id']}: payloadUtf8 differed")
        else:
            group.pass_check()
    return group


def _planned_groups() -> list[dict[str, str]]:
    return [{"id": group_id} for group_id in REQUIRED_GROUP_IDS]


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _default_contract_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contract" / "v1"
        if candidate.exists():
            return candidate
    return Path("contract/v1")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _package_version() -> str:
    try:
        return version("deckr")
    except PackageNotFoundError:
        return "0.0.0+unknown"


if __name__ == "__main__":
    raise SystemExit(main())
