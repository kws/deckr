from __future__ import annotations

import pytest

from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import (
    EndpointAddress,
    controller_address,
    entity_subject,
    parse_service_address,
    service_address,
)
from deckr.services.messages import (
    service_message,
)

_CONTRACT = ContractPointer(contractId="service-contract-1", generation=1)


def test_service_endpoint_helpers_round_trip() -> None:
    address = service_address("sonos-home")

    assert address == EndpointAddress.model_validate("service:sonos-home")
    assert parse_service_address(address) == "sonos-home"
    assert parse_service_address("controller:main") is None


def test_service_message_body_rejects_sender_authority_fields() -> None:
    with pytest.raises(ValueError, match="senderSessionId"):
        service_message(
            sender=controller_address("controller-main"),
            sender_session_id="controller-session",
            recipient=service_address("sonos-home"),
            subject=entity_subject("service", serviceId="sonos-home"),
            body={
                "serviceNamespace": "dev.deckr.sonos.service",
                "name": "play",
                "intent": "command",
                "exchangePattern": "request_reply",
                "senderSessionId": "not-body-authority",
            },
        )
