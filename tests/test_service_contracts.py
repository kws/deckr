from __future__ import annotations

from datetime import UTC, datetime

import pytest
from memory_lane_substrate import MemoryStateStore
from pydantic import ValidationError

from deckr.contracts.lanes import CORE_LANE_CONTRACTS
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
)
from deckr.services.state import (
    ServiceCatalog,
    ServiceLiveState,
    ServiceStatus,
    ServiceStatusValue,
    live_service_check,
    parse_service_catalog_key,
    parse_service_status_key,
    parse_service_view_key,
    service_catalog_key,
    service_status_key,
    service_view_key,
)
from deckr.state import EndpointPresence, presence_endpoint_key


def _now() -> datetime:
    return datetime.now(UTC)


def test_service_endpoint_helpers_round_trip() -> None:
    address = service_address("sonos-home")

    assert address == EndpointAddress.model_validate("service:sonos-home")
    assert parse_service_address(address) == "sonos-home"
    assert parse_service_address("controller:main") is None


def test_services_lane_contract_accepts_direct_command_reply() -> None:
    contract = CORE_LANE_CONTRACTS[SERVICES_LANE]
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
    )

    validate_message_for_contract(command, contract)
    validate_message_for_contract(reply, contract)
    assert command.lane == SERVICES_LANE
    assert command.message_type == SERVICE_COMMAND
    assert reply.message_type == SERVICE_COMMAND_REPLY
    assert reply.in_reply_to == command.message_id


def test_services_lane_validation_thaws_frozen_json_body() -> None:
    contract = CORE_LANE_CONTRACTS[SERVICES_LANE]
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
        validate_message_for_contract(message, CORE_LANE_CONTRACTS[SERVICES_LANE])


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


def test_service_current_state_keys_round_trip() -> None:
    catalog_key = service_catalog_key("sonos-home")
    status_key = service_status_key("sonos-home")
    view_key = service_view_key(
        "sonos-home",
        "dev.deckr.sonos.service",
        "zones",
        "Kitchen/Main",
    )

    assert catalog_key == "catalog.services.sonos-home"
    assert status_key == "status.services.sonos-home"
    assert parse_service_catalog_key(catalog_key) == "sonos-home"
    assert parse_service_status_key(status_key) == "sonos-home"
    assert parse_service_view_key(view_key) == (
        "sonos-home",
        "dev.deckr.sonos.service",
        ("zones", "Kitchen/Main"),
    )


def test_service_catalog_and_status_validate_endpoint_identity() -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        ServiceCatalog(
            serviceId="sonos-home",
            serviceEndpoint=service_address("other-home"),
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            timestamp=_now(),
        )
    with pytest.raises(ValidationError, match="endpoint"):
        ServiceStatus(
            serviceId="sonos-home",
            serviceEndpoint=service_address("other-home"),
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            status=ServiceStatusValue.AVAILABLE,
            timestamp=_now(),
        )


@pytest.mark.asyncio
async def test_live_service_check_requires_presence_catalog_status_and_session() -> None:
    lease = MemoryStateStore(name="lease")
    discovery = MemoryStateStore(name="discovery")
    endpoint = service_address("sonos-home")
    await lease.put(
        presence_endpoint_key(lane=SERVICES_LANE, endpoint=endpoint),
        EndpointPresence(
            endpoint=endpoint,
            lane=SERVICES_LANE,
            sessionId="service-session",
            timestamp=_now(),
            ttlSeconds=30,
        ),
    )
    await discovery.put(
        service_catalog_key("sonos-home"),
        ServiceCatalog(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            supportedOperations=("play", "pause"),
            viewPrefixes=("view.services.sonos-home",),
            timestamp=_now(),
        ),
    )
    await discovery.put(
        service_status_key("sonos-home"),
        ServiceStatus(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="service-session",
            status=ServiceStatusValue.AVAILABLE,
            timestamp=_now(),
        ),
    )

    check = await live_service_check(
        lease,
        discovery,
        service_id="sonos-home",
        service_namespace="dev.deckr.sonos.service",
    )

    assert check.state == ServiceLiveState.AVAILABLE
    assert check.session_id == "service-session"

    await discovery.put(
        service_status_key("sonos-home"),
        ServiceStatus(
            serviceId="sonos-home",
            serviceEndpoint=endpoint,
            serviceNamespace="dev.deckr.sonos.service",
            sessionId="old-session",
            status=ServiceStatusValue.AVAILABLE,
            timestamp=_now(),
        ),
    )

    stale = await live_service_check(
        lease,
        discovery,
        service_id="sonos-home",
        service_namespace="dev.deckr.sonos.service",
    )
    assert stale.state == ServiceLiveState.INVALID
    assert stale.reason == "session_mismatch"
