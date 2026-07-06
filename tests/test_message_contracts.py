from __future__ import annotations

import pytest
from pydantic import ValidationError

from deckr.actions import endpoints as action_endpoints
from deckr.contracts.lanes import CORE_LANE_CONTRACTS
from deckr.contracts.messages import (
    ACTIONS_LANE,
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    broadcast_target,
    controller_address,
    endpoint_target,
    entity_subject,
    service_address,
)
from deckr.lanes import message_is_deliverable, validate_message_for_contract

_CONTRACT = {"contractId": "contract-1", "generation": 1}


@pytest.mark.parametrize(
    "address",
    [
        " action_provider:python ",
        "action_provider: python",
        "action_provider:python:extra",
    ],
)
def test_endpoint_addresses_reject_malformed_or_unknown_families(
    address: str,
) -> None:
    with pytest.raises(ValidationError):
        EndpointAddress.model_validate(address)


def test_action_provider_helpers_live_on_action_modules() -> None:
    assert action_endpoints.BUILTIN_ACTION_PROVIDER_ID == "dev.deckr.controller.builtin"
    assert (
        action_endpoints.BUILTIN_ACTION_PROVIDER_ID
        in action_endpoints.RESERVED_BUILTIN_PROVIDER_IDS
    )
    assert (
        action_endpoints.require_provider_instance_id(
            "dev.deckr.controller.builtin",
            field_name="providerInstanceId",
        )
        == "dev.deckr.controller.builtin"
    )
    with pytest.raises(ValueError, match="reserved provider identity"):
        action_endpoints.action_provider_address(
            "dev.deckr.controller.builtin",
        )

    address = action_endpoints.action_provider_address("python")
    assert address == EndpointAddress.model_validate("action_provider:python")
    assert action_endpoints.parse_action_provider_address(address) == "python"
    assert action_endpoints.parse_action_provider_address("controller:main") is None

    broadcast = action_endpoints.action_providers_broadcast(
        domain="demo",
        hop_limit=1,
    )
    assert broadcast.scope == "action_providers"
    assert broadcast.endpoint_family == "action_provider"


def test_broadcast_targets_validate_scope_family_and_hop_limit() -> None:
    with pytest.raises(ValidationError):
        broadcast_target(scope="", endpoint_family="action_provider")

    with pytest.raises(ValidationError):
        broadcast_target(scope="action_providers", endpoint_family="mqtt")

    with pytest.raises(ValidationError):
        BroadcastTarget(
            scope="action_providers",
            endpointFamily="mqtt",
            hopLimit=-1,
        )


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param(
            {"kind": "context", "identifiers": {"": "binding-1"}},
            id="subject2",
        ),
        pytest.param(
            {"kind": "context", "identifiers": {"bindingId": ""}},
            id="subject3",
        ),
    ],
)
def test_entity_subject_rejects_empty_identity_fields(
    subject: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EntitySubject.model_validate(subject)


def test_recipient_session_requires_direct_endpoint_recipient() -> None:
    with pytest.raises(ValidationError, match="recipientSessionId"):
        DeckrMessage(
            lane=ACTIONS_LANE,
            messageType="actionExtension",
            sender=action_endpoints.action_provider_address("python"),
            senderSessionId="session-provider",
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
        lane=ACTIONS_LANE,
        messageType="settingsRequest",
        sender=action_endpoints.action_provider_address("python"),
        senderSessionId="session-provider",
        recipient=endpoint_target(controller_address("main")),
        subject=entity_subject("settings", contextId="ctx"),
        body={},
    )

    with pytest.raises(ValidationError, match="target"):
        validate_message_for_contract(
            message,
            CORE_LANE_CONTRACTS[ACTIONS_LANE],
        )


def _valid_action_extension(**overrides) -> DeckrMessage:
    fields = {
        "lane": ACTIONS_LANE,
        "messageType": "actionExtension",
        "sender": action_endpoints.action_provider_address("python"),
        "senderSessionId": "session-provider",
        "recipient": endpoint_target(controller_address("main")),
        "subject": entity_subject("extension", contextId="ctx"),
        "contract": _CONTRACT,
        "body": {
            "extensionType": "test.extension",
            "extensionSchemaId": "test.extension.v1",
            "data": {},
        },
    }
    fields.update(overrides)
    return DeckrMessage(**fields)


def test_contract_validation_rejects_wrong_lane() -> None:
    message = _valid_action_extension(lane="other")

    with pytest.raises(ValueError, match="does not match contract"):
        validate_message_for_contract(message, CORE_LANE_CONTRACTS[ACTIONS_LANE])


def test_contract_validation_rejects_bad_message_type() -> None:
    message = _valid_action_extension(messageType="unknown")

    with pytest.raises(ValueError, match="not supported"):
        validate_message_for_contract(message, CORE_LANE_CONTRACTS[ACTIONS_LANE])


def test_contract_validation_rejects_disallowed_recipient_family() -> None:
    message = _valid_action_extension(recipient=endpoint_target(service_address("media")))

    with pytest.raises(ValueError, match="Recipient family"):
        validate_message_for_contract(message, CORE_LANE_CONTRACTS[ACTIONS_LANE])


def test_contract_validation_rejects_bad_broadcast_target() -> None:
    message = _valid_action_extension(
        recipient=broadcast_target(
            scope="hardware_managers",
            endpoint_family="hardware_manager",
        )
    )

    with pytest.raises(ValueError, match="Broadcast target"):
        validate_message_for_contract(message, CORE_LANE_CONTRACTS[ACTIONS_LANE])


def test_expired_message_is_not_deliverable() -> None:
    message = _valid_action_extension(
        recipient=endpoint_target(controller_address("main")),
        ttlMs=0,
    )

    assert not message_is_deliverable(
        message,
        endpoint=controller_address("main"),
        endpoint_session_id="session-controller",
        contract=CORE_LANE_CONTRACTS[ACTIONS_LANE],
    )
