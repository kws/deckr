from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deckr.contracts.lanes import CORE_LANE_CONTRACTS
from deckr.contracts.messages import (
    PLUGIN_MESSAGES_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    broadcast_target,
    controller_address,
    endpoint_target,
    entity_subject,
    host_address,
)
from deckr.lanes import validate_message_for_contract


@pytest.mark.parametrize(
    "address",
    [
        "host:",
        " host:python ",
        "host: python",
        "mqtt:bridge",
        "host:python:extra",
    ],
)
def test_endpoint_addresses_reject_malformed_or_unknown_families(
    address: str,
) -> None:
    with pytest.raises(ValidationError):
        EndpointAddress.model_validate(address)


def test_broadcast_targets_validate_scope_family_and_hop_limit() -> None:
    with pytest.raises(ValidationError):
        broadcast_target(scope="", endpoint_family="host")

    with pytest.raises(ValidationError):
        broadcast_target(scope="plugin_hosts", endpoint_family="mqtt")

    with pytest.raises(ValidationError):
        BroadcastTarget(
            scope="plugin_hosts",
            endpointFamily="host",
            hopLimit=-1,
        )


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "", "identifiers": {}},
        {"kind": " context", "identifiers": {}},
        {"kind": "context", "identifiers": {"": "binding-1"}},
        {"kind": "context", "identifiers": {"bindingId": ""}},
    ],
)
def test_entity_subject_rejects_empty_identity_fields(
    subject: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EntitySubject.model_validate(subject)


def test_deckr_message_body_rejects_non_json_values() -> None:
    with pytest.raises(ValidationError):
        DeckrMessage(
            lane=PLUGIN_MESSAGES_LANE,
            messageType="pluginExtension",
            sender=host_address("python"),
            senderSessionId="session-host",
            recipient=endpoint_target(controller_address("main")),
            subject=entity_subject("extension", contextId="ctx"),
            body={
                "extensionType": "test.extension",
                "extensionSchemaId": "test.extension.v1",
                "data": {"createdAt": datetime.now(UTC)},
            },
        )


def test_recipient_session_requires_direct_endpoint_recipient() -> None:
    with pytest.raises(ValidationError, match="recipientSessionId"):
        DeckrMessage(
            lane=PLUGIN_MESSAGES_LANE,
            messageType="pluginExtension",
            sender=host_address("python"),
            senderSessionId="session-host",
            recipient=broadcast_target(scope="controllers", endpoint_family="controller"),
            recipientSessionId="session-controller",
            subject=entity_subject("extension", contextId="ctx"),
            body={
                "extensionType": "test.extension",
                "extensionSchemaId": "test.extension.v1",
                "data": {},
            },
        )


def test_core_lane_validation_checks_message_type_body_pair() -> None:
    message = DeckrMessage(
        lane=PLUGIN_MESSAGES_LANE,
        messageType="settingsRequest",
        sender=host_address("python"),
        senderSessionId="session-host",
        recipient=endpoint_target(controller_address("main")),
        subject=entity_subject("settings", contextId="ctx"),
        body={},
    )

    with pytest.raises(ValidationError, match="target"):
        validate_message_for_contract(
            message,
            CORE_LANE_CONTRACTS[PLUGIN_MESSAGES_LANE],
        )
