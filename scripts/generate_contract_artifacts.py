from __future__ import annotations

import html
import json
import shutil
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ACTION_MESSAGES_SCHEMA_ID,
    SETTINGS_REQUEST,
    ActionDescriptor,
    ActionProviderCatalog,
    SettingsTargetRef,
    action_message_schema,
)
from deckr.actions.state import action_provider_catalog_key
from deckr.contracts.messages import (
    ACTIONS_LANE,
    SERVICES_LANE,
    DeckrMessage,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    service_address,
)
from deckr.hardware.capabilities import (
    button_activation_value_schema,
    raster_bitmap_command_schema,
)
from deckr.hardware.descriptors import (
    CAPABILITY_DESCRIPTOR_SCHEMA_ID,
    CONTROL_DESCRIPTOR_SCHEMA_ID,
    DEVICE_DESCRIPTOR_SCHEMA_ID,
    DeviceDescriptor,
    descriptor_schema_artifacts,
)
from deckr.hardware.messages import (
    HARDWARE_MESSAGES_SCHEMA_ID,
    control_input_message,
    device_available_message,
    hardware_message_schema,
)
from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_MESSAGES_SCHEMA_ID,
    ServiceCommandBody,
    service_message_schema,
)
from deckr.services.state import (
    ServiceCatalog,
    ServiceStatus,
    ServiceStatusValue,
    service_catalog_key,
    service_status_key,
    service_view_key,
)
from deckr.state import (
    DeviceClaim,
    EndpointPresence,
    HardwareInventory,
    decode_key_token,
    device_claim_key,
    encode_key_token,
    hardware_inventory_key,
    presence_endpoint_key,
)
from deckr.substrates.nats import _headers_for, _subject_for

CONTRACT_VERSION = "v1"
SPEC_VERSION = "1"
FIXED_NOW = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

STATE_SCHEMA_IDS = {
    "endpoint_presence": "dev.deckr.state.endpoint_presence.v1",
    "hardware_inventory": "dev.deckr.state.hardware_inventory.v1",
    "device_claim": "dev.deckr.state.device_claim.v1",
    "action_provider_catalog": "dev.deckr.state.action_provider_catalog.v1",
    "service_catalog": "dev.deckr.state.service_catalog.v1",
    "service_status": "dev.deckr.state.service_status.v1",
}

DESCRIPTOR_SCHEMA_FILENAMES = {
    DEVICE_DESCRIPTOR_SCHEMA_ID: "device-descriptor.v1.schema.json",
    CONTROL_DESCRIPTOR_SCHEMA_ID: "control-descriptor.v1.schema.json",
    CAPABILITY_DESCRIPTOR_SCHEMA_ID: "capability-descriptor.v1.schema.json",
}


def generate_contract_artifacts(output_root: Path | None = None) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    bundle_root = output_root or repo_root / "contract" / CONTRACT_VERSION
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)

    artifacts: list[dict[str, Any]] = []

    def add_artifact(
        *,
        kind: str,
        artifact_id: str,
        path: str,
        title: str,
        description: str,
        payload: Mapping[str, Any] | list[Any],
        **metadata: Any,
    ) -> None:
        _write_json(bundle_root / path, payload)
        artifacts.append(
            {
                "kind": kind,
                "id": artifact_id,
                "path": path,
                "title": title,
                "description": description,
                **metadata,
            }
        )

    _add_schemas(add_artifact)
    fixtures = _fixtures()
    for fixture in fixtures:
        add_artifact(**fixture)
    _add_vectors(add_artifact, fixtures=fixtures)

    manifest = {
        "schema": "dev.deckr.contract.bundle.v1",
        "bundle": "deckr-contract-v1",
        "specVersion": SPEC_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "deckrPackageVersion": _package_version(repo_root),
        "description": "Generated Deckr v1 contract artifact bundle.",
        "artifacts": sorted(artifacts, key=lambda item: (item["kind"], item["path"])),
    }
    _write_json(bundle_root / "manifest.json", manifest)
    _write_text(bundle_root / "index.html", _render_index(manifest))


def _add_schemas(add_artifact) -> None:
    add_artifact(
        kind="schema",
        artifact_id=ACTION_MESSAGES_SCHEMA_ID,
        path="schemas/actions/actions.v1.schema.json",
        title="Actions lane messages",
        description="Canonical JSON Schema for Deckr actions lane envelopes and bodies.",
        schemaId=ACTION_MESSAGES_SCHEMA_ID,
        payload=action_message_schema(),
    )
    add_artifact(
        kind="schema",
        artifact_id=HARDWARE_MESSAGES_SCHEMA_ID,
        path="schemas/hardware/hardware-messages.v1.schema.json",
        title="Hardware messages lane messages",
        description="Canonical JSON Schema for Deckr hardware_messages lane envelopes and bodies.",
        schemaId=HARDWARE_MESSAGES_SCHEMA_ID,
        payload=hardware_message_schema(),
    )
    add_artifact(
        kind="schema",
        artifact_id=SERVICE_MESSAGES_SCHEMA_ID,
        path="schemas/services/services.v1.schema.json",
        title="Services lane messages",
        description="Canonical JSON Schema for Deckr services lane envelopes and bodies.",
        schemaId=SERVICE_MESSAGES_SCHEMA_ID,
        payload=service_message_schema(),
    )
    for schema_id, schema in descriptor_schema_artifacts().items():
        add_artifact(
            kind="schema",
            artifact_id=schema_id,
            path=f"schemas/hardware/{DESCRIPTOR_SCHEMA_FILENAMES[schema_id]}",
            title=schema["title"],
            description=f"Canonical JSON Schema for {schema['title']}.",
            schemaId=schema_id,
            payload=schema,
        )

    state_schema_models: tuple[tuple[str, str, type[BaseModel], str], ...] = (
        (
            STATE_SCHEMA_IDS["endpoint_presence"],
            "schemas/state/endpoint-presence.v1.schema.json",
            EndpointPresence,
            "Endpoint presence state",
        ),
        (
            STATE_SCHEMA_IDS["hardware_inventory"],
            "schemas/state/hardware-inventory.v1.schema.json",
            HardwareInventory,
            "Hardware inventory state",
        ),
        (
            STATE_SCHEMA_IDS["device_claim"],
            "schemas/state/device-claim.v1.schema.json",
            DeviceClaim,
            "Device claim state",
        ),
        (
            STATE_SCHEMA_IDS["action_provider_catalog"],
            "schemas/state/action-provider-catalog.v1.schema.json",
            ActionProviderCatalog,
            "Action provider catalog state",
        ),
        (
            STATE_SCHEMA_IDS["service_catalog"],
            "schemas/state/service-catalog.v1.schema.json",
            ServiceCatalog,
            "Service catalog state",
        ),
        (
            STATE_SCHEMA_IDS["service_status"],
            "schemas/state/service-status.v1.schema.json",
            ServiceStatus,
            "Service status state",
        ),
    )
    for schema_id, path, model, title in state_schema_models:
        add_artifact(
            kind="schema",
            artifact_id=schema_id,
            path=path,
            title=title,
            description=f"Canonical JSON Schema for {title}.",
            schemaId=schema_id,
            payload=_model_schema(model, schema_id=schema_id, title=title),
        )


def _fixtures() -> list[dict[str, Any]]:
    descriptor = _device_descriptor()
    settings_request = _stable_message(
        DeckrMessage(
            lane=ACTIONS_LANE,
            messageType=SETTINGS_REQUEST,
            sender=action_provider_address("clock-main"),
            senderSessionId="provider-session",
            recipient=endpoint_target(controller_address("controller-main")),
            recipientSessionId="controller-session",
            subject=entity_subject(
                "settings",
                controllerId="controller-main",
                providerInstanceId="clock-main",
                actionInstanceId="clock-instance-1",
            ),
            body={"target": _settings_target().to_dict()},
        ),
        message_id="fixture-action-settings-request",
    )
    hardware_available = _stable_wire_message(
        device_available_message(
            manager_id="mirabox-main",
            sender_session_id="manager-session",
            descriptor=descriptor,
        ),
        message_id="fixture-hardware-device-available",
    )
    hardware_input = _stable_wire_message(
        control_input_message(
            manager_id="mirabox-main",
            sender_session_id="manager-session",
            device_id="deck-1",
            fingerprint="fingerprint:deck-1",
            control_id="key.0.0",
            capability_id="button.press",
            event_type="press",
            value={"eventType": "press"},
            sequence=1,
        ),
        message_id="fixture-hardware-control-input",
    )
    service_command = _stable_message(
        DeckrMessage(
            lane=SERVICES_LANE,
            messageType=SERVICE_COMMAND,
            sender=controller_address("controller-main"),
            senderSessionId="controller-session",
            recipient=endpoint_target(service_address("media-home")),
            recipientSessionId="service-session",
            subject=entity_subject(
                "service",
                serviceId="media-home",
                namespace="dev.deckr.media.service",
                operation="play",
            ),
            body=ServiceCommandBody(
                serviceNamespace="dev.deckr.media.service",
                operation="play",
                params={"zone": "kitchen"},
            ).to_dict(),
        ),
        message_id="fixture-service-command",
    )

    action_catalog = ActionProviderCatalog(
        providerInstanceId="clock-main",
        providerEndpoint=action_provider_address("clock-main"),
        providerId="dev.deckr.clock",
        sessionId="provider-session",
        timestamp=FIXED_NOW,
        labels={"room": "office"},
        annotations={"runtime": "python"},
        actions={
            "dev.deckr.clock.time": ActionDescriptor(
                actionId="dev.deckr.clock.time",
                name="Clock",
            )
        },
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    hardware_inventory = HardwareInventory(
        managerId="mirabox-main",
        managerEndpoint=hardware_manager_address("mirabox-main"),
        sessionId="manager-session",
        timestamp=FIXED_NOW,
        labels={"room": "office"},
        devices={
            "deck-1": {
                "deviceRef": {
                    "managerId": "mirabox-main",
                    "deviceId": "deck-1",
                    "fingerprint": "fingerprint:deck-1",
                },
                "descriptor": descriptor,
            }
        },
    ).model_dump(by_alias=True, exclude_none=True, mode="json")

    return [
        _fixture(
            artifact_id="dev.deckr.fixture.actions.settings_request.valid.v1",
            path="fixtures/valid/actions/settings-request.v1.json",
            title="Valid settingsRequest action message",
            schema_path="schemas/actions/actions.v1.schema.json",
            payload=settings_request,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.device_available.valid.v1",
            path="fixtures/valid/hardware/device-available.v1.json",
            title="Valid deviceAvailable hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_available,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.control_input.valid.v1",
            path="fixtures/valid/hardware/control-input.v1.json",
            title="Valid controlInput hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_input,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.services.service_command.valid.v1",
            path="fixtures/valid/services/service-command.v1.json",
            title="Valid serviceCommand service message",
            schema_path="schemas/services/services.v1.schema.json",
            payload=service_command,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.endpoint_presence.valid.v1",
            path="fixtures/valid/state/endpoint-presence.v1.json",
            title="Valid endpoint presence state",
            schema_path="schemas/state/endpoint-presence.v1.schema.json",
            payload=EndpointPresence(
                endpoint=action_provider_address("clock-main"),
                lane=ACTIONS_LANE,
                sessionId="provider-session",
                timestamp=FIXED_NOW,
                ttlSeconds=30,
                metadata={"runtime": "python"},
            ).model_dump(by_alias=True, exclude_none=True, mode="json"),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.hardware_inventory.valid.v1",
            path="fixtures/valid/state/hardware-inventory.v1.json",
            title="Valid hardware inventory state",
            schema_path="schemas/state/hardware-inventory.v1.schema.json",
            payload=hardware_inventory,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.device_claim.valid.v1",
            path="fixtures/valid/state/device-claim.v1.json",
            title="Valid device claim state",
            schema_path="schemas/state/device-claim.v1.schema.json",
            payload=DeviceClaim(
                claimedByEndpoint=controller_address("controller-main"),
                claimedBySessionId="controller-session",
                timestamp=FIXED_NOW,
                ttlSeconds=30,
            ).model_dump(by_alias=True, exclude_none=True, mode="json"),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.action_provider_catalog.valid.v1",
            path="fixtures/valid/state/action-provider-catalog.v1.json",
            title="Valid action provider catalog state",
            schema_path="schemas/state/action-provider-catalog.v1.schema.json",
            payload=action_catalog,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.service_catalog.valid.v1",
            path="fixtures/valid/state/service-catalog.v1.json",
            title="Valid service catalog state",
            schema_path="schemas/state/service-catalog.v1.schema.json",
            payload=ServiceCatalog(
                serviceId="media-home",
                serviceEndpoint=service_address("media-home"),
                serviceNamespace="dev.deckr.media.service",
                sessionId="service-session",
                supportedOperations=("play", "pause"),
                viewPrefixes=("zones",),
                timestamp=FIXED_NOW,
                labels={"room": "office"},
                annotations={"runtime": "python"},
            ).model_dump(by_alias=True, exclude_none=True, mode="json"),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.service_status.valid.v1",
            path="fixtures/valid/state/service-status.v1.json",
            title="Valid service status state",
            schema_path="schemas/state/service-status.v1.schema.json",
            payload=ServiceStatus(
                serviceId="media-home",
                serviceEndpoint=service_address("media-home"),
                serviceNamespace="dev.deckr.media.service",
                sessionId="service-session",
                status=ServiceStatusValue.AVAILABLE,
                timestamp=FIXED_NOW,
            ).model_dump(by_alias=True, exclude_none=True, mode="json"),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.actions.settings_request.invalid_missing_target.v1",
            path="fixtures/invalid/actions/settings-request-missing-target.v1.json",
            title="Invalid settingsRequest missing target",
            schema_path="schemas/actions/actions.v1.schema.json",
            payload={**settings_request, "body": {}},
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.control_input.invalid_missing_device_ref.v1",
            path="fixtures/invalid/hardware/control-input-missing-device-ref.v1.json",
            title="Invalid controlInput missing deviceRef",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload={**hardware_input, "body": {"eventType": "press"}},
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.services.service_command.invalid_missing_namespace.v1",
            path="fixtures/invalid/services/service-command-missing-namespace.v1.json",
            title="Invalid serviceCommand missing serviceNamespace",
            schema_path="schemas/services/services.v1.schema.json",
            payload={
                **service_command,
                "body": {"operation": "play", "params": {"zone": "kitchen"}},
            },
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.state.endpoint_presence.invalid_missing_session.v1",
            path="fixtures/invalid/state/endpoint-presence-missing-session.v1.json",
            title="Invalid endpoint presence missing sessionId",
            schema_path="schemas/state/endpoint-presence.v1.schema.json",
            payload={
                "endpoint": "action_provider:clock-main",
                "lane": "actions",
                "timestamp": "2026-04-29T10:00:00Z",
                "ttlSeconds": 30,
            },
            valid=False,
        ),
    ]


def _add_vectors(add_artifact, *, fixtures: list[dict[str, Any]]) -> None:
    valid_fixture_by_path = {
        fixture["path"]: fixture["payload"]
        for fixture in fixtures
        if fixture["kind"] == "fixture" and fixture["valid"]
    }
    action_fixture_path = "fixtures/valid/actions/settings-request.v1.json"
    action_message = DeckrMessage.from_dict(valid_fixture_by_path[action_fixture_path])

    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.key_tokens.v1",
        path="vectors/key-tokens.v1.json",
        title="Key token encoding vectors",
        description="Acceptance vectors for Deckr NATS-safe key token encoding.",
        payload={
            "schema": "dev.deckr.vector.key_tokens.v1",
            "cases": [
                _key_token_case("deck_1"),
                _key_token_case("b64_native"),
                _key_token_case("deck:one"),
                _key_token_case("provider with spaces"),
                _key_token_case("elgato.com.example.plugin"),
            ],
        },
    )
    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.state_keys.v1",
        path="vectors/state-keys.v1.json",
        title="Current-state key vectors",
        description="Acceptance vectors for Deckr current-state key helpers and parsers.",
        payload={
            "schema": "dev.deckr.vector.state_keys.v1",
            "cases": [
                {
                    "id": "presence.actions.action_provider",
                    "helper": "presence_endpoint_key",
                    "input": {
                        "lane": "actions",
                        "endpoint": "action_provider:elgato.com.example.plugin",
                    },
                    "key": presence_endpoint_key(
                        lane="actions",
                        endpoint="action_provider:elgato.com.example.plugin",
                    ),
                    "parsed": {
                        "lane": "actions",
                        "endpoint": "action_provider:elgato.com.example.plugin",
                    },
                },
                {
                    "id": "inventory.hardware",
                    "helper": "hardware_inventory_key",
                    "input": {"managerId": "mirabox-main"},
                    "key": hardware_inventory_key("mirabox-main"),
                    "parsed": {"managerId": "mirabox-main"},
                },
                {
                    "id": "claim.device",
                    "helper": "device_claim_key",
                    "input": {"managerId": "room/a", "deviceId": "deck:one"},
                    "key": device_claim_key(manager_id="room/a", device_id="deck:one"),
                    "parsed": {"managerId": "room/a", "deviceId": "deck:one"},
                },
                {
                    "id": "catalog.actions.providers",
                    "helper": "action_provider_catalog_key",
                    "input": {"providerInstanceId": "provider with spaces"},
                    "key": action_provider_catalog_key("provider with spaces"),
                    "parsed": {"providerInstanceId": "provider with spaces"},
                },
                {
                    "id": "catalog.services",
                    "helper": "service_catalog_key",
                    "input": {"serviceId": "media-home"},
                    "key": service_catalog_key("media-home"),
                    "parsed": {"serviceId": "media-home"},
                },
                {
                    "id": "status.services",
                    "helper": "service_status_key",
                    "input": {"serviceId": "media-home"},
                    "key": service_status_key("media-home"),
                    "parsed": {"serviceId": "media-home"},
                },
                {
                    "id": "view.services",
                    "helper": "service_view_key",
                    "input": {
                        "serviceId": "media-home",
                        "serviceNamespace": "dev.deckr.media.service",
                        "tokens": ["zones", "Kitchen/Main"],
                    },
                    "key": service_view_key(
                        "media-home",
                        "dev.deckr.media.service",
                        "zones",
                        "Kitchen/Main",
                    ),
                    "parsed": {
                        "serviceId": "media-home",
                        "serviceNamespace": "dev.deckr.media.service",
                        "tokens": ["zones", "Kitchen/Main"],
                    },
                },
            ],
        },
    )
    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.nats_lane.v1",
        path="vectors/nats-lane.v1.json",
        title="NATS lane subject and header vectors",
        description="Acceptance vectors for Deckr NATS lane subject and header hints.",
        payload={
            "schema": "dev.deckr.vector.nats_lane.v1",
            "cases": [
                {
                    "id": "actions.settings_request",
                    "fixture": action_fixture_path,
                    "subject": _subject_for(action_message),
                    "headers": dict(_headers_for(action_message)),
                }
            ],
        },
    )


def _device_descriptor() -> DeviceDescriptor:
    return DeviceDescriptor.model_validate(
        {
            "deviceId": "deck-1",
            "fingerprint": "fingerprint:deck-1",
            "displayName": "Example Deck",
            "manufacturer": "Deckr",
            "model": "Fixture Panel",
            "controls": [
                {
                    "controlId": "key.0.0",
                    "kind": "key",
                    "geometry": {
                        "x": 0,
                        "y": 0,
                        "width": 1,
                        "height": 1,
                        "unit": "grid",
                    },
                    "inputCapabilities": [
                        {
                            "capabilityId": "button.press",
                            "family": "dev.deckr.input.button",
                            "type": "activation",
                            "direction": "input",
                            "access": ["emits"],
                            "eventTypes": ["press"],
                            "valueSchema": button_activation_value_schema().model_dump(
                                by_alias=True,
                                exclude_none=True,
                                mode="json",
                            ),
                        }
                    ],
                    "outputCapabilities": [
                        {
                            "capabilityId": "raster.bitmap",
                            "family": "dev.deckr.output.raster",
                            "type": "bitmap",
                            "direction": "output",
                            "access": ["settable", "invokable"],
                            "commandTypes": ["set_frame", "clear"],
                            "commandSchema": raster_bitmap_command_schema(
                                width=72,
                                height=72,
                            ).model_dump(
                                by_alias=True,
                                exclude_none=True,
                                mode="json",
                            ),
                        }
                    ],
                }
            ],
        }
    )


def _settings_target() -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_instance",
        controllerId="controller-main",
        configId="office-panel",
        providerInstanceId="clock-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
        stableId="clock",
    )


def _stable_wire_message(message: DeckrMessage, *, message_id: str) -> dict[str, Any]:
    wire = _stable_message(message, message_id=message_id)
    body = wire.get("body")
    if isinstance(body, dict) and "occurredAt" in body:
        body["occurredAt"] = "2026-04-29T10:00:00Z"
        wire = DeckrMessage.from_dict(wire).to_dict()
    return wire


def _stable_message(message: DeckrMessage, *, message_id: str) -> dict[str, Any]:
    wire = message.to_dict()
    wire["messageId"] = message_id
    wire["createdAt"] = "2026-04-29T10:00:00Z"
    return DeckrMessage.from_dict(wire).to_dict()


def _fixture(
    *,
    artifact_id: str,
    path: str,
    title: str,
    schema_path: str,
    payload: Mapping[str, Any],
    valid: bool = True,
) -> dict[str, Any]:
    return {
        "kind": "fixture",
        "artifact_id": artifact_id,
        "path": path,
        "title": title,
        "description": (
            "Valid fixture for schema validation."
            if valid
            else "Invalid fixture that must be rejected by schema validation."
        ),
        "schemaPath": schema_path,
        "valid": valid,
        "payload": payload,
    }


def _key_token_case(raw: str) -> dict[str, str]:
    encoded = encode_key_token(raw)
    return {
        "raw": raw,
        "encoded": encoded,
        "decoded": decode_key_token(encoded),
    }


def _model_schema(model: type[BaseModel], *, schema_id: str, title: str) -> dict[str, Any]:
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    schema["$schema"] = JSON_SCHEMA_URI
    schema["$id"] = schema_id
    schema["title"] = title
    schema["x-deckr-schema-version"] = SPEC_VERSION
    return schema


def _package_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _write_json(path: Path, payload: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _render_index(manifest: Mapping[str, Any]) -> str:
    rows = "\n".join(
        _artifact_row(artifact) for artifact in manifest["artifacts"]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Deckr Contract v1</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    body {{
      margin: 0;
      background: #f6f7f9;
      color: #17202a;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 24px 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2rem;
      letter-spacing: 0;
    }}
    p {{
      margin: 0 0 20px;
      max-width: 760px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border: 1px solid #d9dee7;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid #e5e9f0;
      text-align: left;
      vertical-align: top;
      font-size: 0.92rem;
    }}
    th {{
      background: #edf1f7;
      font-size: 0.78rem;
      text-transform: uppercase;
      color: #4c596a;
    }}
    code {{
      font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
      font-size: 0.88em;
    }}
    a {{
      color: #0b63ce;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Deckr Contract v1</h1>
    <p>
      Generated contract artifact browser for Deckr package
      <code>{html.escape(str(manifest["deckrPackageVersion"]))}</code>.
      The authoritative manifest is <a href="manifest.json">manifest.json</a>.
    </p>
    <table>
      <thead>
        <tr>
          <th>Kind</th>
          <th>Artifact</th>
          <th>Path</th>
          <th>Description</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </main>
</body>
</html>
"""


def _artifact_row(artifact: Mapping[str, Any]) -> str:
    kind = html.escape(str(artifact["kind"]))
    artifact_id = html.escape(str(artifact["id"]))
    title = html.escape(str(artifact["title"]))
    path = html.escape(str(artifact["path"]))
    description = html.escape(str(artifact["description"]))
    return f"""        <tr>
          <td><code>{kind}</code></td>
          <td>{title}<br><code>{artifact_id}</code></td>
          <td><a href="{path}"><code>{path}</code></a></td>
          <td>{description}</td>
        </tr>"""


def main() -> None:
    generate_contract_artifacts()


if __name__ == "__main__":
    main()
