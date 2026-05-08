from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable

from deckr.contracts.lanes import LaneContract
from deckr.contracts.messages import (
    ACTIONS_LANE,
    HARDWARE_MESSAGES_LANE,
    SERVICES_LANE,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
    message_is_expired,
    message_targets_endpoint,
)

ReplyPredicate = Callable[[DeckrMessage], bool | Awaitable[bool]]


def validate_message_for_contract(
    message: DeckrMessage,
    contract: LaneContract,
) -> None:
    if message.lane != contract.lane:
        raise ValueError(
            f"Message lane {message.lane!r} does not match contract {contract.lane!r}"
        )
    if contract.message_types and message.message_type not in contract.message_types:
        raise ValueError(
            f"Message type {message.message_type!r} is not supported on "
            f"lane {message.lane!r}"
        )
    _validate_core_lane_body(message)
    if (
        contract.allowed_sender_families is not None
        and message.sender.family not in contract.allowed_sender_families
    ):
        raise ValueError(
            f"Sender family {message.sender.family!r} is not allowed on "
            f"lane {message.lane!r}"
        )
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        _validate_recipient_family(
            recipient.endpoint.family,
            contract=contract,
            lane=message.lane,
        )
        return
    expected_family = contract.broadcast_targets.get(recipient.scope)
    if expected_family != recipient.endpoint_family:
        raise ValueError(
            f"Broadcast target {recipient.scope!r} for family "
            f"{recipient.endpoint_family!r} is not allowed on lane {message.lane!r}"
        )
    _validate_recipient_family(
        recipient.endpoint_family,
        contract=contract,
        lane=message.lane,
    )


def message_is_deliverable(
    message: DeckrMessage,
    *,
    endpoint: EndpointAddress,
    endpoint_session_id: str,
    contract: LaneContract,
) -> bool:
    if message_is_expired(message):
        return False
    validate_message_for_contract(message, contract)
    if (
        message.recipient_session_id is not None
        and message.recipient_session_id != endpoint_session_id
    ):
        return False
    return message_targets_endpoint(message, endpoint)


async def reply_is_accepted(
    reply: DeckrMessage,
    *,
    request: DeckrMessage,
    accept: ReplyPredicate | None,
) -> bool:
    if reply.message_id == request.message_id:
        return False
    if reply.in_reply_to != request.message_id:
        return False
    if (
        reply.recipient_session_id is not None
        and reply.recipient_session_id != request.sender_session_id
    ):
        return False
    if accept is None:
        return True
    accepted = accept(reply)
    if isawaitable(accepted):
        accepted = await accepted
    return bool(accepted)


def _validate_recipient_family(
    family: str,
    *,
    contract: LaneContract,
    lane: str,
) -> None:
    if contract.allowed_recipient_families is None:
        return
    if family not in contract.allowed_recipient_families:
        raise ValueError(f"Recipient family {family!r} is not allowed on lane {lane!r}")


def _validate_core_lane_body(message: DeckrMessage) -> None:
    if message.lane == ACTIONS_LANE:
        from deckr.actions.messages import action_body

        action_body(message)
        return
    if message.lane == HARDWARE_MESSAGES_LANE:
        from deckr.hardware.messages import hardware_body_from_message

        hardware_body_from_message(message)
        return
    if message.lane == SERVICES_LANE:
        from deckr.services.messages import service_body

        service_body(message)


__all__ = [
    "ReplyPredicate",
    "message_is_deliverable",
    "reply_is_accepted",
    "validate_message_for_contract",
]
