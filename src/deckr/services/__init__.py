"""Shared service endpoint and message contracts."""

from deckr.services.messages import (
    SERVICE_COMMAND,
    SERVICE_COMMAND_REPLY,
    ServiceCommandBody,
    ServiceCommandReplyBody,
    ServiceCommandStatus,
    ServiceError,
    ServiceMessageBody,
    service_body,
    service_body_for_type,
    service_command_message,
    service_command_reply_message,
    service_message,
    service_message_schema,
)

__all__ = [
    "SERVICE_COMMAND",
    "SERVICE_COMMAND_REPLY",
    "ServiceCommandBody",
    "ServiceCommandReplyBody",
    "ServiceCommandStatus",
    "ServiceError",
    "ServiceMessageBody",
    "service_body",
    "service_body_for_type",
    "service_command_message",
    "service_command_reply_message",
    "service_message",
    "service_message_schema",
]
