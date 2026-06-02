from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import anyio
from pydantic import ValidationError

from deckr.contracts.keys import encode_key_token
from deckr.contracts.lanes import LaneContractRegistry
from deckr.contracts.messages import (
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

_LANE_PREFIX = "deckr.lane"


class NatsSubstrate:
    def __init__(
        self,
        *,
        url: str = "nats://127.0.0.1:4222",
        auth_token: str | None = None,
        lane_contracts: LaneContractRegistry,
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

    async def publish(self, message: DeckrMessage) -> None:
        contract = self._lane_contracts.contract_for(message.lane)
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
        contract = self._lane_contracts.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        response = await self._nc.request(
            _subject_for(message),
            _payload_for(message),
            timeout=timeout,
            headers=_headers_for(message),
        )
        reply = self._message_from_nats(response)
        if not message_is_deliverable(
            reply,
            endpoint=message.sender,
            endpoint_session_id=message.sender_session_id,
            contract=contract,
        ):
            raise TimeoutError("NATS request returned no deliverable Deckr reply")
        if not await reply_is_accepted(reply, request=message, accept=accept):
            raise TimeoutError("NATS request returned no accepted Deckr reply")
        return reply

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
        contract = self._lane_contracts.contract_for(lane)
        subscription = None
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
            if subscription is not None:
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

        subscription = await self._nc.subscribe(
            f"{_LANE_PREFIX}.{encode_key_token(lane)}.>",
            cb=callback,
        )
        try:
            yield receive
        finally:
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
    ) -> None:
        if self._nc is None:
            raise RuntimeError("NATS substrate is not connected")
        await self._nc.publish(subject, _payload_for(message), headers=dict(headers))

    def _message_from_nats(self, msg) -> DeckrMessage:
        try:
            raw = json.loads(msg.data.decode("utf-8"))
            message = DeckrMessage.from_dict(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValueError("NATS message payload is not a Deckr envelope") from exc
        _validate_subject_hint(msg.subject, message)
        _validate_headers(getattr(msg, "headers", None), message)
        contract = self._lane_contracts.contract_for(message.lane)
        validate_message_for_contract(message, contract)
        return message


def _subject_for(message: DeckrMessage) -> str:
    return ".".join(
        (
            _LANE_PREFIX,
            encode_key_token(message.lane),
            encode_key_token(message.sender.family),
            encode_key_token(message.sender.endpoint_id),
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
    expected = _headers_for(message)
    for key, value in expected.items():
        header_value = headers.get(key)
        if header_value is not None and header_value != value:
            raise ValueError(f"NATS header {key!r} disagrees with Deckr envelope")


def _validate_subject_hint(subject: str, message: DeckrMessage) -> None:
    if not subject.startswith(f"{_LANE_PREFIX}."):
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


__all__ = [
    "NatsSubstrate",
    "_headers_for",
    "_subject_for",
]
