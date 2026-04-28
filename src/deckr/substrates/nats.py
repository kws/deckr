from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import anyio
from pydantic import ValidationError

from deckr.contracts.lanes import LaneContractRegistry
from deckr.contracts.messages import (
    DeckrMessage,
    EndpointAddress,
    EndpointTarget,
)
from deckr.contracts.models import thaw_json
from deckr.lanes import (
    ReplyPredicate,
    message_is_deliverable,
    reply_is_accepted,
    validate_message_for_contract,
)
from deckr.state import (
    StateChange,
    StateConflict,
    StateEntry,
    StateStore,
    encode_key_token,
    state_expires_at,
    state_value,
)

logger = logging.getLogger(__name__)

_LANE_PREFIX = "deckr.lane"


class NatsSubstrate:
    def __init__(
        self,
        *,
        url: str = "nats://127.0.0.1:4222",
        lane_contracts: LaneContractRegistry,
        buffer_size: int = 100,
        state_sweep_interval: float = 0.25,
    ) -> None:
        self.url = url
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._state_sweep_interval = state_sweep_interval
        self._nc = None
        self._js = None
        self._reply_subjects: dict[str, str] = {}
        self._states: dict[str, NatsStateStore] = {}

    async def connect(self) -> None:
        try:
            import nats
        except ModuleNotFoundError as exc:
            raise RuntimeError("NATS substrate requires deckr[nats].") from exc

        self._nc = await nats.connect(self.url)
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
        await self._publish_payload(reply_subject, message, headers=_headers_for(message))

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
        if not await reply_is_accepted(reply, request=message, accept=accept):
            raise TimeoutError("NATS request returned no accepted Deckr reply")
        return reply

    @asynccontextmanager
    async def subscribe(
        self,
        lane: str,
        endpoint: EndpointAddress,
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        if self._nc is None:
            raise RuntimeError("NATS substrate is not connected")
        send, receive = anyio.create_memory_object_stream[DeckrMessage](
            max_buffer_size=self._buffer_size
        )
        contract = self._lane_contracts.contract_for(lane)

        async def callback(msg) -> None:
            try:
                message = self._message_from_nats(msg)
                if not message_is_deliverable(
                    message,
                    endpoint=endpoint,
                    contract=contract,
                ):
                    return
                if msg.reply:
                    self._reply_subjects[message.message_id] = msg.reply
                await send.send(message)
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

    def state(self, name: str) -> StateStore:
        if self._js is None:
            raise RuntimeError("NATS substrate is not connected")
        store = self._states.get(name)
        if store is None:
            store = NatsStateStore(
                name=name,
                js=self._js,
                buffer_size=self._buffer_size,
                sweep_interval=self._state_sweep_interval,
            )
            self._states[name] = store
        return store

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
        return message


class NatsStateStore:
    def __init__(
        self,
        *,
        name: str,
        js,
        buffer_size: int,
        sweep_interval: float,
    ) -> None:
        self.name = name
        self._js = js
        self._buffer_size = buffer_size
        self._sweep_interval = sweep_interval
        self._kv = None
        self._cache: dict[str, StateEntry] = {}

    async def get(self, key: str) -> StateEntry | None:
        kv = await self._ensure_kv()
        try:
            entry = await kv.get(key)
        except Exception:
            return None
        return _state_entry_from_kv(entry)

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]:
        kv = await self._ensure_kv()
        filter_pattern = _kv_watch_pattern(prefix)
        try:
            keys = await kv.keys(filters=[filter_pattern])
        except TypeError:
            keys = await kv.keys()
        except Exception:
            keys = ()
        entries: list[StateEntry] = []
        for key in keys:
            if not str(key).startswith(prefix):
                continue
            entry = await self.get(str(key))
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> StateEntry:
        del ttl
        kv = await self._ensure_kv()
        normalized = state_value(value)
        revision = await kv.put(key, _state_payload(normalized))
        return StateEntry(key=key, value=normalized, revision=int(revision))

    async def create(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> StateEntry:
        del ttl
        kv = await self._ensure_kv()
        normalized = state_value(value)
        try:
            revision = await kv.create(key, _state_payload(normalized))
        except Exception as exc:
            raise StateConflict(f"State key {key!r} already exists") from exc
        return StateEntry(key=key, value=normalized, revision=int(revision))

    async def update(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> StateEntry:
        del ttl
        kv = await self._ensure_kv()
        normalized = state_value(value)
        try:
            new_revision = await kv.update(
                key,
                _state_payload(normalized),
                last=revision,
            )
        except Exception as exc:
            raise StateConflict(f"State key {key!r} revision changed") from exc
        return StateEntry(key=key, value=normalized, revision=int(new_revision))

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        kv = await self._ensure_kv()
        try:
            if revision is None:
                await kv.delete(key)
            else:
                await kv.delete(key, last=revision)
        except Exception as exc:
            raise StateConflict(f"State key {key!r} revision changed") from exc

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[StateChange]]:
        kv = await self._ensure_kv()
        send, receive = anyio.create_memory_object_stream[StateChange](
            max_buffer_size=self._buffer_size
        )
        watcher = await kv.watch(_kv_watch_pattern(prefix))

        async def pump() -> None:
            async for update in watcher:
                if update is None:
                    continue
                change = _state_change_from_kv(update)
                if change is None or not change.key.startswith(prefix):
                    continue
                if change.entry is None:
                    self._cache.pop(change.key, None)
                else:
                    self._cache[change.key] = change.entry
                await send.send(change)

        async def sweep() -> None:
            while True:
                await anyio.sleep(self._sweep_interval)
                now = datetime.now(UTC)
                for key, entry in tuple(self._cache.items()):
                    if not key.startswith(prefix):
                        continue
                    expires_at = state_expires_at(entry.value)
                    if expires_at is None or expires_at > now:
                        continue
                    self._cache.pop(key, None)
                    await send.send(StateChange("expire", key, None))

        async with anyio.create_task_group() as tg:
            tg.start_soon(pump)
            tg.start_soon(sweep)
            try:
                yield receive
            finally:
                tg.cancel_scope.cancel()
                await watcher.stop()
                await send.aclose()
                await receive.aclose()

    async def _ensure_kv(self):
        if self._kv is not None:
            return self._kv
        try:
            self._kv = await self._js.key_value(self.name)
        except Exception:
            try:
                self._kv = await self._js.create_key_value(bucket=self.name)
            except TypeError:
                from nats.js.api import KeyValueConfig

                self._kv = await self._js.create_key_value(
                    config=KeyValueConfig(bucket=self.name)
                )
            except Exception:
                self._kv = await self._js.key_value(self.name)
        return self._kv


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
        "Deckr-Recipient": _recipient_header(message),
    }
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


def _state_payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(thaw_json(value), separators=(",", ":")).encode("utf-8")


def _state_entry_from_kv(entry) -> StateEntry:
    value = json.loads(entry.value.decode("utf-8")) if entry.value else {}
    return StateEntry(
        key=str(entry.key),
        value=state_value(value),
        revision=int(entry.revision),
    )


def _state_change_from_kv(entry) -> StateChange | None:
    operation = str(getattr(entry, "operation", "PUT")).upper()
    key = str(entry.key)
    if "DEL" in operation or "PURGE" in operation:
        return StateChange("delete", key, None)
    if entry.value is None:
        return None
    state_entry = _state_entry_from_kv(entry)
    return StateChange("put", key, state_entry)


def _kv_watch_pattern(prefix: str) -> str:
    if not prefix:
        return ">"
    if prefix.endswith("."):
        return f"{prefix}>"
    return prefix


__all__ = [
    "NatsSubstrate",
    "_headers_for",
    "_subject_for",
]
