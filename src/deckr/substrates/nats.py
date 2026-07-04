from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import anyio
from pydantic import ValidationError

from deckr.contracts.keys import decode_key_token, encode_key_token
from deckr.contracts.lanes import MessageContract, MessageContractRegistry
from deckr.contracts.messages import (
    BroadcastTarget,
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
)
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.substrates.nats_kv import KvBucketPolicy, NatsJsonKvBucket

logger = logging.getLogger(__name__)

_LANE_PREFIX = "deckr.msg"


class NatsSubstrate:
    def __init__(
        self,
        *,
        url: str = "nats://127.0.0.1:4222",
        auth_token: str | None = None,
        lane_contracts: MessageContractRegistry,
        buffer_size: int = 100,
    ) -> None:
        self.url = url
        self.auth_token = auth_token
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._nc = None
        self._js = None
        self._reply_subjects: dict[str, str] = {}

    async def connect(self) -> None:
        try:
            import nats
        except ModuleNotFoundError as exc:
            raise RuntimeError("NATS substrate requires deckr[nats].") from exc

        connect_options = {}
        if self.auth_token is not None:
            connect_options["token"] = self.auth_token
        self._nc = await nats.connect(self.url, **connect_options)
        self._js = self._nc.jetstream()

    async def aclose(self) -> None:
        if self._nc is not None:
            await self._nc.close()
        self._nc = None
        self._js = None

    def contract_for(self, lane: str) -> MessageContract:
        return self._lane_contracts.contract_for(lane)

    async def publish(self, message: DeckrMessage) -> None:
        contract = self.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        await self._publish_payload(
            _subject_for(message),
            message,
            headers=_headers_for(message),
        )

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None:
        reply_subject = self._reply_subjects.pop(request.message_id, None)
        if reply_subject is None:
            await self.publish(message)
            return
        await self._publish_payload(
            reply_subject, message, headers=_headers_for(message)
        )

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        if self._nc is None:
            raise RuntimeError("NATS substrate is not connected")
        contract = self.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=1
        )
        reply_subject = self._nc.new_inbox()

        async def callback(msg) -> None:
            try:
                reply = self._message_from_nats(msg)
                if not message_is_deliverable(
                    reply,
                    endpoint=message.sender,
                    endpoint_session_id=message.sender_session_id,
                    contract=contract,
                ):
                    return
                if not await reply_is_accepted(reply, request=message, accept=accept):
                    return
                send.send_nowait(reply)
            except anyio.WouldBlock:
                return
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                return
            except Exception:
                logger.exception("Dropped invalid NATS Deckr request reply")

        subscription = await self._nc.subscribe(reply_subject, cb=callback)
        try:
            await self._publish_payload(
                _subject_for(message),
                message,
                headers=_headers_for(message),
                reply_subject=reply_subject,
            )
            with anyio.fail_after(timeout):
                return await receive.receive()
        finally:
            await subscription.unsubscribe()
            await send.aclose()
            await receive.aclose()

    @asynccontextmanager
    async def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
        *,
        endpoint_session_id: str,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        if self._nc is None:
            raise RuntimeError("NATS substrate is not connected")
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=self._buffer_size
        )
        contract = self.contract_for(lane)
        subscriptions = []
        subscriber_closed = False

        async def close_subscriber_for_backpressure() -> None:
            nonlocal subscriber_closed
            if subscriber_closed:
                return
            subscriber_closed = True
            logger.warning(
                "NATS Deckr lane subscriber buffer full; unsubscribing "
                "lane=%s endpoint=%s session=%s",
                lane,
                endpoint,
                endpoint_session_id,
            )
            await send.aclose()
            for subscription in tuple(subscriptions):
                try:
                    await subscription.unsubscribe()
                except Exception:
                    logger.debug(
                        "Could not unsubscribe NATS Deckr lane subscriber "
                        "after backpressure lane=%s endpoint=%s session=%s",
                        lane,
                        endpoint,
                        endpoint_session_id,
                        exc_info=True,
                    )
            subscriptions.clear()

        async def callback(msg) -> None:
            try:
                message = self._message_from_nats(msg)
                if not message_is_deliverable(
                    message,
                    endpoint=endpoint,
                    endpoint_session_id=endpoint_session_id,
                    contract=contract,
                ):
                    return
                if msg.reply:
                    self._reply_subjects[message.message_id] = msg.reply
                send.send_nowait(message)
            except anyio.WouldBlock:
                await close_subscriber_for_backpressure()
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                return
            except Exception:
                logger.exception("Dropped invalid NATS Deckr lane message")

        lane_token = encode_key_token(lane)
        endpoint_family_token = encode_key_token(endpoint.family)
        direct_subject = ".".join(
            (
                _LANE_PREFIX,
                lane_token,
                "to",
                endpoint_family_token,
                encode_key_token(endpoint.endpoint_id),
            )
        )
        broadcast_subject = ".".join(
            (
                _LANE_PREFIX,
                lane_token,
                "broadcast",
                "*",
                endpoint_family_token,
            )
        )
        subscriptions = [
            await self._nc.subscribe(direct_subject, cb=callback),
            await self._nc.subscribe(broadcast_subject, cb=callback),
        ]
        try:
            yield receive
        finally:
            for subscription in tuple(subscriptions):
                await subscription.unsubscribe()
            await send.aclose()
            await receive.aclose()

    def kv_bucket(self, policy: KvBucketPolicy) -> NatsJsonKvBucket:
        if self._js is None:
            raise RuntimeError("NATS substrate is not connected")
        return NatsJsonKvBucket(
            js=self._js,
            policy=policy,
            buffer_size=self._buffer_size,
        )

    async def _publish_payload(
        self,
        subject: str,
        message: DeckrMessage,
        *,
        headers: Mapping[str, str],
        reply_subject: str | None = None,
    ) -> None:
        if self._nc is None:
            raise RuntimeError("NATS substrate is not connected")
        await self._nc.publish(
            subject,
            _payload_for(message),
            reply=reply_subject or "",
            headers=dict(headers),
        )

    def _message_from_nats(self, msg) -> DeckrMessage:
        try:
            raw = json.loads(msg.data.decode("utf-8"))
            message = DeckrMessage.from_dict(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValueError("NATS message payload is not a Deckr envelope") from exc
        _validate_subject_hint(msg.subject, message)
        _validate_headers(getattr(msg, "headers", None), message)
        contract = self.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        return message


def _subject_for(message: DeckrMessage) -> str:
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        return ".".join(
            (
                _LANE_PREFIX,
                encode_key_token(message.lane),
                "to",
                encode_key_token(recipient.endpoint.family),
                encode_key_token(recipient.endpoint.endpoint_id),
            )
        )
    return ".".join(
        (
            _LANE_PREFIX,
            encode_key_token(message.lane),
            "broadcast",
            encode_key_token(recipient.scope),
            encode_key_token(recipient.endpoint_family),
        )
    )


def _payload_for(message: DeckrMessage) -> bytes:
    return json.dumps(message.to_dict(), separators=(",", ":")).encode("utf-8")


def _headers_for(message: DeckrMessage) -> Mapping[str, str]:
    headers = {
        "Deckr-Message-Id": message.message_id,
        "Deckr-Message-Type": message.message_type,
        "Deckr-Sender": str(message.sender),
        "Deckr-Sender-Session": message.sender_session_id,
        "Deckr-Recipient": _recipient_header(message),
    }
    if message.recipient_session_id is not None:
        headers["Deckr-Recipient-Session"] = message.recipient_session_id
    if message.contract is not None:
        headers["Deckr-Contract-Id"] = message.contract.contract_id
        headers["Deckr-Contract-Generation"] = str(message.contract.generation)
    if message.in_reply_to is not None:
        headers["Deckr-In-Reply-To"] = message.in_reply_to
    return headers


def _recipient_header(message: DeckrMessage) -> str:
    recipient = message.recipient
    if isinstance(recipient, EndpointTarget):
        return str(recipient.endpoint)
    return f"broadcast:{recipient.scope}:{recipient.endpoint_family}"


def _validate_headers(headers: Mapping[str, str] | None, message: DeckrMessage) -> None:
    if headers is None:
        return
    contract_id = headers.get("Deckr-Contract-Id")
    contract_generation = headers.get("Deckr-Contract-Generation")
    if (contract_id is None) != (contract_generation is None):
        raise ValueError("NATS contract headers must be provided together")
    if message.contract is None and contract_id is not None:
        raise ValueError("NATS contract headers disagree with Deckr envelope")
    expected = _headers_for(message)
    for key, value in expected.items():
        header_value = headers.get(key)
        if header_value is not None and header_value != value:
            raise ValueError(f"NATS header {key!r} disagrees with Deckr envelope")


def _validate_subject_hint(subject: str, message: DeckrMessage) -> None:
    if not subject.startswith(f"{_LANE_PREFIX}."):
        return
    tokens = subject.split(".")
    if len(tokens) != 6:
        raise ValueError("NATS subject has invalid Deckr message shape")
    if tokens[:2] != ["deckr", "msg"]:
        raise ValueError("NATS subject has invalid Deckr message prefix")
    if decode_key_token(tokens[2]) != message.lane:
        raise ValueError("NATS subject disagrees with Deckr envelope lane")
    route = tokens[3]
    recipient = message.recipient
    if route == "to":
        if not isinstance(recipient, EndpointTarget):
            raise ValueError("NATS direct subject disagrees with broadcast envelope")
        if (
            decode_key_token(tokens[4]) != recipient.endpoint.family
            or decode_key_token(tokens[5]) != recipient.endpoint.endpoint_id
        ):
            raise ValueError("NATS subject disagrees with Deckr envelope recipient")
        return
    if route == "broadcast":
        if not isinstance(recipient, BroadcastTarget):
            raise ValueError("NATS broadcast subject disagrees with direct envelope")
        if (
            decode_key_token(tokens[4]) != recipient.scope
            or decode_key_token(tokens[5]) != recipient.endpoint_family
        ):
            raise ValueError("NATS subject disagrees with Deckr envelope broadcast")
        return
    raise ValueError("NATS subject has invalid Deckr message route")


__all__ = [
    "NatsSubstrate",
    "_headers_for",
    "_subject_for",
]
