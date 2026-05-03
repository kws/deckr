from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import deckr.contracts as public_contracts
import deckr.contracts.messages as contract_messages
import deckr.state as public_state
from deckr.actions import endpoints as action_endpoints
from deckr.actions import state as action_state
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
)
from deckr.lanes import validate_message_for_contract


@pytest.mark.parametrize(
    "address",
    [
        "action_provider:",
        " action_provider:python ",
        "action_provider: python",
        "mqtt:bridge",
        "action_provider:python:extra",
    ],
)
def test_endpoint_addresses_reject_malformed_or_unknown_families(
    address: str,
) -> None:
    with pytest.raises(ValidationError):
        EndpointAddress.model_validate(address)


def test_action_provider_endpoint_ids_reject_reserved_builtin_provider() -> None:
    with pytest.raises(ValidationError, match="reserved provider identity"):
        EndpointAddress.model_validate("action_provider:deckr.controller.builtin")


def test_action_provider_helpers_live_on_action_modules() -> None:
    assert action_endpoints.BUILTIN_ACTION_PROVIDER_ID == "deckr.controller.builtin"
    assert (
        action_endpoints.BUILTIN_ACTION_PROVIDER_ID
        in action_endpoints.RESERVED_BUILTIN_PROVIDER_IDS
    )
    assert (
        action_endpoints.require_provider_instance_id(
            "python",
            field_name="providerInstanceId",
        )
        == "python"
    )
    with pytest.raises(ValueError, match="reserved provider identity"):
        action_endpoints.require_provider_instance_id(
            "deckr.controller.builtin",
            field_name="providerInstanceId",
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

    catalog_key = action_state.action_provider_catalog_key("python")
    assert catalog_key == "catalog.actions.providers.python"
    assert action_state.parse_action_provider_catalog_key(catalog_key) == "python"


@pytest.mark.parametrize(
    ("module", "helper_names"),
    [
        (
            public_contracts,
            {
                "BUILTIN_ACTION_PROVIDER_ID",
                "RESERVED_BUILTIN_PROVIDER_IDS",
                "action_provider_address",
                "action_providers_broadcast",
                "parse_action_provider_address",
                "require_provider_instance_id",
            },
        ),
        (
            contract_messages,
            {
                "BUILTIN_ACTION_PROVIDER_ID",
                "RESERVED_BUILTIN_PROVIDER_IDS",
                "action_provider_address",
                "action_providers_broadcast",
                "parse_action_provider_address",
                "require_provider_instance_id",
            },
        ),
        (
            public_state,
            {
                "action_provider_catalog_key",
                "parse_action_provider_catalog_key",
            },
        ),
    ],
)
def test_action_provider_helpers_are_not_exported_from_generic_modules(
    module: object,
    helper_names: set[str],
) -> None:
    for helper_name in helper_names:
        assert not hasattr(module, helper_name)


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
            lane=ACTIONS_LANE,
            messageType="actionExtension",
            sender=action_endpoints.action_provider_address("python"),
            senderSessionId="session-provider",
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
