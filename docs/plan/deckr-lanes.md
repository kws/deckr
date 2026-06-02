# Developer instructions: simplify lanes and endpoint messaging

## 1. Decision

Keep **lane** as a protocol-visible message namespace, but remove it as an
endpoint/session owner or runtime authority.

A lane is:

```text
A stable DeckrMessage namespace used to select a message contract and transport
routing subject.
```

A lane is not:

```text
- an endpoint lifecycle manager
- a session identity factory
- a service registry
- a discovery authority
- an agreement or authorization authority
- a persistent queue, replay log, or current-state store
```

The protocol object is `DeckrMessage`: it carries `lane`, `messageType`,
sender/session, recipient/session, subject, TTL, reply correlation, causation,
trace, and body. The runtime API should be shaped around endpoint sessions that
stamp and exchange these envelopes.

Use this definition in code comments and docs:

```text
Lane

A lane is a stable message namespace in a DeckrMessage. It selects the message
contract used to validate sender family, recipient family, broadcast targets,
message type, and schema identity. Lanes provide ephemeral, at-most-once
command/data transport only. Lanes do not provide discovery, liveness,
ownership, authorization, persistence, replay, or current state.
```

Boundary rule:

```text
EndpointSession owns identity.
Beacon owns discovery.
Concord owns authority.
MessageBus owns ephemeral transport.
Lane is only a message namespace.
```

---

## 2. Primary API: endpoint-scoped messaging

The public messaging API should be endpoint-scoped, not lane-scoped.

Target usage:

```python
async with deckr.endpoint("controller:main") as endpoint:
    reply = await endpoint.request(
        lane="hardware_messages",
        recipient="hardware_manager:mirabox-main",
        subject=entity_subject(
            "hardware",
            managerId="mirabox-main",
            deviceId="deck-1",
            controlId="0,0",
            capabilityId="raster.bitmap",
        ),
        message_type="controlCommand",
        body={
            "deviceRef": {"managerId": "mirabox-main", "deviceId": "deck-1"},
            "controlId": "0,0",
            "capabilityId": "raster.bitmap",
            "commandType": "set_frame",
            "params": {},
        },
        timeout=2.0,
    )
```

```python
async with deckr.endpoint("hardware_manager:mirabox-main") as endpoint:
    async with endpoint.subscribe("hardware_messages") as messages:
        async for message in messages:
            await endpoint.reply_to(
                message,
                message_type="commandAccepted",
                body={"commandId": message.message_id},
            )
```

The endpoint session is created once and reused for:

```text
- Beacon advertisements
- Concord participant tokens
- message sending
- message receiving
```

This fixes the current design problem where
`Lane.register_endpoint(...)` creates a separate lane-local UUID session for the
same endpoint on each lane.

Target API:

```python
class Deckr:
    def endpoint(
        self,
        address: str | EndpointAddress,
        *,
        session_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> AbstractAsyncContextManager[EndpointSession]:
        ...
```

```python
@dataclass(frozen=True, slots=True)
class EndpointSessionInfo:
    address: EndpointAddress
    session_id: str
    metadata: Mapping[str, str]
```

```python
class EndpointSession:
    @property
    def address(self) -> EndpointAddress: ...

    @property
    def session_id(self) -> str: ...

    async def send(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        ...

    async def request(
        self,
        *,
        lane: str,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        ...

    async def reply_to(
        self,
        request: DeckrMessage,
        *,
        message_type: str,
        body: Mapping[str, Any],
        subject: EntitySubject | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        ...

    def subscribe(
        self,
        lane: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        ...
```

`EndpointSession` stamps:

```text
sender
senderSessionId
lane
recipient
recipientSessionId
subject
messageType
body
ttlMs
causationId
trace
```

Endpoint session rules:

```text
1. Endpoint sessions are local runtime identities.
2. They are not distributed presence records.
3. A supplied session id is allowed for deterministic tests.
4. A generated session id is the normal production path.
5. Closing an endpoint session stops local subscriptions opened by that session.
6. Closing an endpoint session does not directly cancel agreements; callers stop
   maintaining agreement tokens or cancel explicitly through the authority API.
```

---

## 3. Minimal Lane API

A public `Lane` object is optional. If retained, it must be a thin contract view
that uses an existing `EndpointSession`.

Allowed shape:

```python
class Lane:
    @property
    def name(self) -> str: ...

    @property
    def contract(self) -> MessageContract: ...

    async def send(
        self,
        endpoint: EndpointSession,
        *,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        ...

    async def request(
        self,
        endpoint: EndpointSession,
        *,
        recipient: str | EndpointAddress | MessageTarget,
        subject: EntitySubject,
        message_type: str,
        body: Mapping[str, Any],
        recipient_session_id: str | None = None,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
        ttl_ms: int | None = None,
        causation_id: str | None = None,
        trace: TraceContext | None = None,
    ) -> DeckrMessage:
        ...

    def subscribe(
        self,
        endpoint: EndpointSession,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        ...
```

Forbidden on `Lane`:

```python
Lane.register_endpoint(...)
Lane.create_session(...)
Lane.advertise(...)
Lane.claim(...)
Lane.authorize(...)
Lane.state(...)
Lane.service(...)
```

`RegisteredEndpointLane` should be deleted or made private after internal
examples and tests use `EndpointSession`.

---

## 4. Message bus API

Replace the lane substrate concept with a transport-focused message bus.

```python
class MessageBus(Protocol):
    def contract_for(self, lane: str) -> MessageContract:
        ...

    async def publish(self, message: DeckrMessage) -> None:
        ...

    async def request(
        self,
        message: DeckrMessage,
        *,
        timeout: float = 2.0,
        accept: ReplyPredicate | None = None,
    ) -> DeckrMessage:
        ...

    async def publish_reply(
        self,
        message: DeckrMessage,
        *,
        request: DeckrMessage,
    ) -> None:
        ...

    def subscribe(
        self,
        *,
        lane: str,
        endpoint: EndpointAddress,
        endpoint_session_id: str,
    ) -> AbstractAsyncContextManager[anyio.abc.ObjectReceiveStream[DeckrMessage]]:
        ...
```

The message bus only publishes, requests, replies, subscribes, and returns lane
contracts. It must not expose discovery, authority, services, or current-state
access.

---

## 5. Message contracts

Keep the runtime contract small and aligned with implemented behavior:

```python
@dataclass(frozen=True, slots=True)
class MessageContract:
    lane: str
    schema_id: str | None = None
    message_types: frozenset[str] = frozenset()
    allowed_sender_families: frozenset[str] | None = None
    allowed_recipient_families: frozenset[str] | None = None
    broadcast_targets: Mapping[str, str] = field(default_factory=dict)
    default_broadcast_hop_limit: int | None = None
```

Delivery semantics currently supported by the runtime:

```text
- ephemeral
- at-most-once
- no replay
- local or transport FIFO only
- drop expired messages
- local backpressure closes/drops the subscriber
- remote backpressure is transport disconnect behavior
- malformed messages are dropped and logged
```

Keep richer delivery metadata only as documentation/spec metadata unless it
becomes implemented behavior.

---

## 6. Validation and deliverability

Centralize validation in one function:

```python
def validate_message_for_contract(
    message: DeckrMessage,
    contract: MessageContract,
) -> None:
    ...
```

Validation must check:

```text
1. message.lane == contract.lane
2. message.message_type is allowed when contract.message_types is non-empty
3. sender family is allowed when configured
4. direct recipient family is allowed when configured
5. broadcast target scope/family is allowed when configured
6. recipientSessionId appears only on direct endpoint messages
7. body is valid for the lane/message schema if schema validation is available
```

Receive-side deliverability must check:

```text
1. message is not expired
2. message validates against its contract
3. recipientSessionId matches this endpoint session when present
4. recipient targets this endpoint or this endpoint family
```

The existing `validate_message_for_contract(...)` and
`message_is_deliverable(...)` behavior should be preserved and moved only as
needed to support the endpoint-scoped API.

---

## 7. NATS subject routing

The current sender-hinted subject shape forces each endpoint subscriber to
consume the whole lane and filter locally:

```text
deckr.lane.<lane-token>.<sender-family-token>.<sender-id-token>
```

Use recipient-hinted subjects for new publish/subscribe behavior:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>
deckr.msg.<lane-token>.broadcast.<scope-token>.<endpoint-family-token>
```

An optional sender hint may be appended after recipient fields:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>.from.<sender-family-token>.<sender-id-token>
```

Receive subscriptions:

```text
direct endpoint:
  deckr.msg.<lane>.to.<endpoint-family>.<endpoint-id>

broadcast to family:
  deckr.msg.<lane>.broadcast.*.<endpoint-family>
```

The envelope remains authoritative. On receive, validate that subject hints
agree with the `DeckrMessage` envelope. If subject and payload disagree, drop
and log the message.

During migration, the NATS implementation may subscribe to both old and new
subjects, but it should publish the new recipient-hinted subject.

---

## 8. Broadcast semantics

Broadcast remains modest:

```text
Deliver this ephemeral message to subscribers for the target endpoint family and
scope.
```

Broadcast does not mean:

```text
- durable fan-out
- guaranteed delivery
- presence enumeration
- authorization
- multi-hop routing, unless explicitly implemented
```

`BroadcastTarget` carries `scope`, `endpoint_family`, optional `domain`, and
optional `hop_limit`. If `domain` and `hop_limit` are not implemented by the
runtime, validate them but do not document them as guaranteed routing behavior.

---

## 9. Request/reply semantics

Request/reply is a message bus feature.

Rules:

```text
1. A request is a normal DeckrMessage with a generated messageId.
2. A reply is a normal DeckrMessage whose inReplyTo equals the request messageId.
3. The reply recipient is the original request sender.
4. The reply recipientSessionId is the original request senderSessionId.
5. The requester accepts the first deliverable reply that matches inReplyTo and
   the optional predicate.
6. Timeout means no accepted reply arrived in time.
```

The existing `inReplyTo` and recipient-session reply acceptance behavior should
be preserved.

---

## 10. Services lane boundary

If the `services` lane remains in core, it is ordinary command/reply traffic.
Its existence must not recreate service discovery, service authority, protected
views, or state ownership in the lane runtime.

Service-like behavior is composed from separate single-purpose APIs:

```text
- descriptor advertisements through discovery
- service-use agreements through authority
- service commands/replies through ordinary lanes
- optional application-owned views outside lane delivery
```

---

## 11. Migration sequence

1. Add `Deckr.endpoint(...)`, `EndpointSession`, `EndpointSessionInfo`, and
   `MessageBus`.
2. Move sender/session stamping from `RegisteredEndpointLane` into
   `EndpointSession`.
3. Update runtime code, tests, examples, and docs to use
   `async with deckr.endpoint(...) as endpoint:`.
4. Keep `Lane` temporarily only as a contract view and convenience wrapper over
   `EndpointSession`.
5. Remove public `Lane.register_endpoint(...)` and `RegisteredEndpointLane`.
6. Simplify runtime message contracts to the fields used for validation.
7. Switch NATS publishing to recipient-hinted subjects, with old-subject receive
   compatibility only if needed for one transition window.

---

## 12. Tests to add

API tests:

```text
- EndpointSession creates one session id reused across multiple lane
  sends/subscriptions.
- Lane, if retained, does not create sessions.
- send() stamps sender and senderSessionId from EndpointSession.
- request/reply preserves messageId, inReplyTo, and recipientSessionId.
```

Validation tests:

```text
- wrong lane is rejected
- unsupported message type is rejected
- disallowed sender family is rejected
- disallowed recipient family is rejected
- disallowed broadcast target is rejected
- recipientSessionId on broadcast is rejected
- expired message is not delivered
```

NATS routing tests:

```text
- direct recipient subscribes only to its recipient-hinted subject
- unrelated endpoints do not receive direct messages
- broadcast subscribers receive matching family/scope messages
- subject/payload mismatch is dropped and logged
```

Boundary tests:

```text
- lane subscription does not imply Beacon advertisement
- lane subscription does not imply Concord authority
- Beacon withdrawal does not stop lane subscription
- Concord cancellation does not delete lane subscriptions
- command authorization, where required, is checked by authority/profile code
  before command handling, not by lane delivery
```

---

## 13. Definition of done

The lane refactor is complete when:

```text
1. The public messaging API is endpoint-scoped.
2. Endpoint sessions are no longer lane-local.
3. Lanes are only message namespaces and contract selectors.
4. Lane code does not advertise, discover, claim, authorize, manage services, or
   expose current-state access.
5. Message contract validation is centralized and small.
6. Runtime delivery semantics match what is implemented: ephemeral, at-most-once,
   no replay.
7. NATS subject routing is recipient-hinted or otherwise avoids whole-lane
   fan-out to every endpoint.
8. Beacon and Concord examples use EndpointSession identity consistently.
9. The services lane, if retained, is ordinary message traffic only.
```
