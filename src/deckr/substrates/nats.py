from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
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
    StateUnavailable,
    encode_key_token,
    state_value,
)

logger = logging.getLogger(__name__)

_LANE_PREFIX = "deckr.lane"
_STATE_LEASE_TTL_SECONDS = 15.0
_KV_OPERATION_HEADER = "KV-Operation"
_KV_DELETE_OPERATION = "DEL"
_KV_PURGE_OPERATION = "PURGE"
_NATS_MARKER_REASON_HEADER = "Nats-Marker-Reason"
_NATS_MARKER_MAX_AGE = "MaxAge"


class NatsSubstrate:
    def __init__(
        self,
        *,
        url: str = "nats://127.0.0.1:4222",
        lane_contracts: LaneContractRegistry,
        buffer_size: int = 100,
        state_lease_ttl_seconds: float = _STATE_LEASE_TTL_SECONDS,
    ) -> None:
        self.url = url
        self._lane_contracts = lane_contracts
        self._buffer_size = buffer_size
        self._state_lease_ttl_seconds = state_lease_ttl_seconds
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
                lease_ttl_seconds=self._state_lease_ttl_seconds,
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
        lease_ttl_seconds: float = _STATE_LEASE_TTL_SECONDS,
    ) -> None:
        self.name = name
        self._js = js
        self._buffer_size = buffer_size
        self._lease_ttl_seconds = float(lease_ttl_seconds)
        self._kv = None

    async def get(self, key: str) -> StateEntry | None:
        return await self._get_entry(key)

    async def items(self, prefix: str = "") -> tuple[StateEntry, ...]:
        kv = await self._available_kv()
        filter_pattern = _kv_watch_pattern(prefix)
        try:
            keys = await kv.keys(filters=[filter_pattern])
        except TypeError:
            try:
                keys = await kv.keys()
            except Exception as exc:
                if _is_key_missing(exc):
                    keys = ()
                else:
                    raise StateUnavailable(
                        f"Could not list state keys with prefix {prefix!r}"
                    ) from exc
        except Exception as exc:
            if not _is_key_missing(exc):
                raise StateUnavailable(
                    f"Could not list state keys with prefix {prefix!r}"
                ) from exc
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
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = state_value(value)
        try:
            revision = await kv.put(key, _state_payload(normalized))
        except Exception as exc:
            raise StateUnavailable(f"Could not put state key {key!r}") from exc
        return StateEntry(key=key, value=normalized, revision=int(revision))

    async def create(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> StateEntry:
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = state_value(value)
        try:
            revision = await kv.create(key, _state_payload(normalized))
        except Exception as exc:
            if _is_revision_conflict(exc):
                raise StateConflict(f"State key {key!r} already exists") from exc
            raise StateUnavailable(f"Could not create state key {key!r}") from exc
        return StateEntry(key=key, value=normalized, revision=int(revision))

    async def update(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        revision: int,
        ttl: float | None = None,
    ) -> StateEntry:
        self._validate_ttl(ttl)
        kv = await self._available_kv()
        normalized = state_value(value)
        current = await self._get_entry(key)
        if current is None or current.revision != revision:
            raise StateConflict(f"State key {key!r} revision changed")
        try:
            new_revision = await kv.update(
                key,
                _state_payload(normalized),
                last=revision,
            )
        except Exception as exc:
            if _is_revision_conflict(exc):
                raise StateConflict(f"State key {key!r} revision changed") from exc
            raise StateUnavailable(f"Could not update state key {key!r}") from exc
        return StateEntry(key=key, value=normalized, revision=int(new_revision))

    async def delete(self, key: str, *, revision: int | None = None) -> None:
        kv = await self._available_kv()
        current = await self._get_entry(key)
        if current is None:
            return
        if revision is not None and current.revision != revision:
            raise StateConflict(f"State key {key!r} revision changed")
        try:
            if revision is None:
                await kv.delete(key)
            else:
                await kv.delete(key, last=revision)
        except Exception as exc:
            if _is_key_missing(exc):
                return
            if _is_revision_conflict(exc):
                raise StateConflict(f"State key {key!r} revision changed") from exc
            raise StateUnavailable(f"Could not delete state key {key!r}") from exc

    @asynccontextmanager
    async def watch(
        self,
        prefix: str = "",
    ) -> AsyncIterator[anyio.abc.ObjectReceiveStream[StateChange]]:
        await self._available_kv()
        send, receive = anyio.create_memory_object_stream[StateChange](
            max_buffer_size=self._buffer_size
        )
        subject = f"{_kv_subject_prefix(self.name)}{_kv_watch_pattern(prefix)}"

        async def callback(msg) -> None:
            try:
                change = _state_change_from_nats_msg(
                    msg,
                    subject_prefix=_kv_subject_prefix(self.name),
                )
                if change is None or not change.key.startswith(prefix):
                    return
                await send.send(change)
            except Exception:
                logger.exception("Dropped invalid NATS Deckr state update")

        from nats.js import api

        try:
            subscription = await self._js.subscribe(
                subject,
                cb=callback,
                ordered_consumer=True,
                deliver_policy=api.DeliverPolicy.LAST_PER_SUBJECT,
                inactive_threshold=5 * 60,
            )
        except Exception as exc:
            raise StateUnavailable(
                f"Could not watch state prefix {prefix!r}"
            ) from exc
        try:
            yield receive
        finally:
            await subscription.unsubscribe()
            await send.aclose()
            await receive.aclose()

    async def _ensure_kv(self):
        if self._kv is not None:
            return self._kv
        try:
            self._kv = await self._js.key_value(self.name)
        except Exception:
            try:
                self._kv = await self._create_kv()
            except TypeError:
                self._kv = await self._create_kv_with_params()
            except Exception:
                self._kv = await self._js.key_value(self.name)
        await self._ensure_kv_stream_config(self._kv)
        return self._kv

    async def _available_kv(self):
        try:
            return await self._ensure_kv()
        except Exception as exc:
            raise StateUnavailable(
                f"NATS current-state bucket {self.name!r} is unavailable"
            ) from exc

    async def _create_kv(self):
        from nats.js.api import KeyValueConfig

        return await self._js.create_key_value(
            config=KeyValueConfig(
                bucket=self.name,
                history=1,
                ttl=self._lease_ttl_seconds,
            )
        )

    async def _create_kv_with_params(self):
        return await self._js.create_key_value(
            bucket=self.name,
            history=1,
            ttl=self._lease_ttl_seconds,
        )

    async def _ensure_kv_stream_config(self, kv) -> None:
        stream_name = getattr(kv, "_stream", f"KV_{self.name}")
        try:
            info = await self._js.stream_info(stream_name)
        except Exception as exc:
            raise RuntimeError(
                f"Could not inspect NATS KV bucket {self.name!r}; delete and "
                "recreate the development bucket if it was created by an older "
                "Deckr build."
            ) from exc
        config = info.config
        needs_update = (
            getattr(config, "max_age", None) != self._lease_ttl_seconds
            or getattr(config, "max_msgs_per_subject", None) != 1
            or getattr(config, "allow_msg_ttl", None) is not True
        )
        if not needs_update:
            return
        config.max_age = self._lease_ttl_seconds
        config.max_msgs_per_subject = 1
        config.allow_msg_ttl = True
        try:
            await self._js.update_stream(config)
        except Exception as exc:
            raise RuntimeError(
                f"Existing NATS KV bucket {self.name!r} is not configured for "
                f"Deckr's {self._lease_ttl_seconds:g}s broker-owned lease TTL. "
                f"Delete the development bucket/stream KV_{self.name} and restart."
            ) from exc

    async def _get_entry(self, key: str) -> StateEntry | None:
        kv = await self._available_kv()
        try:
            entry = await kv.get(key)
        except Exception as exc:
            if _is_key_missing(exc):
                return None
            raise StateUnavailable(f"Could not get state key {key!r}") from exc
        return _state_entry_from_kv(entry)

    def _validate_ttl(self, ttl: float | None) -> None:
        if ttl is None:
            return
        if abs(float(ttl) - self._lease_ttl_seconds) <= 0.001:
            return
        raise ValueError(
            "NATS current state uses the broker-owned bucket TTL "
            f"({self._lease_ttl_seconds:g}s); per-key TTL {ttl!r} is not supported "
            "during Sprint 1/2."
        )


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


def _state_change_from_nats_msg(
    msg,
    *,
    subject_prefix: str,
) -> StateChange | None:
    subject = str(msg.subject)
    if not subject.startswith(subject_prefix):
        return None
    key = subject[len(subject_prefix) :]
    headers = getattr(msg, "headers", None) or getattr(msg, "header", None) or {}
    marker_reason = headers.get(_NATS_MARKER_REASON_HEADER)
    if marker_reason == _NATS_MARKER_MAX_AGE:
        return StateChange("expire", key, None)
    if marker_reason is not None:
        return StateChange("delete", key, None)
    operation = str(headers.get(_KV_OPERATION_HEADER, "")).upper()
    if operation in {_KV_DELETE_OPERATION, _KV_PURGE_OPERATION}:
        return StateChange("delete", key, None)
    if msg.data is None:
        return None
    value = json.loads(msg.data.decode("utf-8")) if msg.data else {}
    metadata = getattr(msg, "metadata", None)
    sequence = getattr(metadata, "sequence", None)
    revision = getattr(sequence, "stream", 0) if sequence is not None else 0
    return StateChange(
        "put",
        key,
        StateEntry(
            key=key,
            value=state_value(value),
            revision=int(revision),
        ),
    )


def _kv_subject_prefix(bucket: str) -> str:
    return f"$KV.{bucket}."


def _kv_watch_pattern(prefix: str) -> str:
    if not prefix:
        return ">"
    if prefix.endswith("."):
        return f"{prefix}>"
    return prefix


def _exception_names(exc: BaseException) -> set[str]:
    names: set[str] = set()
    current: BaseException | None = exc
    while current is not None:
        names.add(type(current).__name__)
        current = current.__cause__
    return names


def _is_key_missing(exc: BaseException) -> bool:
    names = _exception_names(exc)
    if names & {"KeyNotFoundError", "KeyDeletedError", "NoKeysError", "NotFoundError"}:
        return True
    message = str(exc).lower()
    return message in {"missing", "not found", "key not found"}


def _is_revision_conflict(exc: BaseException) -> bool:
    names = _exception_names(exc)
    if "KeyWrongLastSequenceError" in names:
        return True
    if getattr(exc, "err_code", None) == 10071:
        return True
    message = str(exc).lower()
    return (
        message in {"exists", "revision changed"}
        or "wrong last" in message
        or "wrong sequence" in message
        or "revision changed" in message
    )


__all__ = [
    "NatsSubstrate",
    "_headers_for",
    "_subject_for",
]
