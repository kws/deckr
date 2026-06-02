# Developer instructions: define and simplify the Lane API

## 1. Decision

Keep **lane** as a protocol-visible message namespace, but remove it as a heavyweight runtime construct.

A lane should be:

```text
A named, ephemeral message channel used to select a message contract and NATS routing subject.
```

A lane should **not** be:

```text
- an endpoint lifecycle manager
- a session identity factory
- a service registry
- a queue with replay/durability semantics
- a discovery authority
- an agreement/authorization authority
- a generic state/KV accessor
```

The current `DeckrMessage` envelope already has a `lane` field plus `messageType`, sender/session, recipient/session, subject, TTL, reply correlation, causation, trace, and body. That envelope is the important protocol object.  The current `Lane` / `RegisteredEndpointLane` object model should be simplified around that envelope.

---

## 2. Core definition

Use this definition in code comments and docs:

```text
Lane

A lane is a stable message namespace in a DeckrMessage. It selects the message
contract used to validate sender family, recipient family, broadcast targets,
message type, and schema identity. Lanes provide ephemeral, at-most-once
command/data transport only. Lanes do not provide discovery, liveness, ownership,
authorization, persistence, replay, or current state.
```

This aligns with the existing docs: lane messages are ordinary command/data messaging, while Beacon and Concord are the discovery and agreement authorities. 

---

## 3. Responsibilities

### A lane is responsible for

```text
1. Naming a message namespace.
2. Selecting the message contract for validation.
3. Validating allowed message types.
4. Validating allowed sender endpoint families.
5. Validating allowed recipient endpoint families.
6. Validating allowed broadcast targets.
7. Providing a routing hint for the transport.
8. Preserving request/reply correlation through messageId and inReplyTo.
9. Enforcing TTL/expiry behavior at receive time.
10. Applying local backpressure behavior.
```

The current `LaneContract` already captures most of this: `lane`, `schema_id`, `message_types`, allowed sender/recipient families, broadcast targets, and default broadcast hop limit.  The existing core contracts define `actions`, `hardware_messages`, and `services` with message type and endpoint-family constraints. 

### A lane is not responsible for

```text
1. Creating endpoint sessions.
2. Registering endpoint presence.
3. Advertising features.
4. Discovering candidates.
5. Proposing or validating agreements.
6. Authorizing protected commands.
7. Owning service-use leases.
8. Reading or writing KV state.
9. Persisting messages.
10. Retrying, replaying, or ordering beyond local/transport FIFO.
```

The current `LaneSubstrate` mixes message transport with `state(...) -> StateStore`. That should be removed; message transport must not expose generic state. 

---

## 4. Preferred public API: endpoint-scoped messaging

The primary public API should be endpoint-scoped, not lane-scoped.

### Target usage

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
            ...
```

The endpoint session is created once and reused for:

```text
- Beacon advertisements
- Concord participant tokens
- message sending
- message receiving
```

This fixes the current issue where `Lane.register_endpoint()` creates a new lane-local UUID session every time an endpoint is registered on a lane. 

### Required classes

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

The endpoint object should stamp:

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

The old `RegisteredEndpointLane` stamping behavior should survive, but it should move to `EndpointSession`. The current lane-bound sender already performs this stamping before publishing. 

---

## 5. Internal API: message bus, not lane substrate

Replace `LaneSubstrate` with a transport-focused API.

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

Do **not** include:

```python
def state(...) -> StateStore
```

Beacon, Concord, and application KV should be separate runtime components.

---

## 6. Minimal Lane API, only if retained

A public `Lane` object is optional. Prefer not to expose it. If retained for compatibility or ergonomics, it must be a thin contract view only.

### Allowed shape

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

### Explicitly forbidden on `Lane`

Do not implement:

```python
Lane.register_endpoint(...)
Lane.create_session(...)
Lane.advertise(...)
Lane.claim(...)
Lane.authorize(...)
Lane.state(...)
Lane.service(...)
```

`Lane` must never create or own endpoint identity. It may only use an existing `EndpointSession`.

---

## 7. Message contract simplification

The current `DeliverySemantics` type models persistence, delivery guarantee, replay, ordering, expiry handling, backpressure, malformed-message handling, idempotency, message families, and ordering keys.  But the registry rejects anything except the one currently implemented profile: ephemeral, at-most-once, no replay, local/connection FIFO, drop-and-report expiry, drop-subscriber local backpressure, disconnect remote backpressure, and drop/log malformed messages. 

Move that richer delivery model out of the hot runtime path.

Use this runtime contract:

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

Keep delivery metadata only as documentation/spec metadata unless it becomes implemented behavior.

---

## 8. Validation rules

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

Current `validate_message_for_contract()` already performs most of this validation and should be preserved or moved. 

Receive-side deliverability must check:

```text
1. message is not expired
2. message validates against its contract
3. recipientSessionId matches this endpoint session when present
4. recipient targets this endpoint or this endpoint family
```

The current `message_is_deliverable()` already performs these checks. 

---

## 9. NATS subject routing

The current documented subject shape is sender-hinted:

```text
deckr.lane.<lane-token>.<sender-family-token>.<sender-id-token>
```

The docs say the subject is only an optimization and the payload is authoritative. 

That subject shape should be replaced or augmented because it forces each endpoint subscriber to consume the whole lane and filter locally. The current implementation subscribes to:

```text
deckr.lane.<lane>.>
```

then parses and filters messages by recipient/session in Python. 

Use recipient-hinted subjects instead:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>
deckr.msg.<lane-token>.broadcast.<scope-token>.<endpoint-family-token>
```

Optional sender hint may be added after recipient fields:

```text
deckr.msg.<lane-token>.to.<recipient-family-token>.<recipient-id-token>.from.<sender-family-token>.<sender-id-token>
```

Receive subscriptions become:

```text
direct endpoint:
  deckr.msg.<lane>.to.<endpoint-family>.<endpoint-id>

broadcast to family:
  deckr.msg.<lane>.broadcast.*.<endpoint-family>
```

Payload remains authoritative. On receive, validate that subject hints agree with the `DeckrMessage` envelope. If subject and payload disagree, drop and log.

This preserves interop safety while reducing fan-out.

---

## 10. Broadcast semantics

Keep broadcast, but keep it modest.

A broadcast target means:

```text
Deliver this ephemeral message to subscribers for the target endpoint family and scope.
```

Broadcast does not mean:

```text
durable fan-out
guaranteed delivery
presence enumeration
authorization
multi-hop routing, unless explicitly implemented
```

`BroadcastTarget` already carries `scope`, `endpoint_family`, optional `domain`, and optional `hop_limit`.  If `domain` and `hop_limit` are not implemented, validate but do not over-promise them.

---

## 11. Request/reply semantics

Request/reply should remain a message bus feature, not a lane object feature.

Rules:

```text
1. A request is a normal DeckrMessage with a generated messageId.
2. A reply is a normal DeckrMessage whose inReplyTo equals the request messageId.
3. The reply recipient is the original request sender.
4. The reply recipientSessionId is the original request senderSessionId.
5. The requester accepts the first deliverable reply that matches inReplyTo and optional predicate.
6. Timeout means no accepted reply arrived in time.
```

The current implementation already uses `inReplyTo` and recipient session matching for reply acceptance.  

---

## 12. Endpoint sessions

Create a new endpoint session abstraction.

```python
@dataclass(frozen=True, slots=True)
class EndpointSessionInfo:
    address: EndpointAddress
    session_id: str
    metadata: Mapping[str, str]
```

Rules:

```text
1. Endpoint sessions are local runtime identities.
2. They are not distributed presence records.
3. They are reused across Beacon, Concord, and MessageBus.
4. Closing an endpoint session stops local subscriptions.
5. Closing an endpoint session does not automatically cancel Concord contracts unless the owning component chooses to cancel or stops maintaining its token.
6. A session id may be caller-supplied for deterministic tests, but is normally generated by the runtime.
```

This aligns with the docs: endpoint sessions are local runtime and message-envelope identities; Beacon carries discovery evidence; Concord carries live agreement authority. 

---

## 13. Service lane after service-layer removal

Once the Python service runtime layer is removed, do not keep `services` as an unconditional core runtime dependency.

Current core lane names are:

```text
actions
hardware_messages
services
```



After removing the service runtime, treat `services` as one of:

```text
1. a compatibility lane, retained temporarily but not used by core runtime; or
2. an optional profile-provided extension contract; or
3. removed from CORE_LANE_NAMES in a breaking-change branch.
```

Do not let the existence of a `services` lane recreate a hidden service authority layer. Service-like behavior should be composed from:

```text
Beacon descriptor advertisement
Concord service-use agreement
ordinary message bus commands
optional application-owned views
```

---

## 14. Migration plan

### Step 1: introduce endpoint-scoped API

Add:

```python
Deckr.endpoint(...)
EndpointSession
MessageBus
MessageContract
```

Keep existing `Lane` temporarily.

### Step 2: make `Lane.register_endpoint()` deprecated

Change:

```python
async with deckr.lane("hardware_messages").register_endpoint(endpoint) as lane:
    ...
```

to emit a deprecation warning pointing to:

```python
async with deckr.endpoint(endpoint) as endpoint_session:
    async with endpoint_session.subscribe("hardware_messages") as messages:
        ...
```

### Step 3: move session ownership

Ensure `EndpointSession` is the only normal source of `session_id`.

Beacon and Concord examples must pass:

```python
endpoint.address
endpoint.session_id
```

not `lane.endpoint` and `lane.session_id`.

### Step 4: remove `LaneSubstrate.state()`

Move any generic state access to `Deckr.app_state(...)` or a dedicated app KV component. Beacon and Concord must not use it.

### Step 5: switch NATS subject routing

Add recipient-hinted subjects while retaining old sender-hinted subscribe compatibility for one transition window if needed.

During compatibility:

```text
publish new subject
optionally subscribe old subject
validate payload in both cases
```

### Step 6: remove public `RegisteredEndpointLane`

Delete or make private after all internal examples and tests use `EndpointSession`.

---

## 15. Tests to add

### API tests

```text
- EndpointSession creates one session id reused across multiple lane sends/subscriptions.
- Lane, if retained, does not create sessions.
- send() stamps sender and senderSessionId from EndpointSession.
- request/reply preserves messageId/inReplyTo/recipientSessionId.
```

### Validation tests

```text
- wrong lane is rejected
- unsupported message type is rejected
- disallowed sender family is rejected
- disallowed recipient family is rejected
- disallowed broadcast target is rejected
- recipientSessionId on broadcast is rejected
- expired message is not delivered
```

### NATS routing tests

```text
- direct recipient subscribes only to its recipient-hinted subject
- unrelated endpoints do not receive direct messages
- broadcast subscribers receive matching family/scope messages
- subject/payload mismatch is dropped and logged
```

### Authority-boundary tests

```text
- lane subscription does not imply Beacon advertisement
- lane subscription does not imply Concord authority
- Beacon withdrawal does not stop lane subscription
- Concord cancellation does not delete lane subscriptions
- command authorization, where required, is checked through Concord/profile code, not lane delivery
```

---

## 16. Definition of done

The lane refactor is complete when:

```text
1. The public messaging API is endpoint-scoped.
2. Endpoint sessions are no longer lane-local.
3. Lanes are only message namespaces / contract selectors.
4. Lane code does not expose StateStore.
5. Lane code does not advertise, discover, claim, authorize, or manage services.
6. Message contract validation is centralized and small.
7. Runtime delivery semantics match what is actually implemented: ephemeral, at-most-once, no replay.
8. NATS subject routing is recipient-hinted or otherwise avoids whole-lane fan-out to every endpoint.
9. Beacon and Concord examples use EndpointSession identity consistently.
10. The service lane is compatibility-only or moved out of core once the service runtime is removed.
```

The key design rule is:

```text
EndpointSession owns identity.
Beacon owns discovery.
Concord owns authority.
MessageBus owns ephemeral transport.
Lane is only a message namespace.
```
