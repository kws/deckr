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
    SettingsTargetRef,
    action_message_schema,
)
from deckr.beacon import (
    BEACON_ADVERTISEMENT_SCHEMA_ID,
    AdvertisementRecord,
    beacon_advertisement_key,
)
from deckr.concord import (
    CONCORD_CONTRACT_SCHEMA_ID,
    CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    ContractRecord,
    ParticipantTokenRecord,
    canonical_json_bytes,
    canonical_json_hash,
    concord_contract_key,
    concord_participant_token_key,
)
from deckr.contracts.keys import encode_key_token
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
    CapabilityRef,
    ControlRef,
    DeviceDescriptor,
    DeviceRef,
    descriptor_schema_artifacts,
)
from deckr.hardware.messages import (
    HARDWARE_MESSAGES_SCHEMA_ID,
    control_input_message,
    device_available_message,
    hardware_message_schema,
)
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HARDWARE_FEATURE_ID,
    HARDWARE_PROFILE_ID,
    HardwareAdvertisementDevice,
    HardwareBeaconPayload,
    HardwareClaimDevice,
    HardwareClaimTerms,
    ProfileCapacity,
)
from deckr.profiles import (
    ACTION_BINDING_PROFILE_ID,
    ACTIONS_FEATURE_ID,
    ACTIONS_PROFILE_ID,
    ActionBeaconDescriptor,
    ActionBindingTerms,
    ActionsBeaconPayload,
)
from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_MESSAGES_SCHEMA_ID,
    ServiceCommandBody,
    service_message_schema,
)
from deckr.substrates.nats import _headers_for, _subject_for

CONTRACT_VERSION = "v1"
SPEC_VERSION = "1"
FIXED_NOW = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

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

    schema_models: tuple[tuple[str, str, type[BaseModel], str], ...] = (
        (
            BEACON_ADVERTISEMENT_SCHEMA_ID,
            "schemas/beacon/advertisement.v1.schema.json",
            AdvertisementRecord,
            "Beacon advertisement",
        ),
        (
            CONCORD_CONTRACT_SCHEMA_ID,
            "schemas/concord/contract.v1.schema.json",
            ContractRecord,
            "Concord contract",
        ),
        (
            CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
            "schemas/concord/participant-token.v1.schema.json",
            ParticipantTokenRecord,
            "Concord participant token",
        ),
        (
            HARDWARE_PROFILE_ID,
            "schemas/profiles/hardware.v1.schema.json",
            HardwareBeaconPayload,
            "Deckr Beacon hardware profile payload",
        ),
        (
            ACTIONS_PROFILE_ID,
            "schemas/profiles/actions.v1.schema.json",
            ActionsBeaconPayload,
            "Deckr Beacon actions profile payload",
        ),
        (
            HARDWARE_CLAIM_PROFILE_ID,
            "schemas/profiles/hardware-claim.v1.schema.json",
            HardwareClaimTerms,
            "Deckr Concord hardware claim terms",
        ),
        (
            ACTION_BINDING_PROFILE_ID,
            "schemas/profiles/action-binding.v1.schema.json",
            ActionBindingTerms,
            "Deckr Concord action binding terms",
        ),
    )
    for schema_id, path, model, title in schema_models:
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
    hardware_payload = _hardware_payload(descriptor)
    actions_payload = _actions_payload()
    hardware_claim_terms = _hardware_claim_terms()
    action_binding_terms = _action_binding_terms()

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
            occurred_at=FIXED_NOW,
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
                namespace="org.example.media.service",
                operation="play",
            ),
            body=ServiceCommandBody(
                serviceNamespace="org.example.media.service",
                operation="play",
                params={"zone": "kitchen"},
            ).to_dict(),
        ),
        message_id="fixture-service-command",
    )

    hardware_advertisement = AdvertisementRecord(
        advertisementId="hardware-advertisement-1",
        featureId=HARDWARE_FEATURE_ID,
        advertiser=hardware_manager_address("mirabox-main"),
        endpoint=hardware_manager_address("mirabox-main"),
        sessionId="manager-session",
        refreshSeq=1,
        ttlSeconds=30,
        payload=hardware_payload.to_dict(),
        createdAt=FIXED_NOW,
        updatedAt=FIXED_NOW,
    ).to_dict()
    actions_advertisement = AdvertisementRecord(
        advertisementId="actions-advertisement-1",
        featureId=ACTIONS_FEATURE_ID,
        advertiser=action_provider_address("clock-main"),
        endpoint=action_provider_address("clock-main"),
        sessionId="provider-session",
        refreshSeq=1,
        ttlSeconds=30,
        payload=actions_payload.to_dict(),
        createdAt=FIXED_NOW,
        updatedAt=FIXED_NOW,
    ).to_dict()
    hardware_claim_contract = ContractRecord(
        contractId="hardware-contract-1",
        generation=1,
        profile=HARDWARE_CLAIM_PROFILE_ID,
        participants=(
            controller_address("controller-main"),
            hardware_manager_address("mirabox-main"),
        ),
        termsHash=canonical_json_hash(hardware_claim_terms),
        terms=hardware_claim_terms.to_dict(),
        createdBy=controller_address("controller-main"),
        createdAt=FIXED_NOW,
    ).to_dict()
    hardware_claim_token = ParticipantTokenRecord(
        contractId="hardware-contract-1",
        generation=1,
        participant=controller_address("controller-main"),
        sessionId="controller-session",
        tokenId="controller-token-1",
        refreshSeq=1,
        ttlSeconds=30,
        termsHash=canonical_json_hash(hardware_claim_terms),
    ).to_dict()

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
            artifact_id="dev.deckr.fixture.beacon.hardware.valid.v1",
            path="fixtures/valid/beacon/hardware-advertisement.v1.json",
            title="Valid Beacon hardware advertisement",
            schema_path="schemas/beacon/advertisement.v1.schema.json",
            payload=hardware_advertisement,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.beacon.actions.valid.v1",
            path="fixtures/valid/beacon/actions-advertisement.v1.json",
            title="Valid Beacon actions advertisement",
            schema_path="schemas/beacon/advertisement.v1.schema.json",
            payload=actions_advertisement,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.concord.hardware_claim_contract.valid.v1",
            path="fixtures/valid/concord/hardware-claim-contract.v1.json",
            title="Valid Concord hardware claim contract",
            schema_path="schemas/concord/contract.v1.schema.json",
            payload=hardware_claim_contract,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.concord.hardware_claim_token.valid.v1",
            path="fixtures/valid/concord/hardware-claim-token.v1.json",
            title="Valid Concord hardware claim participant token",
            schema_path="schemas/concord/participant-token.v1.schema.json",
            payload=hardware_claim_token,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.hardware.valid.v1",
            path="fixtures/valid/profiles/hardware.v1.json",
            title="Valid Deckr hardware Beacon profile payload",
            schema_path="schemas/profiles/hardware.v1.schema.json",
            payload=hardware_payload.to_dict(),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.actions.valid.v1",
            path="fixtures/valid/profiles/actions.v1.json",
            title="Valid Deckr actions Beacon profile payload",
            schema_path="schemas/profiles/actions.v1.schema.json",
            payload=actions_payload.to_dict(),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.hardware_claim.valid.v1",
            path="fixtures/valid/profiles/hardware-claim.v1.json",
            title="Valid Deckr hardware claim Concord terms",
            schema_path="schemas/profiles/hardware-claim.v1.schema.json",
            payload=hardware_claim_terms.to_dict(),
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.action_binding.valid.v1",
            path="fixtures/valid/profiles/action-binding.v1.json",
            title="Valid Deckr action binding Concord terms",
            schema_path="schemas/profiles/action-binding.v1.schema.json",
            payload=action_binding_terms.to_dict(),
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
            artifact_id="dev.deckr.fixture.beacon.invalid_missing_session.v1",
            path="fixtures/invalid/beacon/advertisement-missing-session.v1.json",
            title="Invalid Beacon advertisement missing sessionId",
            schema_path="schemas/beacon/advertisement.v1.schema.json",
            payload={
                key: value
                for key, value in hardware_advertisement.items()
                if key != "sessionId"
            },
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.concord.invalid_token_missing_participant.v1",
            path="fixtures/invalid/concord/token-missing-participant.v1.json",
            title="Invalid Concord participant token missing participant",
            schema_path="schemas/concord/participant-token.v1.schema.json",
            payload={
                key: value for key, value in hardware_claim_token.items() if key != "participant"
            },
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.hardware.invalid_missing_manager.v1",
            path="fixtures/invalid/profiles/hardware-missing-manager.v1.json",
            title="Invalid hardware profile payload missing managerId",
            schema_path="schemas/profiles/hardware.v1.schema.json",
            payload={
                key: value for key, value in hardware_payload.to_dict().items() if key != "managerId"
            },
            valid=False,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.profile.action_binding.invalid_missing_binding.v1",
            path="fixtures/invalid/profiles/action-binding-missing-binding.v1.json",
            title="Invalid action binding terms missing bindingId",
            schema_path="schemas/profiles/action-binding.v1.schema.json",
            payload={
                key: value
                for key, value in action_binding_terms.to_dict().items()
                if key != "bindingId"
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
    hardware_claim_terms = _hardware_claim_terms()
    action_binding_terms = _action_binding_terms()

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
        artifact_id="dev.deckr.vector.beacon_concord_keys.v1",
        path="vectors/beacon-concord-keys.v1.json",
        title="Beacon and Concord key vectors",
        description="Acceptance vectors for Beacon and Concord state key helpers.",
        payload={
            "schema": "dev.deckr.vector.beacon_concord_keys.v1",
            "cases": [
                {
                    "id": "beacon.hardware",
                    "helper": "beacon_advertisement_key",
                    "input": {
                        "featureId": HARDWARE_FEATURE_ID,
                        "advertisementId": "hardware advertisement/1",
                    },
                    "key": beacon_advertisement_key(
                        feature_id=HARDWARE_FEATURE_ID,
                        advertisement_id="hardware advertisement/1",
                    ),
                },
                {
                    "id": "concord.contract",
                    "helper": "concord_contract_key",
                    "input": {"contractId": "hardware contract/1", "generation": 1},
                    "key": concord_contract_key(
                        contract_id="hardware contract/1",
                        generation=1,
                    ),
                },
                {
                    "id": "concord.participant_token",
                    "helper": "concord_participant_token_key",
                    "input": {
                        "contractId": "hardware contract/1",
                        "generation": 1,
                        "participant": "hardware_manager:mirabox-main",
                    },
                    "key": concord_participant_token_key(
                        contract_id="hardware contract/1",
                        generation=1,
                        participant=hardware_manager_address("mirabox-main"),
                    ),
                },
            ],
        },
    )
    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.concord_terms_hash.v1",
        path="vectors/concord-terms-hash.v1.json",
        title="Concord canonical terms hash vectors",
        description="Acceptance vectors for canonical Concord term hashing.",
        payload={
            "schema": "dev.deckr.vector.concord_terms_hash.v1",
            "cases": [
                _terms_hash_case("hardware_claim", hardware_claim_terms),
                _terms_hash_case("action_binding", action_binding_terms),
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
                                mode="json",
                            ),
                        },
                    ],
                    "outputCapabilities": [
                        {
                            "capabilityId": "screen",
                            "family": "dev.deckr.output.raster",
                            "type": "bitmap",
                            "direction": "output",
                            "access": ["invokable"],
                            "commandTypes": ["set_frame", "clear"],
                            "commandSchema": raster_bitmap_command_schema().model_dump(
                                by_alias=True,
                                mode="json",
                            ),
                        },
                    ],
                }
            ],
        }
    )


def _settings_target() -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_instance",
        controllerId="controller-main",
        configId="clock-config-1",
        providerInstanceId="clock-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
    )


def _hardware_payload(descriptor: DeviceDescriptor) -> HardwareBeaconPayload:
    return HardwareBeaconPayload(
        managerId="mirabox-main",
        managerEndpoint=hardware_manager_address("mirabox-main"),
        sessionId="manager-session",
        labels={"room": "office"},
        devices={
            "deck-1": HardwareAdvertisementDevice(
                capacity=ProfileCapacity(
                    totalInstances=1,
                    claimedInstances=0,
                    availableInstances=1,
                ),
                deviceRef=DeviceRef(
                    managerId="mirabox-main",
                    deviceId="deck-1",
                    fingerprint="fingerprint:deck-1",
                ),
                descriptor=descriptor,
            )
        },
    )


def _actions_payload() -> ActionsBeaconPayload:
    return ActionsBeaconPayload(
        providerInstanceId="clock-main",
        providerEndpoint=action_provider_address("clock-main"),
        providerId="dev.deckr.clock",
        sessionId="provider-session",
        labels={"room": "office"},
        annotations={"runtime": "python"},
        actions={
            "dev.deckr.clock.time": ActionBeaconDescriptor(
                actionId="dev.deckr.clock.time",
                providerId="dev.deckr.clock",
                name="Clock",
                requirements=None,
                capacity=ProfileCapacity(claimedInstances=0),
                hints={"priority": 100},
            )
        },
    )


def _hardware_claim_terms() -> HardwareClaimTerms:
    return HardwareClaimTerms(
        claimId="claim-1",
        controllerEndpoint=controller_address("controller-main"),
        managerEndpoint=hardware_manager_address("mirabox-main"),
        managerAdvertisementId="hardware-advertisement-1",
        devices=(
            HardwareClaimDevice(
                deviceRef=DeviceRef(
                    managerId="mirabox-main",
                    deviceId="deck-1",
                    fingerprint="fingerprint:deck-1",
                ),
                instanceCount=1,
            ),
        ),
    )


def _action_binding_terms() -> ActionBindingTerms:
    device_ref = DeviceRef(
        managerId="mirabox-main",
        deviceId="deck-1",
        fingerprint="fingerprint:deck-1",
    )
    control_ref = ControlRef(deviceRef=device_ref, controlId="key.0.0")
    return ActionBindingTerms(
        bindingId="binding-1",
        controllerEndpoint=controller_address("controller-main"),
        providerEndpoint=action_provider_address("clock-main"),
        providerInstanceId="clock-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
        configId="clock-config-1",
        contextId="context-1",
        hardwareClaimId="claim-1",
        deviceRef=device_ref,
        controlRef=control_ref,
        matchedCapabilities=(
            {
                "requirementName": "press",
                "capability": CapabilityRef(
                    deviceRef=device_ref,
                    controlId="key.0.0",
                    capabilityId="button.press",
                ),
                "family": "dev.deckr.input.button",
                "type": "activation",
                "direction": "input",
                "eventTypes": ("press",),
            },
        ),
    )


def _stable_message(message: DeckrMessage, *, message_id: str) -> dict[str, Any]:
    data = message.to_dict()
    data["messageId"] = message_id
    data["createdAt"] = FIXED_NOW.isoformat().replace("+00:00", "Z")
    return data


def _stable_wire_message(message: DeckrMessage, *, message_id: str) -> dict[str, Any]:
    return _stable_message(message, message_id=message_id)


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
        "description": title,
        "schemaPath": schema_path,
        "valid": valid,
        "payload": payload,
    }


def _key_token_case(raw: str) -> dict[str, str]:
    return {"raw": raw, "token": encode_key_token(raw)}


def _terms_hash_case(
    case_id: str,
    terms: HardwareClaimTerms | ActionBindingTerms,
) -> dict[str, str]:
    canonical = canonical_json_bytes(terms).decode("utf-8")
    return {
        "id": case_id,
        "canonicalJson": canonical,
        "hash": canonical_json_hash(terms),
    }


def _model_schema(model: type[BaseModel], *, schema_id: str, title: str) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
    schema["$schema"] = JSON_SCHEMA_URI
    schema["$id"] = schema_id
    schema["title"] = title
    schema["x-deckr-schema-version"] = SPEC_VERSION
    return schema


def _write_json(path: Path, payload: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _package_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _render_index(manifest: Mapping[str, Any]) -> str:
    rows = []
    for artifact in manifest["artifacts"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(artifact['kind'])}</td>"
            f"<td>{html.escape(artifact['id'])}</td>"
            f"<td><a href=\"{html.escape(artifact['path'])}\">{html.escape(artifact['path'])}</a></td>"
            f"<td>{html.escape(artifact['title'])}</td>"
            "</tr>"
        )
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Deckr Contract Bundle</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; }}
    th {{ background: #f6f6f6; }}
  </style>
</head>
<body>
  <h1>Deckr Contract Bundle</h1>
  <p>Bundle: {bundle}</p>
  <table>
    <thead><tr><th>Kind</th><th>ID</th><th>Path</th><th>Title</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</body>
</html>
""".format(
        bundle=html.escape(str(manifest["bundle"])),
        rows="\n      ".join(rows),
    )


if __name__ == "__main__":
    generate_contract_artifacts()
