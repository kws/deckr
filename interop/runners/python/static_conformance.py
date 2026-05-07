from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from deckr.actions.state import (
    action_provider_catalog_key,
    parse_action_provider_catalog_key,
)
from deckr.contracts.artifacts import contract_bundle_path, contract_manifest
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
from deckr.substrates.nats import _headers_for, _subject_for


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
        _check_fixtures(root, manifest, valid=True),
        _check_fixtures(root, manifest, valid=False),
        _check_key_token_vectors(root),
        _check_state_key_vectors(root),
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
    group = GroupResult("manifest")
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
    return group


def _check_fixtures(
    root: Path,
    manifest: dict[str, Any],
    *,
    valid: bool,
) -> GroupResult:
    group = GroupResult("fixtures.valid" if valid else "fixtures.invalid")
    for artifact in manifest["artifacts"]:
        if artifact["kind"] != "fixture" or artifact["valid"] is not valid:
            continue
        schema = _json(root / artifact["schemaPath"])
        fixture = _json(root / artifact["path"])
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


def _check_key_token_vectors(root: Path) -> GroupResult:
    group = GroupResult("vectors.key_tokens")
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
    return group


def _check_state_key_vectors(root: Path) -> GroupResult:
    group = GroupResult("vectors.state_keys")
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
    raise ValueError(f"Unknown state-key helper {helper!r}")


def _check_nats_lane_vectors(root: Path) -> GroupResult:
    group = GroupResult("vectors.nats_lane")
    vector = _json(root / "vectors" / "nats-lane.v1.json")
    for case in vector["cases"]:
        try:
            message = DeckrMessage.from_dict(_json(root / case["fixture"]))
            subject = _subject_for(message)
            headers = dict(_headers_for(message))
        except Exception as exc:
            group.fail_check(f"{case['id']}: helper failed: {exc}")
            continue
        if subject != case["subject"]:
            group.fail_check(f"{case['id']}: subject {subject!r}")
        elif headers != case["headers"]:
            group.fail_check(f"{case['id']}: headers {headers!r}")
        else:
            group.pass_check()
    return group


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
