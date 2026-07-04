from __future__ import annotations

import pytest

from deckr.contracts.authority import ContractPointer
from deckr.contracts.lanes import SERVICE_LANE_CONTRACT
from deckr.contracts.messages import (
    SERVICES_LANE,
    EndpointAddress,
    controller_address,
    entity_subject,
    hardware_manager_address,
    parse_service_address,
    service_address,
)
from deckr.lanes import validate_message_for_contract
from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_COMMAND_REPLY,
    ServiceCommandBody,
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    service_command_message,
    service_command_reply_message,
    service_message_schema,
)

_CONTRACT = ContractPointer(contractId="service-contract-1", generation=1)


def test_service_endpoint_helpers_round_trip() -> None:
    address = service_address("sonos-home")

    assert address == EndpointAddress.model_validate("service:sonos-home")
    assert parse_service_address(address) == "sonos-home"
    assert parse_service_address("controller:main") is None


def test_services_lane_contract_accepts_direct_command_reply() -> None:
    contract = SERVICE_LANE_CONTRACT
    command = service_command_message(
        sender=controller_address("controller-main"),
        sender_session_id="controller-session",
        recipient=service_address("sonos-home"),
        recipient_session_id="service-session",
        subject=entity_subject(
            "service",
            serviceId="sonos-home",
            namespace="dev.deckr.sonos.service",
            operation="play",
        ),
        body=ServiceCommandBody(
            serviceNamespace="dev.deckr.sonos.service",
            operation="play",
            params={"zone": "Kitchen"},
        ),
        contract=_CONTRACT,
    )
    reply = service_command_reply_message(
        sender=service_address("sonos-home"),
        sender_session_id="service-session",
        recipient=controller_address("controller-main"),
        recipient_session_id="controller-session",
        subject=command.subject,
        in_reply_to=command.message_id,
        body=ServiceCommandReplyBody(
            serviceNamespace="dev.deckr.sonos.service",
            operation="play",
            status=ServiceCommandStatus.OK,
            result={"accepted": True},
        ),
        contract=_CONTRACT,
    )

    validate_message_for_contract(command, contract)
    validate_message_for_contract(reply, contract)
    assert command.lane == SERVICES_LANE
    assert command.message_type == SERVICE_COMMAND
    assert reply.message_type == SERVICE_COMMAND_REPLY
    assert reply.in_reply_to == command.message_id


def test_service_message_schema_exports_typed_bodies() -> None:
    schema = service_message_schema()

    assert schema["$id"] == "dev.deckr.message.services.v1"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    service_command_variant = next(
        variant
        for variant in schema["oneOf"]
        if variant["allOf"][1]["properties"]["messageType"]["const"]
        == SERVICE_COMMAND
    )
    variant_properties = service_command_variant["allOf"][1]["properties"]
    assert variant_properties["lane"]["const"] == SERVICES_LANE
    assert variant_properties["body"]["$ref"] == "#/$defs/ServiceCommandBody"


def test_services_lane_validation_thaws_frozen_json_body() -> None:
    contract = SERVICE_LANE_CONTRACT
    command = service_command_message(
        sender=controller_address("controller-main"),
        sender_session_id="controller-session",
        recipient=service_address("openhab-home"),
        recipient_session_id="service-session",
        subject=entity_subject(
            "service",
            serviceId="openhab-home",
            namespace="dev.deckr.openhab.service",
            operation="ensureItems",
        ),
        body=ServiceCommandBody(
            serviceNamespace="dev.deckr.openhab.service",
            operation="ensureItems",
            params={"items": ["KajsRoomScene"], "refresh": True},
        ),
        contract=_CONTRACT,
    )
    reply = service_command_reply_message(
        sender=service_address("openhab-home"),
        sender_session_id="service-session",
        recipient=controller_address("controller-main"),
        recipient_session_id="controller-session",
        subject=command.subject,
        in_reply_to=command.message_id,
        body=ServiceCommandReplyBody(
            serviceNamespace="dev.deckr.openhab.service",
            operation="ensureItems",
            status=ServiceCommandStatus.OK,
            result={"items": {"KajsRoomScene": {"state": "ON"}}},
        ),
        contract=_CONTRACT,
    )

    validate_message_for_contract(command, contract)
    validate_message_for_contract(reply, contract)


def test_services_lane_rejects_hardware_manager_participants() -> None:
    message = service_command_message(
        sender=hardware_manager_address("mirabox-main"),
        sender_session_id="manager-session",
        recipient=service_address("sonos-home"),
        subject=entity_subject("service", serviceId="sonos-home"),
        body={
            "serviceNamespace": "dev.deckr.sonos.service",
            "operation": "play",
        },
    )

    with pytest.raises(ValueError, match="Sender family"):
        validate_message_for_contract(message, SERVICE_LANE_CONTRACT)


def test_service_command_body_rejects_sender_authority_fields() -> None:
    with pytest.raises(ValueError, match="senderSessionId"):
        service_command_message(
            sender=controller_address("controller-main"),
            sender_session_id="controller-session",
            recipient=service_address("sonos-home"),
            subject=entity_subject("service", serviceId="sonos-home"),
            body={
                "serviceNamespace": "dev.deckr.sonos.service",
                "operation": "play",
                "senderSessionId": "not-body-authority",
            },
        )
