from __future__ import annotations

import html
import json
import shutil
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ACTION_MESSAGES_SCHEMA_ID,
    ACTION_INSTANCE_CREATED,
    BINDING_OUTPUT,
    OPEN_PAGE,
    SETTINGS_REQUEST,
    ActionDescriptor,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
    ActionProviderCatalog,
    BindingMetadata,
    BindingOutputBody,
    DynamicPageCommand,
    OpenPageBody,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
    SettingsTargetRef,
    action_message,
    action_message_schema,
    context_subject,
    parse_settings_target_key,
    settings_target_key,
)
from deckr.actions.state import action_provider_catalog_key
from deckr.contracts.lanes import (
    CORE_LANE_CONTRACTS,
    DeliverySemantics,
    LaneContract,
    MessageFamilyDelivery,
)
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    SERVICES_LANE,
    DeckrMessage,
    broadcast_target,
    controller_address,
    endpoint_target,
    entity_subject,
    hardware_manager_address,
    service_address,
)
from deckr.contracts.nats import (
    lane_message_headers,
    lane_message_payload,
    lane_message_subject,
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
    CAPABILITY_STATE_CHANGED,
    CAPABILITY_STATE_REQUEST,
    COMMAND_REJECTED,
    CONTROL_COMMAND,
    HARDWARE_MESSAGES_SCHEMA_ID,
    CapabilityStateChangedMessage,
    CapabilityStateRequestMessage,
    CommandRejectedMessage,
    ControlCommandMessage,
    control_input_message,
    device_available_message,
    hardware_message,
    hardware_message_schema,
    hardware_subject_for_capability,
)
from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_MESSAGES_SCHEMA_ID,
    ServiceCommandBody,
    ServiceCommandReplyBody,
    service_command_reply_message,
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

CONTRACT_VERSION = "v1"
SPEC_VERSION = "1"
ASYNCAPI_VERSION = "3.0.0"
ASYNCAPI_REACT_COMPONENT_VERSION = "2.6.5"
FIXED_NOW = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
JSON_SCHEMA_DRAFT_07_URI = "http://json-schema.org/draft-07/schema#"
ASYNCAPI_SCHEMA_FORMAT = "application/schema+json;version=draft-07"

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

SCHEMA_COMPONENTS = {
    "schemas/actions/actions.v1.schema.json": "ActionsLaneEnvelope",
    "schemas/hardware/hardware-messages.v1.schema.json": "HardwareMessagesLaneEnvelope",
    "schemas/services/services.v1.schema.json": "ServicesLaneEnvelope",
    "schemas/hardware/device-descriptor.v1.schema.json": "DeviceDescriptor",
    "schemas/hardware/control-descriptor.v1.schema.json": "ControlDescriptor",
    "schemas/hardware/capability-descriptor.v1.schema.json": "CapabilityDescriptor",
    "schemas/state/endpoint-presence.v1.schema.json": "EndpointPresenceState",
    "schemas/state/hardware-inventory.v1.schema.json": "HardwareInventoryState",
    "schemas/state/device-claim.v1.schema.json": "DeviceClaimState",
    "schemas/state/action-provider-catalog.v1.schema.json": (
        "ActionProviderCatalogState"
    ),
    "schemas/state/service-catalog.v1.schema.json": "ServiceCatalogState",
    "schemas/state/service-status.v1.schema.json": "ServiceStatusState",
}

LANE_SCHEMA_PATHS = {
    ACTIONS_LANE: "schemas/actions/actions.v1.schema.json",
    HARDWARE_MESSAGES_LANE: "schemas/hardware/hardware-messages.v1.schema.json",
    SERVICES_LANE: "schemas/services/services.v1.schema.json",
}

LANE_ASYNCAPI_COMPONENTS = {
    ACTIONS_LANE: {
        "channel": "actionsLane",
        "message": "ActionsLaneMessage",
        "channel_message": "actionsLaneEnvelope",
        "publish_operation": "publishActionsLaneMessage",
        "receive_operation": "receiveActionsLaneMessages",
        "title": "Actions lane",
        "summary": "Action provider and controller protocol messages.",
    },
    HARDWARE_MESSAGES_LANE: {
        "channel": "hardwareMessagesLane",
        "message": "HardwareMessagesLaneMessage",
        "channel_message": "hardwareMessagesLaneEnvelope",
        "publish_operation": "publishHardwareMessagesLaneMessage",
        "receive_operation": "receiveHardwareMessagesLaneMessages",
        "title": "Hardware messages lane",
        "summary": "Hardware manager and controller protocol messages.",
    },
    SERVICES_LANE: {
        "channel": "servicesLane",
        "message": "ServicesLaneMessage",
        "channel_message": "servicesLaneEnvelope",
        "publish_operation": "publishServicesLaneMessage",
        "receive_operation": "receiveServicesLaneMessages",
        "title": "Services lane",
        "summary": "Endpoint-addressed service command and reply messages.",
    },
}


def generate_contract_artifacts(output_root: Path | None = None) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    bundle_root = output_root or repo_root / "contract" / CONTRACT_VERSION
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)

    artifacts: list[dict[str, Any]] = []
    schema_payloads: dict[str, Mapping[str, Any]] = {}
    valid_fixture_payloads: dict[str, Mapping[str, Any]] = {}

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
        if kind == "schema":
            if not isinstance(payload, Mapping):
                raise TypeError(f"Schema artifact {path!r} must be a mapping")
            schema_payloads[path] = payload
        if kind == "fixture" and metadata.get("valid") is True:
            if not isinstance(payload, Mapping):
                raise TypeError(f"Fixture artifact {path!r} must be a mapping")
            valid_fixture_payloads[path] = payload
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

    package_version = _package_version(repo_root)
    asyncapi_document = _asyncapi_document(
        package_version=package_version,
        schema_payloads=schema_payloads,
        fixtures=fixtures,
        valid_fixture_payloads=valid_fixture_payloads,
    )
    add_artifact(
        kind="spec",
        artifact_id="dev.deckr.asyncapi.v1",
        path="asyncapi.json",
        title="Deckr AsyncAPI contract",
        description="AsyncAPI document for Deckr NATS lane contracts.",
        payload=asyncapi_document,
        format="asyncapi",
        asyncapi=ASYNCAPI_VERSION,
    )

    manifest = {
        "schema": "dev.deckr.contract.bundle.v1",
        "bundle": "deckr-contract-v1",
        "specVersion": SPEC_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "deckrPackageVersion": package_version,
        "description": "Generated Deckr v1 contract artifact bundle.",
        "artifacts": sorted(artifacts, key=lambda item: (item["kind"], item["path"])),
    }
    _write_json(bundle_root / "manifest.json", manifest)
    _write_text(bundle_root / "index.html", _render_index(manifest, asyncapi_document))


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
    descriptor_payload = descriptor.model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    control_payload = descriptor.controls[0].model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    capability_payload = descriptor.controls[0].input_capabilities[0].model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    device_ref = DeviceRef(
        managerId="mirabox-main",
        deviceId="deck-1",
        fingerprint="fingerprint:deck-1",
    )
    control_ref = ControlRef(deviceRef=device_ref, controlId="key.0.0")
    output_capability_ref = CapabilityRef(
        deviceRef=device_ref,
        controlId="key.0.0",
        capabilityId="raster.bitmap",
    )
    input_capability_ref = CapabilityRef(
        deviceRef=device_ref,
        controlId="key.0.0",
        capabilityId="button.press",
    )
    binding = _binding_metadata(
        device_ref=device_ref,
        control_ref=control_ref,
        output_capability_ref=output_capability_ref,
    )
    action_instance_metadata = ActionInstanceMetadata(
        providerInstanceId="clock-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
        configId="office-panel",
        contextId="clock-context",
    )
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
    action_instance_created = _stable_message(
        action_message(
            sender=controller_address("controller-main"),
            sender_session_id="controller-session",
            recipient=endpoint_target(action_provider_address("clock-main")),
            recipient_session_id="provider-session",
            message_type=ACTION_INSTANCE_CREATED,
            body=ActionInstanceLifecycleBody(
                metadata=action_instance_metadata,
                settings={"format": "24h"},
            ),
            subject=context_subject(
                "clock-context",
                provider_instance_id="clock-main",
                provider_id="dev.deckr.clock",
                config_id="office-panel",
                action_instance_id="clock-instance-1",
            ),
        ),
        message_id="fixture-action-instance-created",
    )
    binding_output = _stable_message(
        action_message(
            sender=action_provider_address("clock-main"),
            sender_session_id="provider-session",
            recipient=endpoint_target(controller_address("controller-main")),
            recipient_session_id="controller-session",
            message_type=BINDING_OUTPUT,
            body=BindingOutputBody(
                binding=binding,
                capability=output_capability_ref,
                commandType="clear",
                params={},
                generation=1,
            ),
            subject=context_subject(
                "clock-context",
                provider_instance_id="clock-main",
                provider_id="dev.deckr.clock",
                config_id="office-panel",
                action_instance_id="clock-instance-1",
                binding_id="binding-1",
            ),
        ),
        message_id="fixture-action-binding-output",
    )
    open_page = _stable_message(
        action_message(
            sender=action_provider_address("clock-main"),
            sender_session_id="provider-session",
            recipient=endpoint_target(controller_address("controller-main")),
            recipient_session_id="controller-session",
            message_type=OPEN_PAGE,
            body=OpenPageBody(
                descriptor=DynamicPageCommand(
                    pageId="clock-page",
                    bindings=(
                        PageChildBindingDescriptor(
                            controlId="key.0.0",
                            target=PageChildBindingTarget(kind="self"),
                            itemKey="clock",
                            handler="default",
                            settings={"title": "Clock"},
                        ),
                    ),
                )
            ),
            subject=context_subject(
                "clock-context",
                provider_instance_id="clock-main",
                provider_id="dev.deckr.clock",
                config_id="office-panel",
                action_instance_id="clock-instance-1",
                binding_id="binding-1",
            ),
        ),
        message_id="fixture-action-open-page",
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
    hardware_command = _stable_message(
        hardware_message(
            sender=controller_address("controller-main"),
            sender_session_id="controller-session",
            recipient=endpoint_target(hardware_manager_address("mirabox-main")),
            recipient_session_id="manager-session",
            message_type=CONTROL_COMMAND,
            body=ControlCommandMessage(
                deviceRef=device_ref,
                controlId="key.0.0",
                capabilityId="raster.bitmap",
                commandType="clear",
                params={},
            ),
            subject=hardware_subject_for_capability(output_capability_ref),
        ),
        message_id="fixture-hardware-control-command",
    )
    hardware_state_changed = _stable_wire_message(
        hardware_message(
            sender=hardware_manager_address("mirabox-main"),
            sender_session_id="manager-session",
            recipient=broadcast_target(scope="controllers", endpoint_family="controller"),
            message_type=CAPABILITY_STATE_CHANGED,
            body=CapabilityStateChangedMessage(
                deviceRef=device_ref,
                controlId="key.0.0",
                capabilityId="button.press",
                stateType="pressed",
                value=False,
                sequence=2,
                occurredAt=FIXED_NOW,
            ),
            subject=hardware_subject_for_capability(input_capability_ref),
        ),
        message_id="fixture-hardware-state-changed",
    )
    hardware_state_request = _stable_message(
        hardware_message(
            sender=controller_address("controller-main"),
            sender_session_id="controller-session",
            recipient=endpoint_target(hardware_manager_address("mirabox-main")),
            recipient_session_id="manager-session",
            message_type=CAPABILITY_STATE_REQUEST,
            body=CapabilityStateRequestMessage(
                deviceRef=device_ref,
                controlId="key.0.0",
                capabilityId="button.press",
                stateType="pressed",
                params={},
            ),
            subject=hardware_subject_for_capability(input_capability_ref),
        ),
        message_id="fixture-hardware-state-request",
    )
    hardware_command_rejected = _stable_message(
        hardware_message(
            sender=hardware_manager_address("mirabox-main"),
            sender_session_id="manager-session",
            recipient=endpoint_target(controller_address("controller-main")),
            recipient_session_id="controller-session",
            message_type=COMMAND_REJECTED,
            body=CommandRejectedMessage(
                deviceRef=device_ref,
                controlId="key.0.0",
                capabilityId="raster.bitmap",
                commandType="set_frame",
                reason="unsupported",
                message="frame format unsupported",
            ),
            subject=hardware_subject_for_capability(output_capability_ref),
            in_reply_to="fixture-hardware-control-command",
        ),
        message_id="fixture-hardware-command-rejected",
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
    service_reply = _stable_message(
        service_command_reply_message(
            sender=service_address("media-home"),
            sender_session_id="service-session",
            recipient=endpoint_target(controller_address("controller-main")),
            recipient_session_id="controller-session",
            body=ServiceCommandReplyBody(
                serviceNamespace="dev.deckr.media.service",
                operation="play",
                status="ok",
                result={"accepted": True},
            ),
            subject=entity_subject(
                "service",
                serviceId="media-home",
                namespace="dev.deckr.media.service",
                operation="play",
            ),
            in_reply_to="fixture-service-command",
        ),
        message_id="fixture-service-command-reply",
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
            artifact_id="dev.deckr.fixture.actions.action_instance_created.valid.v1",
            path="fixtures/valid/actions/action-instance-created.v1.json",
            title="Valid actionInstanceCreated action message",
            schema_path="schemas/actions/actions.v1.schema.json",
            payload=action_instance_created,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.actions.binding_output.valid.v1",
            path="fixtures/valid/actions/binding-output.v1.json",
            title="Valid bindingOutput action message",
            schema_path="schemas/actions/actions.v1.schema.json",
            payload=binding_output,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.actions.open_page.valid.v1",
            path="fixtures/valid/actions/open-page.v1.json",
            title="Valid openPage action message",
            schema_path="schemas/actions/actions.v1.schema.json",
            payload=open_page,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.device_descriptor.valid.v1",
            path="fixtures/valid/hardware/device-descriptor.v1.json",
            title="Valid device descriptor",
            schema_path="schemas/hardware/device-descriptor.v1.schema.json",
            payload=descriptor_payload,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.control_descriptor.valid.v1",
            path="fixtures/valid/hardware/control-descriptor.v1.json",
            title="Valid control descriptor",
            schema_path="schemas/hardware/control-descriptor.v1.schema.json",
            payload=control_payload,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.capability_descriptor.valid.v1",
            path="fixtures/valid/hardware/capability-descriptor.v1.json",
            title="Valid capability descriptor",
            schema_path="schemas/hardware/capability-descriptor.v1.schema.json",
            payload=capability_payload,
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
            artifact_id="dev.deckr.fixture.hardware.control_command.valid.v1",
            path="fixtures/valid/hardware/control-command.v1.json",
            title="Valid controlCommand hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_command,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.capability_state_changed.valid.v1",
            path="fixtures/valid/hardware/capability-state-changed.v1.json",
            title="Valid capabilityStateChanged hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_state_changed,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.capability_state_request.valid.v1",
            path="fixtures/valid/hardware/capability-state-request.v1.json",
            title="Valid capabilityStateRequest hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_state_request,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.hardware.command_rejected.valid.v1",
            path="fixtures/valid/hardware/command-rejected.v1.json",
            title="Valid commandRejected hardware message",
            schema_path="schemas/hardware/hardware-messages.v1.schema.json",
            payload=hardware_command_rejected,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.services.service_command.valid.v1",
            path="fixtures/valid/services/service-command.v1.json",
            title="Valid serviceCommand service message",
            schema_path="schemas/services/services.v1.schema.json",
            payload=service_command,
        ),
        _fixture(
            artifact_id="dev.deckr.fixture.services.service_command_reply.valid.v1",
            path="fixtures/valid/services/service-command-reply.v1.json",
            title="Valid serviceCommandReply service message",
            schema_path="schemas/services/services.v1.schema.json",
            payload=service_reply,
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
            artifact_id="dev.deckr.fixture.hardware.capability_descriptor.invalid_missing_family.v1",
            path="fixtures/invalid/hardware/capability-descriptor-missing-family.v1.json",
            title="Invalid capability descriptor missing family",
            schema_path="schemas/hardware/capability-descriptor.v1.schema.json",
            payload={
                key: value for key, value in capability_payload.items() if key != "family"
            },
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
    lane_fixture_paths = [
        fixture["path"]
        for fixture in fixtures
        if fixture["kind"] == "fixture"
        and fixture["valid"]
        and fixture["schemaPath"]
        in {
            "schemas/actions/actions.v1.schema.json",
            "schemas/hardware/hardware-messages.v1.schema.json",
            "schemas/services/services.v1.schema.json",
        }
    ]

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
                {
                    "id": "settings.target.action_instance",
                    "helper": "settings_target_key",
                    "input": {"target": _settings_target().to_dict()},
                    "key": settings_target_key(_settings_target()),
                    "parsed": {
                        "target": parse_settings_target_key(
                            settings_target_key(_settings_target())
                        ).to_dict()
                    },
                },
            ],
        },
    )
    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.identity.v1",
        path="vectors/identity.v1.json",
        title="Endpoint and subject identity vectors",
        description="Acceptance vectors for Deckr endpoint identity and entity subject helpers.",
        payload={
            "schema": "dev.deckr.vector.identity.v1",
            "endpointCases": [
                {
                    "id": "controller.valid",
                    "input": "controller:controller-main",
                    "valid": True,
                    "family": "controller",
                    "endpointId": "controller-main",
                },
                {
                    "id": "hardware_manager.valid",
                    "input": "hardware_manager:mirabox-main",
                    "valid": True,
                    "family": "hardware_manager",
                    "endpointId": "mirabox-main",
                },
                {
                    "id": "action_provider.valid",
                    "input": "action_provider:clock-main",
                    "valid": True,
                    "family": "action_provider",
                    "endpointId": "clock-main",
                },
                {
                    "id": "service.valid",
                    "input": "service:media-home",
                    "valid": True,
                    "family": "service",
                    "endpointId": "media-home",
                },
                {
                    "id": "endpoint.empty-id",
                    "input": "controller:",
                    "valid": False,
                },
                {
                    "id": "endpoint.unknown-family",
                    "input": "driver:mirabox-main",
                    "valid": False,
                },
            ],
            "subjectCases": [
                {
                    "id": "context.full",
                    "helper": "context_subject",
                    "input": {
                        "contextId": "clock-context",
                        "providerInstanceId": "clock-main",
                        "providerId": "dev.deckr.clock",
                        "configId": "office-panel",
                        "actionInstanceId": "clock-instance-1",
                        "bindingId": "binding-1",
                    },
                    "subject": context_subject(
                        "clock-context",
                        provider_instance_id="clock-main",
                        provider_id="dev.deckr.clock",
                        config_id="office-panel",
                        action_instance_id="clock-instance-1",
                        binding_id="binding-1",
                    ).model_dump(by_alias=True, exclude_none=True, mode="json"),
                },
                {
                    "id": "hardware.capability",
                    "helper": "hardware_subject_for_capability",
                    "input": {
                        "deviceRef": {
                            "managerId": "mirabox-main",
                            "deviceId": "deck-1",
                        },
                        "controlId": "key.0.0",
                        "capabilityId": "raster.bitmap",
                    },
                    "subject": hardware_subject_for_capability(
                        CapabilityRef(
                            deviceRef=DeviceRef(
                                managerId="mirabox-main",
                                deviceId="deck-1",
                            ),
                            controlId="key.0.0",
                            capabilityId="raster.bitmap",
                        )
                    ).model_dump(by_alias=True, exclude_none=True, mode="json"),
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
                _nats_lane_case(path, valid_fixture_by_path[path])
                for path in lane_fixture_paths
            ],
        },
    )
    add_artifact(
        kind="vector",
        artifact_id="dev.deckr.vector.lane_runtime.v1",
        path="vectors/lane-runtime.v1.json",
        title="Lane runtime semantic vectors",
        description="Acceptance vectors for message expiry and endpoint deliverability semantics.",
        payload={
            "schema": "dev.deckr.vector.lane_runtime.v1",
            "cases": [
                {
                    "id": "direct.accepts-target-endpoint",
                    "fixture": action_fixture_path,
                    "endpoint": "controller:controller-main",
                    "endpointSessionId": "controller-session",
                    "now": "2026-04-29T10:00:00Z",
                    "targetsEndpoint": True,
                    "expired": False,
                    "deliverable": True,
                },
                {
                    "id": "direct.rejects-other-endpoint",
                    "fixture": action_fixture_path,
                    "endpoint": "controller:other",
                    "endpointSessionId": "controller-session",
                    "now": "2026-04-29T10:00:00Z",
                    "targetsEndpoint": False,
                    "expired": False,
                    "deliverable": False,
                },
                {
                    "id": "ttl-expired",
                    "message": {
                        **valid_fixture_by_path[action_fixture_path],
                        "messageId": "vector-expired-message",
                        "createdAt": "2026-04-29T10:00:00Z",
                        "ttlMs": 1,
                    },
                    "endpoint": "controller:controller-main",
                    "endpointSessionId": "controller-session",
                    "now": "2026-04-29T10:00:01Z",
                    "targetsEndpoint": True,
                    "expired": True,
                    "deliverable": False,
                },
            ],
        },
    )
def _asyncapi_document(
    *,
    package_version: str,
    schema_payloads: Mapping[str, Mapping[str, Any]],
    fixtures: list[dict[str, Any]],
    valid_fixture_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    schema_components = _asyncapi_schema_components(schema_payloads)
    channels = {
        details["channel"]: _asyncapi_lane_channel(lane, CORE_LANE_CONTRACTS[lane])
        for lane, details in LANE_ASYNCAPI_COMPONENTS.items()
    }
    messages = {
        details["message"]: _asyncapi_lane_message(
            lane=lane,
            schema_path=LANE_SCHEMA_PATHS[lane],
            fixtures=fixtures,
            valid_fixture_payloads=valid_fixture_payloads,
        )
        for lane, details in LANE_ASYNCAPI_COMPONENTS.items()
    }
    operations = {}
    for lane, details in LANE_ASYNCAPI_COMPONENTS.items():
        operations.update(_asyncapi_lane_operations(lane, details=details))

    return {
        "asyncapi": ASYNCAPI_VERSION,
        "id": "urn:dev.deckr:contract:v1",
        "info": {
            "title": "Deckr Core Contract",
            "version": package_version,
            "description": (
                "Contract-first Deckr v1 artifacts for NATS lane messages, "
                "current-state payloads, fixtures, and interop vectors."
            ),
            "license": {"name": "MIT"},
            "tags": [
                {
                    "name": "lanes",
                    "description": "Endpoint-bound Deckr message lanes over NATS.",
                },
                {
                    "name": "state",
                    "description": (
                        "Deckr current-state payload schemas and key vectors."
                    ),
                },
                {
                    "name": "interop",
                    "description": (
                        "Fixtures and vectors for cross-language contract tests."
                    ),
                },
            ],
            "externalDocs": {
                "description": "Deckr NATS bus documentation",
                "url": "https://github.com/kws/deckr/blob/main/docs/nats-bus.md",
            },
        },
        "defaultContentType": "application/json",
        "servers": {
            "deckrNats": {
                "host": "127.0.0.1:4222",
                "protocol": "nats",
                "description": (
                    "Example Deckr NATS broker. Deployments provide their own "
                    "host, authentication, and authorization."
                ),
            }
        },
        "channels": channels,
        "operations": operations,
        "components": {
            "messages": messages,
            "schemas": schema_components,
            "messageTraits": {
                "deckrNatsHeaders": {
                    "headers": _asyncapi_headers_schema(),
                    "correlationId": {
                        "description": "Deckr message id used for request/reply correlation.",
                        "location": "$message.header#/Deckr-Message-Id",
                    },
                }
            },
        },
        "x-deckr-contract-version": CONTRACT_VERSION,
        "x-deckr-spec-version": SPEC_VERSION,
        "x-deckr-artifact-manifest": "manifest.json",
        "x-deckr-current-state": _asyncapi_state_artifacts(schema_payloads),
        "x-deckr-interop-vectors": [
            "vectors/identity.v1.json",
            "vectors/key-tokens.v1.json",
            "vectors/lane-runtime.v1.json",
            "vectors/nats-lane.v1.json",
            "vectors/state-keys.v1.json",
        ],
    }


def _asyncapi_schema_components(
    schema_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    missing = sorted(set(SCHEMA_COMPONENTS) - set(schema_payloads))
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing schema artifact(s) for AsyncAPI: {names}")

    return {
        component: {
            "schemaFormat": ASYNCAPI_SCHEMA_FORMAT,
            "schema": _asyncapi_embedded_schema(schema_payloads[path], component),
            "x-deckr-schema-path": path,
            "x-deckr-schema-id": schema_payloads[path].get("$id"),
        }
        for path, component in sorted(SCHEMA_COMPONENTS.items())
    }


def _asyncapi_embedded_schema(
    schema: Mapping[str, Any],
    component: str,
) -> dict[str, Any]:
    embedded = deepcopy(dict(schema))
    if "$schema" in embedded:
        embedded["x-deckr-canonical-schema-dialect"] = embedded["$schema"]
        embedded["$schema"] = JSON_SCHEMA_DRAFT_07_URI
    _project_schema_defs_to_draft_07(embedded)
    _rewrite_local_json_schema_refs(
        embedded,
        prefix=f"#/components/schemas/{component}/schema",
    )
    return embedded


def _project_schema_defs_to_draft_07(value: Any) -> None:
    if isinstance(value, dict):
        defs = value.pop("$defs", None)
        if defs is not None:
            value["definitions"] = defs
        for item in value.values():
            _project_schema_defs_to_draft_07(item)
    elif isinstance(value, list):
        for item in value:
            _project_schema_defs_to_draft_07(item)


def _rewrite_local_json_schema_refs(value: Any, *, prefix: str) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if (
                key == "$ref"
                and isinstance(item, str)
                and item.startswith("#/")
                and not item.startswith(f"{prefix}/")
            ):
                value[key] = _asyncapi_projected_ref(item, prefix=prefix)
                continue
            if key == "mapping" and isinstance(item, dict):
                for map_key, map_value in list(item.items()):
                    if (
                        isinstance(map_value, str)
                        and map_value.startswith("#/")
                        and not map_value.startswith(f"{prefix}/")
                    ):
                        item[map_key] = _asyncapi_projected_ref(
                            map_value,
                            prefix=prefix,
                        )
                continue
            _rewrite_local_json_schema_refs(item, prefix=prefix)
    elif isinstance(value, list):
        for item in value:
            _rewrite_local_json_schema_refs(item, prefix=prefix)


def _asyncapi_projected_ref(ref: str, *, prefix: str) -> str:
    suffix = ref[1:]
    if suffix.startswith("/$defs/"):
        suffix = f"/definitions/{suffix.removeprefix('/$defs/')}"
    return f"{prefix}{suffix}"


def _asyncapi_lane_channel(lane: str, contract: LaneContract) -> dict[str, Any]:
    details = LANE_ASYNCAPI_COMPONENTS[lane]
    return {
        "address": (
            f"deckr.lane.{encode_key_token(lane)}."
            "{senderFamily}.{senderEndpointToken}"
        ),
        "title": details["title"],
        "description": (
            "NATS subject pattern for Deckr logical lane messages. "
            "`senderEndpointToken` is encoded with the Deckr key-token rules."
        ),
        "parameters": {
            "senderFamily": {
                "description": "Deckr endpoint family of the sender.",
                "enum": sorted(contract.allowed_sender_families or ()),
            },
            "senderEndpointToken": {
                "description": (
                    "NATS-safe encoded token for the sender endpoint id. "
                    "Decode it with the Deckr key-token vectors."
                ),
                "examples": ["controller-main", "clock-main"],
            },
        },
        "messages": {
            details["channel_message"]: {
                "$ref": f"#/components/messages/{details['message']}"
            }
        },
        "x-deckr-lane": lane,
        "x-deckr-schema-path": LANE_SCHEMA_PATHS[lane],
        "x-deckr-message-types": sorted(contract.message_types),
        "x-deckr-allowed-recipient-families": sorted(
            contract.allowed_recipient_families or ()
        ),
        "x-deckr-broadcast-targets": dict(contract.broadcast_targets),
        "x-deckr-delivery": _asyncapi_delivery(contract.delivery),
    }


def _asyncapi_lane_message(
    *,
    lane: str,
    schema_path: str,
    fixtures: list[dict[str, Any]],
    valid_fixture_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    details = LANE_ASYNCAPI_COMPONENTS[lane]
    return {
        "name": lane,
        "title": f"{details['title']} envelope",
        "summary": details["summary"],
        "description": (
            "The payload is the full Deckr logical message envelope. "
            "The `body` field is discriminated by `messageType` inside the "
            "lane schema."
        ),
        "contentType": "application/json",
        "payload": {
            "$ref": f"#/components/schemas/{SCHEMA_COMPONENTS[schema_path]}"
        },
        "traits": [{"$ref": "#/components/messageTraits/deckrNatsHeaders"}],
        "examples": _asyncapi_message_examples(
            schema_path=schema_path,
            fixtures=fixtures,
            valid_fixture_payloads=valid_fixture_payloads,
        ),
        "tags": [{"name": "lanes"}],
        "x-deckr-lane": lane,
        "x-deckr-schema-path": schema_path,
    }


def _asyncapi_message_examples(
    *,
    schema_path: str,
    fixtures: list[dict[str, Any]],
    valid_fixture_payloads: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for fixture in sorted(fixtures, key=lambda item: item["path"]):
        if (
            fixture["kind"] != "fixture"
            or not fixture["valid"]
            or fixture["schemaPath"] != schema_path
        ):
            continue
        payload = valid_fixture_payloads[fixture["path"]]
        examples.append(
            {
                "name": _asyncapi_name_from_path(fixture["path"]),
                "summary": fixture["title"],
                "payload": payload,
            }
        )
    return examples


def _asyncapi_name_from_path(path: str) -> str:
    stem = Path(path).stem
    if stem.endswith(".v1"):
        stem = stem[:-3]
    parts = stem.replace("_", "-").split("-")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _asyncapi_lane_operations(
    lane: str,
    *,
    details: Mapping[str, str],
) -> dict[str, Any]:
    message_ref = {
        "$ref": (
            f"#/channels/{details['channel']}/messages/"
            f"{details['channel_message']}"
        )
    }
    channel_ref = {"$ref": f"#/channels/{details['channel']}"}
    bindings = {"nats": {"bindingVersion": "0.1.0"}}
    tags = [{"name": "lanes"}]
    return {
        details["publish_operation"]: {
            "action": "send",
            "channel": channel_ref,
            "messages": [message_ref],
            "summary": f"Publish {details['title']} messages.",
            "bindings": bindings,
            "tags": tags,
            "x-deckr-lane": lane,
        },
        details["receive_operation"]: {
            "action": "receive",
            "channel": channel_ref,
            "messages": [message_ref],
            "summary": f"Receive deliverable {details['title']} messages.",
            "bindings": bindings,
            "tags": tags,
            "x-deckr-lane": lane,
        },
    }


def _asyncapi_headers_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "Deckr-Message-Id",
            "Deckr-Message-Type",
            "Deckr-Sender",
            "Deckr-Sender-Session",
            "Deckr-Recipient",
        ],
        "properties": {
            "Deckr-Message-Id": {"type": "string"},
            "Deckr-Message-Type": {"type": "string"},
            "Deckr-Sender": {"type": "string"},
            "Deckr-Sender-Session": {"type": "string"},
            "Deckr-Recipient": {"type": "string"},
            "Deckr-Recipient-Session": {"type": "string"},
            "Deckr-In-Reply-To": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _asyncapi_delivery(delivery: DeliverySemantics | None) -> dict[str, Any] | None:
    if delivery is None:
        return None
    return {
        "persistence": delivery.persistence.value,
        "guarantee": delivery.guarantee.value,
        "replay": delivery.replay.value,
        "ordering": delivery.ordering.value,
        "orderingKeys": list(delivery.ordering_keys),
        "expiry": delivery.expiry.value,
        "localBackpressure": delivery.local_backpressure.value,
        "remoteBackpressure": delivery.remote_backpressure.value,
        "malformedMessages": delivery.malformed_messages.value,
        "messageFamilies": [
            _asyncapi_message_family(family) for family in delivery.message_families
        ],
    }


def _asyncapi_message_family(family: MessageFamilyDelivery) -> dict[str, Any]:
    return {
        "family": family.family.value,
        "messageTypes": sorted(family.message_types),
        "idempotency": family.idempotency.value if family.idempotency else None,
        "orderingKeys": list(family.ordering_keys),
    }


def _asyncapi_state_artifacts(
    schema_payloads: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "schema": schema_payloads[path].get("$id", ""),
            "component": SCHEMA_COMPONENTS[path],
            "path": path,
        }
        for path in sorted(SCHEMA_COMPONENTS)
        if path.startswith("schemas/state/")
    ]


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


def _binding_metadata(
    *,
    device_ref: DeviceRef,
    control_ref: ControlRef,
    output_capability_ref: CapabilityRef,
) -> BindingMetadata:
    return BindingMetadata(
        providerInstanceId="clock-main",
        providerId="dev.deckr.clock",
        actionId="dev.deckr.clock.time",
        actionInstanceId="clock-instance-1",
        configId="office-panel",
        contextId="clock-context",
        bindingId="binding-1",
        deviceRef=device_ref,
        controlRef=control_ref,
        itemKey="clock",
        handler="default",
        outputGeneration=1,
        matchedCapabilities=(
            {
                "requirementName": "screen",
                "capability": output_capability_ref,
                "family": "dev.deckr.output.raster",
                "type": "bitmap",
                "direction": "output",
                "commandTypes": ["set_frame", "clear"],
                "provenance": "native",
            },
        ),
    )


def _nats_lane_case(path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    stored_payload = json.loads(json.dumps(payload, sort_keys=True))
    message = DeckrMessage.from_dict(stored_payload)
    return {
        "id": Path(path).stem.removesuffix(".v1"),
        "fixture": path,
        "lane": message.lane,
        "messageType": message.message_type,
        "subject": lane_message_subject(message),
        "headers": dict(lane_message_headers(message)),
        "payloadUtf8": lane_message_payload(message).decode("utf-8"),
    }


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
    pyproject = repo_root / "libraries" / "python" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
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


def _render_index(
    manifest: Mapping[str, Any],
    asyncapi_document: Mapping[str, Any],
) -> str:
    rows = "\n".join(_artifact_row(artifact) for artifact in manifest["artifacts"])
    asyncapi_json = (
        json.dumps(asyncapi_document, sort_keys=True, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Deckr Contract v1</title>
  <link rel="stylesheet" href="https://unpkg.com/@asyncapi/react-component@{ASYNCAPI_REACT_COMPONENT_VERSION}/styles/default.min.css">
  <style>
    :root {{
      color-scheme: light;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    body {{
      margin: 0;
      background: #f7f8fb;
      color: #17202a;
    }}
    header {{
      padding: 18px 24px;
      border-bottom: 1px solid #d9dee7;
      background: #fff;
    }}
    main {{
      margin: 0;
      padding: 0;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 1.45rem;
      letter-spacing: 0;
    }}
    p {{
      margin: 0;
      max-width: 860px;
    }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      margin-top: 12px;
      font-size: 0.92rem;
    }}
    #asyncapi-viewer {{
      min-height: calc(100vh - 132px);
    }}
    #viewer-status {{
      margin: 24px;
      padding: 14px 16px;
      border: 1px solid #d9dee7;
      background: #fff;
    }}
    #artifact-fallback {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px 24px 48px;
    }}
    #artifact-fallback[hidden] {{
      display: none;
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
  <header>
    <h1>Deckr Contract v1</h1>
    <p>
      Generated contract artifact browser for Deckr package
      <code>{html.escape(str(manifest["deckrPackageVersion"]))}</code>.
    </p>
    <nav class="links" aria-label="Contract artifacts">
      <a href="asyncapi.json">AsyncAPI JSON</a>
      <a href="manifest.json">Manifest</a>
      <a href="schemas/">Schemas</a>
      <a href="fixtures/">Fixtures</a>
      <a href="vectors/">Vectors</a>
    </nav>
  </header>
  <main>
    <div id="asyncapi-viewer"></div>
    <div id="viewer-status" role="status">
      Loading the AsyncAPI browser. If CDN scripts are unavailable, use the
      artifact table below.
    </div>
    <section id="artifact-fallback" aria-label="Contract artifact table">
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
    </section>
  </main>
  <script id="asyncapi-spec" type="application/json">{asyncapi_json}</script>
  <script src="https://unpkg.com/@asyncapi/react-component@{ASYNCAPI_REACT_COMPONENT_VERSION}/browser/standalone/index.js"></script>
  <script>
    const fallback = document.getElementById("artifact-fallback");
    const status = document.getElementById("viewer-status");
    const target = document.getElementById("asyncapi-viewer");
    const rawSpec = document.getElementById("asyncapi-spec").textContent;

    try {{
      const schema = JSON.parse(rawSpec);
      if (!window.AsyncApiStandalone) {{
        throw new Error("AsyncAPI standalone renderer is unavailable");
      }}
      window.AsyncApiStandalone.render({{
        schema,
        config: {{
          schemaID: "deckr-contract-v1",
          show: {{
            sidebar: true,
            errors: true
          }}
        }}
      }}, target);
      fallback.hidden = true;
      status.hidden = true;
    }} catch (error) {{
      status.textContent = `${{error.message}}. Showing the raw artifact table.`;
    }}
  </script>
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
