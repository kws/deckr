"""Body contracts for the core ``services`` lane."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator

from deckr.contracts.messages import (
    SERVICE_MESSAGES_SCHEMA_ID,
    SERVICES_LANE,
    DeckrMessage,
    EndpointAddress,
    EntitySubject,
    MessageTarget,
    endpoint_target,
)
from deckr.contracts.models import DeckrModel, JsonObject, freeze_json, thaw_json

SERVICE_COMMAND = "serviceCommand"
SERVICE_COMMAND_REPLY = "serviceCommandReply"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


class ServiceCommandStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ServiceError(DeckrModel):
    code: str
    message: str
    diagnostics: JsonObject = Field(default_factory=dict)

    @field_validator("code", "message")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service error")

    @field_validator("diagnostics", mode="after")
    @classmethod
    def _freeze_diagnostics(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("diagnostics")
    def _serialize_diagnostics(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class ServiceMessageBody(DeckrModel):
    """Base class for typed ``services`` lane bodies."""

    service_namespace: str = Field(alias="serviceNamespace")
    operation: str

    @field_validator("service_namespace", "operation")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _require_text(value, field_name="service message field")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServiceCommandBody(ServiceMessageBody):
    params: JsonObject = Field(default_factory=dict)

    @field_validator("params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


class ServiceCommandReplyBody(ServiceMessageBody):
    status: ServiceCommandStatus
    result: JsonObject = Field(default_factory=dict)
    error: ServiceError | None = None

    @field_validator("result", mode="after")
    @classmethod
    def _freeze_result(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_json(value)

    @field_serializer("result")
    def _serialize_result(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return thaw_json(value)


SERVICE_BODY_BY_MESSAGE_TYPE: Mapping[str, type[ServiceMessageBody]] = {
    SERVICE_COMMAND: ServiceCommandBody,
    SERVICE_COMMAND_REPLY: ServiceCommandReplyBody,
}


def _target(
    recipient: str | EndpointAddress | MessageTarget,
) -> MessageTarget:
    if not isinstance(recipient, str | EndpointAddress):
        return recipient
    return endpoint_target(recipient)


def service_body_for_type(
    message_type: str,
    body: ServiceMessageBody | Mapping[str, Any],
) -> ServiceMessageBody:
    body_type = SERVICE_BODY_BY_MESSAGE_TYPE.get(message_type)
    if body_type is None:
        raise ValueError(f"Unsupported service message type {message_type!r}")
    if isinstance(body, ServiceMessageBody):
        if not isinstance(body, body_type):
            raise TypeError(
                f"{message_type!r} requires body type {body_type.__name__}, "
                f"got {type(body).__name__}"
            )
        return body
    return body_type.model_validate(thaw_json(body))


def service_body(message: DeckrMessage) -> ServiceMessageBody:
    return service_body_for_type(message.message_type, message.body)


def service_message(
    *,
    sender: str | EndpointAddress,
    sender_session_id: str,
    recipient: str | EndpointAddress | MessageTarget,
    recipient_session_id: str | None = None,
    message_type: Literal["serviceCommand", "serviceCommandReply"],
    body: ServiceMessageBody | Mapping[str, Any],
    subject: EntitySubject,
    in_reply_to: str | None = None,
    causation_id: str | None = None,
) -> DeckrMessage:
    parsed_body = service_body_for_type(message_type, body)
    return DeckrMessage(
        lane=SERVICES_LANE,
        messageType=message_type,
        sender=sender,
        senderSessionId=sender_session_id,
        recipient=_target(recipient),
        recipientSessionId=recipient_session_id,
        subject=subject,
        body=parsed_body.to_dict(),
        inReplyTo=in_reply_to,
        causationId=causation_id,
    )


def service_command_message(
    *,
    sender: str | EndpointAddress,
    sender_session_id: str,
    recipient: str | EndpointAddress | MessageTarget,
    recipient_session_id: str | None = None,
    body: ServiceCommandBody | Mapping[str, Any],
    subject: EntitySubject,
    causation_id: str | None = None,
) -> DeckrMessage:
    return service_message(
        sender=sender,
        sender_session_id=sender_session_id,
        recipient=recipient,
        recipient_session_id=recipient_session_id,
        message_type=SERVICE_COMMAND,
        body=body,
        subject=subject,
        causation_id=causation_id,
    )


def service_command_reply_message(
    *,
    sender: str | EndpointAddress,
    sender_session_id: str,
    recipient: str | EndpointAddress | MessageTarget,
    recipient_session_id: str | None = None,
    body: ServiceCommandReplyBody | Mapping[str, Any],
    subject: EntitySubject,
    in_reply_to: str,
    causation_id: str | None = None,
) -> DeckrMessage:
    return service_message(
        sender=sender,
        sender_session_id=sender_session_id,
        recipient=recipient,
        recipient_session_id=recipient_session_id,
        message_type=SERVICE_COMMAND_REPLY,
        body=body,
        subject=subject,
        in_reply_to=in_reply_to,
        causation_id=causation_id,
    )


def service_message_schema() -> dict[str, Any]:
    """Return the canonical ``services`` lane JSON Schema artifact."""

    definitions: dict[str, Any] = {}
    envelope_ref = _add_schema_model(definitions, DeckrMessage)
    variants: list[dict[str, Any]] = []
    for message_type, body_type in SERVICE_BODY_BY_MESSAGE_TYPE.items():
        body_ref = _add_schema_model(definitions, body_type)
        variants.append(
            {
                "allOf": [
                    envelope_ref,
                    {
                        "type": "object",
                        "required": ["lane", "messageType", "body"],
                        "properties": {
                            "lane": {"const": SERVICES_LANE},
                            "messageType": {"const": message_type},
                            "body": body_ref,
                        },
                    },
                ]
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SERVICE_MESSAGES_SCHEMA_ID,
        "title": "Deckr services Lane Message",
        "x-deckr-schema-version": "1",
        "oneOf": variants,
        "$defs": definitions,
    }


def _add_schema_model(
    definitions: dict[str, Any],
    model: type[DeckrModel],
) -> dict[str, str]:
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    for name, definition in schema.pop("$defs", {}).items():
        _add_schema_definition(definitions, name, definition)
    schema.pop("$schema", None)
    name = model.__name__
    _add_schema_definition(definitions, name, schema)
    return {"$ref": f"#/$defs/{name}"}


def _add_schema_definition(
    definitions: dict[str, Any],
    name: str,
    definition: Mapping[str, Any],
) -> None:
    schema = deepcopy(dict(definition))
    existing = definitions.get(name)
    if existing is not None:
        if existing != schema:
            raise RuntimeError(f"Conflicting schema definition {name!r}")
        return
    definitions[name] = schema
