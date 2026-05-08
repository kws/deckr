from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from deckr.contracts.keys import encode_key_token
from deckr.contracts.messages import DeckrMessage, EndpointTarget
from deckr.contracts.models import thaw_json

LANE_SUBJECT_PREFIX = "deckr.lane"
LANE_SUBJECT_TEMPLATE = f"{LANE_SUBJECT_PREFIX}.{{lane}}.{{senderFamily}}.{{senderEndpointToken}}"
LANE_SUBSCRIBE_TEMPLATE = f"{LANE_SUBJECT_PREFIX}.{{lane}}.>"
NATS_BINDING_SCHEMA_ID = "dev.deckr.binding.nats.v1"
NATS_BINDING_PATH = "bindings/nats.v1.json"
DECKR_NATS_HEADERS = (
    "Deckr-Message-Id",
    "Deckr-Message-Type",
    "Deckr-Sender",
    "Deckr-Sender-Session",
    "Deckr-Recipient",
    "Deckr-Recipient-Session",
    "Deckr-In-Reply-To",
)
REQUIRED_DECKR_NATS_HEADERS = (
    "Deckr-Message-Id",
    "Deckr-Message-Type",
    "Deckr-Sender",
    "Deckr-Sender-Session",
    "Deckr-Recipient",
)


def lane_message_subject(message: DeckrMessage) -> str:
    return ".".join(
        (
            LANE_SUBJECT_PREFIX,
            encode_key_token(message.lane),
            encode_key_token(message.sender.family),
            encode_key_token(message.sender.endpoint_id),
        )
    )


def lane_subscribe_subject(lane: str) -> str:
    return f"{LANE_SUBJECT_PREFIX}.{encode_key_token(lane)}.>"


def lane_message_payload(message: DeckrMessage) -> bytes:
    return json.dumps(message.to_dict(), separators=(",", ":")).encode("utf-8")


def lane_message_headers(message: DeckrMessage) -> Mapping[str, str]:
    headers = {
        "Deckr-Message-Id": message.message_id,
        "Deckr-Message-Type": message.message_type,
        "Deckr-Sender": str(message.sender),
        "Deckr-Sender-Session": message.sender_session_id,
        "Deckr-Recipient": lane_recipient_header(message),
    }
    if message.recipient_session_id is not None:
        headers["Deckr-Recipient-Session"] = message.recipient_session_id
    if message.in_reply_to is not None:
        headers["Deckr-In-Reply-To"] = message.in_reply_to
    return headers


def lane_recipient_header(message: DeckrMessage) -> str:
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        return str(recipient.endpoint)
    return f"broadcast:{recipient.scope}:{recipient.endpoint_family}"


def validate_lane_headers(
    headers: Mapping[str, str] | None,
    message: DeckrMessage,
) -> None:
    if headers is None:
        return
    expected = lane_message_headers(message)
    for key, value in expected.items():
        header_value = headers.get(key)
        if header_value is not None and header_value != value:
            raise ValueError(f"NATS header {key!r} disagrees with Deckr envelope")


def validate_lane_subject_hint(subject: str, message: DeckrMessage) -> None:
    if not subject.startswith(f"{LANE_SUBJECT_PREFIX}."):
        return
    tokens = subject.split(".")
    expected = [
        "deckr",
        "lane",
        encode_key_token(message.lane),
        encode_key_token(message.sender.family),
        encode_key_token(message.sender.endpoint_id),
    ]
    if tokens[:5] != expected:
        raise ValueError("NATS subject disagrees with Deckr envelope sender")


def state_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), separators=(",", ":")).encode("utf-8")


__all__ = [
    "DECKR_NATS_HEADERS",
    "LANE_SUBJECT_PREFIX",
    "LANE_SUBJECT_TEMPLATE",
    "LANE_SUBSCRIBE_TEMPLATE",
    "NATS_BINDING_PATH",
    "NATS_BINDING_SCHEMA_ID",
    "REQUIRED_DECKR_NATS_HEADERS",
    "lane_message_headers",
    "lane_message_payload",
    "lane_message_subject",
    "lane_subscribe_subject",
    "lane_recipient_header",
    "state_payload",
    "validate_lane_headers",
    "validate_lane_subject_hint",
]
