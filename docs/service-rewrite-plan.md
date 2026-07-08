# Service Subscription Rewrite Plan

> Design plan, not current implementation. This document captures the intended
> service client rewrite for reducing service-use boilerplate and Concord churn
> before v1. When implemented, the resulting behavior should be promoted into
> BAU docs, code, tests, and examples.

## Implementation Status

As of July 6, 2026, the core, Sonos, OpenHAB, and Kaj consumer implementation
slices have landed in the working tree, but the full cross-plugin rewrite plan
below is not complete.

Completed so far:

- `deckr.services` now exports the shared subscription state enum, generic
  subscription message model, logical resource subscription session, shared
  resource subscription manager, and shared service request path.
- `DeckrServices` now owns a runtime-scoped shared manager cache and closes
  shared managers during service shutdown before closing remaining direct
  service-use leases.
- Core tests cover overlapping logical sessions, retained resource union
  behavior, replacement-set updates, last-subscriber cleanup, missing view to
  `UNAVAILABLE` without lease churn, lease-loss reconnect and retained-scope
  reapply, command-pool lease reuse, and command-pool service-use-loss retry.
- `SonosServiceClient.zone_subscription_session()` now returns a logical
  message session backed by a shared Sonos zone manager, accepts initial
  `zones`, and uses a provider-level Sonos zone subscriber id for
  `setZoneScope`.
- Sonos zone sessions now expose `messages`, `set_zone_scope()`, and
  lease-backed `command()` behavior. The retained zone set is both the
  authoritative state/watch set and the authority boundary for zone-scoped
  commands. Sonos view absence is converted into a subscription `UNAVAILABLE`
  message instead of being treated as service-use loss.
- Sonos volume rotary now consumes subscription messages and no longer owns
  explicit scope mutation, view-watch, release, or service-use-loss
  classification boilerplate.
- Sonos media, group, shortcut, and non-volume command actions now open zone
  sessions during mount or page-open lifecycle and route reads and writes
  through `session.request(...)`.
- Sonos no longer exposes a public one-shot client command API. Zone-bound
  actions keep a zone session open and do not hide a missing session behind
  command-pool fallback.
- Sonos provider-side scope cleanup uses `setZoneScope`; `ensureZones` and
  `releaseZones` were removed rather than kept as compatibility aliases.
- Kaj status bar uses the Sonos message session instead of owning direct
  Sonos retry/release logic.
- `OpenHABServiceClient.item_subscription_session()` now returns a logical
  message session backed by a shared OpenHAB item manager, accepts initial
  `items`, and uses a provider-level OpenHAB item subscriber id for
  `setItemScope`.
- OpenHAB item sessions expose `messages`, `set_item_scope()`, and
  lease-backed `command()` behavior. The retained item set is both the
  authoritative state/watch set and the authority boundary for item-scoped
  `sendCommand`.
- OpenHAB provider-side scope cleanup uses `setItemScope`; `ensureItems`,
  `releaseItems`, and `refreshItem` were removed rather than kept as
  compatibility aliases.
- OpenHAB item actions and Kaj OpenHAB consumers now open item sessions
  directly during mount lifecycle and route item commands through the active
  session.
- Kaj status bar now uses matching direct Sonos and OpenHAB message-session
  patterns.
- `deckr/docs/usage.md` now documents managed subscriptions and shared command
  pools, and demotes direct service-use-loss helper usage to low-level
  infrastructure guidance.

Known remaining work against this plan:

- The explicit one-shot fallback policy for command scopes that should not be
  pooled still needs to be formalized.

Deckr services already use the right authority model: Beacon discovers
candidate services, Concord owns service-use authority, and service views are
fenced by the live service-use contract. The current consumer API still exposes
too much of that machinery to plugin authors. Simple subscribers such as a
Sonos status display or an OpenHAB item key currently need to manage retry
loops, lease-loss classification, release behavior, stale view handling, and
successor lease negotiation themselves.

This rewrite should move those concerns into the service client layer, with
reusable lifecycle mechanics in `deckr.services` and domain-specific resource
semantics in each plugin client.

## Goals

- Let feature code subscribe to resource state messages instead of Concord
  lifecycle details.
- Require ordinary feature code to perform all service reads, watches, and
  writes through an explicit service session.
- Reuse service-use contracts across same-provider logical subscribers where
  possible, especially for long-lived item and zone subscriptions.
- Avoid input-path Concord churn by opening resource sessions as early as
  possible in the action lifecycle and using those active sessions for
  interaction commands.
- Keep Beacon and Concord semantics strict: no reattaching, no Beacon-as-
  liveness, no reused cancelled contracts, and no parallel lifecycle authority.
- Keep service provider implementations mostly intact. Sonos uses
  `setZoneScope` and OpenHAB uses `setItemScope` as full replacement retained
  resource sets.

## Original Problems And Remaining Gaps

Before this rewrite, subscription consumers duplicated lifecycle code in several
places:

- Kaj status bar owns Sonos retry/release/clear logic directly.
- Sonos volume rotary owns its own zone subscribe loop and lease-loss handling.
- OpenHAB item actions used a mixin that owned retry, release, multi-watch task
  cancellation, and service-use loss classification.
- Sonos command clients retried after service-use loss, while OpenHAB command
  clients did not.

The Sonos volume rotary, command, group, media shortcut, media shortcuts,
OpenHAB item action, Garage key, and Kaj status bar paths now follow the
intended session pattern.

That duplication created bugs and inconsistent behavior, and the same risks
remain for consumers that have not yet moved behind domain service clients:

- `None` from a fenced service view is sometimes treated as "successor lease
  required", but it can also mean ordinary view absence or deletion under the
  current valid lease.
- Retry delays and logging policies differ across consumers.
- Each mounted action can open its own Concord service-use contract even when
  many actions need the same service, zones, or items.
- Actions must know about `service_unavailable_ends_service_use()` and
  `service_reply_ends_service_use()`, which should be service-client
  internals for ordinary feature code.

## Target Consumer API

The author-facing API should preserve the simple shape:

```python
async with sonos.zone_subscription_session(
    SONOS_SERVICE_ID,
    zones=zones,
) as session:
    await session.set_zone_scope(dynamic_zones)

    async for message in session.messages:
        if message.state is ServiceSubscriptionState.READY:
            await update_view(message.resource, message.payload)
        else:
            await render_pending_or_unavailable(message.resource, message)
```

For ordinary feature code, this is a hard boundary: no service reads, service
view watches, or service requests without a service session. A lower-level
service-use lease also counts as a session for infrastructure code, but action
code should normally see the domain session object, not Concord lease plumbing.

Resource-bound actions should open their session as soon as the action lifecycle
identifies the resource set:

- binding actions: in `mounted()`, usually through a binding-scoped background
  task;
- dynamic pages: in `opened()` or before child actions begin resolving content;
- page children: use the owning page/action session instead of opening a fresh
  session per child.

Input handlers should then call `session.request(...)` on the already-open
session. If no session is currently active, the action should render a connected
/ disconnected / unavailable state from the latest session message and return;
it should not hide the missing session by opening a short-lived command lease on
the press or rotation path.

OpenHAB should mirror this:

```python
async with openhab.item_subscription_session(
    OPENHAB_SERVICE_ID,
    items=items,
) as session:
    async for message in session.messages:
        ...
```

The `session` object is a logical subscription handle. It may be backed by a
provider-shared service-use contract, not a private Concord contract for that
single action. Public session methods update this logical handle's requested
resources; the shared manager owns the actual service-use contract and service
commands.

Reconnect behavior is manager-owned, not a per-session boolean. If a consumer
does not want to wait for a successor service-use contract, it should exit the
context manager when it receives a state message it treats as terminal, such as
`RECONNECTING` or `ERROR`.

Use a small shared state enum:

```python
class ServiceSubscriptionState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    RECONNECTING = "reconnecting"
    ERROR = "error"
```

Messages should always include the resource identity and state. Payload is
present only for `READY`.

```python
@dataclass(frozen=True, slots=True)
class ServiceSubscriptionMessage(Generic[ResourceT]):
    resource: ResourceT
    state: ServiceSubscriptionState
    payload: Mapping[str, Any] | None = None
    error: ServiceError | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
```

Plugin clients can expose typed aliases such as `SonosZoneSubscriptionMessage`
and `OpenHABItemSubscriptionMessage`, but they should use the same core state
semantics.

State meanings:

- `PENDING`: resource is requested but no fresh authoritative payload is
  available yet.
- `READY`: payload is current under the active service-use contract.
- `UNAVAILABLE`: the service reports the resource as absent or unavailable, or
  the fenced view is absent under an otherwise usable lease.
- `RECONNECTING`: the previous service-use contract was lost and a successor is
  being negotiated.
- `ERROR`: non-service-use failure that the manager cannot hide or classify as
  ordinary unavailability.

Feature code should not infer state from `payload is None`.

## Shared Manager Model

Each plugin client should delegate long-lived subscriptions to a
provider-instance-scoped manager. The provider runtime already has a
provider-scoped `DeckrServices` instance and provider task hook; the rewrite
should use that lifecycle instead of binding subscriptions directly to each
action task.

For a service id and compatible scope, the manager should:

- Hold one active service-use lease for the shared subscription set.
- Use a stable provider-level subscriber id, for example
  `provider:<provider-instance-id>:sonos-zones` or
  `provider:<provider-instance-id>:openhab-items`.
- Track logical subscribers keyed by local handle id or binding id.
- Maintain the union of requested resources across logical subscribers.
- For replacement-set domains such as Sonos and OpenHAB, call one
  resource-scope operation with the full retained union whenever the shared set
  changes or a successor contract is negotiated. The retained union is both the
  state/watch set and the resource-command authority boundary.
- Watch each retained service view once and fan out messages to interested
  logical sessions.
- Emit `RECONNECTING` on lease loss, then negotiate a successor contract and
  reapply the current retained union.
- On session exit, unregister only that logical subscriber and shrink the
  retained set if no other subscriber needs the same resource.

Single retained-union updates are required for replacement-set domains. The
shared manager must send the full retained union for its provider-level
subscriber id, not only newly added resources.

Dynamic resource methods should have explicit local semantics:

- `set_zone_scope(zones)` replaces the logical Sonos zone scope. An empty set
  clears that logical scope and removes zone-command authority.
- `set_item_scope(items)` replaces the logical OpenHAB item scope. An empty set
  clears that logical scope and removes item-command authority.
- Context exit drops all resources owned by the logical session.

The Sonos service operation name is `setZoneScope`. The OpenHAB service
operation name is `setItemScope`.

## Core Reusable Library

Add reusable primitives to `deckr.services`; keep Sonos/OpenHAB concepts out of
core.

Core should own:

- Service subscription state enum and generic message model.
- Lease supervisor behavior: descriptor resolution, service-use negotiation,
  lease refresh, successor lease loop, service-use-loss classification, and
  close/release rules.
- Shared resource subscription manager behavior: logical session registration,
  retained resource union, fanout streams, backpressure/drop policy, and
  lifecycle logging.
- Helpers for retrying commands when service-use authority is lost.
- A `DeckrServices`-owned cache for provider/runtime-scoped managers so
  `SonosServiceClient(self.services)` and `OpenHABServiceClient(self.services)`
  can be lightweight facades over shared state.

Core should not own:

- Sonos zone names, Sonos command names, Sonos view refs, or Sonos-specific
  payload interpretation.
- OpenHAB item names, OpenHAB command names, OpenHAB view refs, or item-state
  interpretation.
- Display policy such as "render old media until reconnect" or "clear garage
  state when unavailable".

The generic manager should be configured with domain callbacks:

- Resolve/select descriptor for the service id.
- Build operations and view scope for a retained resource set.
- Apply retained resources for the active lease, either as a full replacement
  set or as domain-specific ensure/release mutations.
- Build `ServiceViewRef` for a resource.
- Convert view payload or view absence into a subscription message.
- Optionally expose lease-backed commands allowed by the same subscription
  scope.

## Command Paths

The original rewrite described command-only clients as opening short service-use
contracts for each call. That is only acceptable as low-level infrastructure for
operations that have no retained resource session.

For ordinary action code, "command-only" must not mean "no session". Once an
action binding knows the Sonos zone, OpenHAB item, or other service resource it
acts on, it should open the matching domain session early in its lifecycle and
use that session for both state reads and writes.

Command execution should prefer:

1. The action's own logical subscription session when the command is related to
   an already-retained resource, such as Sonos `playMusicItem`,
   `resolveMusicShortcut`, `adjustVolume`, or `leaveGroup` for a configured
   zone.
2. A provider-shared command session keyed by service id and compatible
   operation set only for operations with no durable resource session.
3. A one-shot service-use contract when no shared lease exists or the command
   scope is too specific to pool safely. This remains a service session; it
   should not be reached by normal bound zone/item action interactions.

If a resource-bound action has no active logical session when input arrives, the
correct user-facing behavior is to show disconnected/unavailable state and avoid
the service request. Falling back to a short-lived request lease on that input
path reintroduces latency and Concord churn, and hides the session health that
the button should display.

Shared request sessions should still obey Concord semantics:

- Refresh before use.
- Treat lease-loss replies and `ServiceUnavailable` codes as authority loss.
- Cancel/close ended leases and negotiate successors; never reattach.
- Return ordinary `ServiceReplyBody` statuses to feature code without
  exposing Concord details.

This should make OpenHAB command behavior match the shared request-session
behavior and reduce per-click Concord churn for common actions.

## Plugin-Specific Changes

### Sonos

Rewrite `SonosServiceClient.zone_subscription_session()` to return a logical
session backed by a shared Sonos zone manager.

The Sonos manager should:

- Use one retained union of zones per service id and compatible operation set.
- Include additional operations requested by consumers, such as `adjustVolume`,
  `play`, `pause`, `resolveMusicShortcut`, `resolveFavourite`,
  `playMusicItem`, `listZones`, and `leaveGroup`, in the shared lease scope
  needed by the action.
- Fan out zone view messages by zone name.
- Use the active shared lease for zone-related commands required by the logical
  session.
- Use Sonos `setZoneScope` as a full replacement retained zone set.

Expected consumer simplifications:

- Sonos volume rotary consumes zone messages and calls session/manager commands
  instead of owning its own subscription loop.
- Sonos media shortcut, media shortcuts, play favourite, group, and command
  actions open `zone_subscription_session(...)` as soon as they mount or open a
  page, retain the configured zone, and call `session.request(...)` from render
  and input paths.
- Sonos action buttons can render connected, disconnected, unavailable, and
  error states from session messages before interaction, and input handlers do
  not pay first-press Concord negotiation latency.

### OpenHAB

Rewrite `OpenHABServiceClient.item_subscription_session()` around a shared
OpenHAB item manager.

The OpenHAB manager should:

- Use one retained union of items per service id.
- Fan out item view messages by item name.
- Convert missing item views into `UNAVAILABLE`, not service-use loss.
- Hide retained-scope replies and lease-loss retries from actions.
- Keep action callbacks focused on item state changes or subscription-state
  messages.

Expected consumer simplifications:

- `OpenHABItemWatcherMixin` is retired from the public/recommended consumer
  API; actions use direct item sessions.
- Item actions no longer import service-use-loss helper functions.
- OpenHAB item commands use the active retained item session and do not fall
  back to command-pool or one-shot service-use leases.

## Service Provider Boundary

The service provider side should stay mostly stable.

OpenHAB and Sonos already store subscriptions by:

- Concord contract generation
- client endpoint
- client session id
- subscriber id

That shape supports provider-level subscribers. The new manager should simply
use a stable provider-level subscriber id instead of one subscriber id per
binding. Service providers should continue to drop contract-bound subscriptions
when Concord reports cancellation, invalidity, or release.

Do not add provider-side lifecycle authority outside Concord. Service view
presence, current-state buckets, endpoint sessions, catalogs, Beacon
advertisements, or ad hoc lease records must not become lifecycle authority.

Provider-side changes should be limited to tests or small protocol cleanup unless
the manager reveals a concrete bug in retained-scope mutation behavior.

## Failure Modes

The manager should provide consistent behavior for these cases:

- Discovery pending: logical sessions emit `PENDING` until a usable descriptor is
  available.
- Service backend unavailable: logical sessions emit `UNAVAILABLE` with service
  diagnostics.
- Retained-scope mutation reports ordinary unavailable/rejected: affected
  resources emit `UNAVAILABLE` or `ERROR`, depending on service error code.
- Retained-scope mutation or view watch reports terminal service-use loss: all
  retained resources on that lease emit `RECONNECTING`; the manager negotiates
  a successor and reapplies the retained union.
- Fenced view missing or deleted under a valid lease: affected resource emits
  `UNAVAILABLE`.
- Logical session closes while a reconnect is in flight: its resources are
  removed from the retained union and it receives no further messages.
- Provider runtime closes: managers release usable active subscriptions
  best-effort, close leases, and stop streams.

## Multi-Agent Implementation Slices

This is a good multi-agent task if the interfaces are agreed first. Suggested
slices:

1. Core service subscription primitives in `deckr.services`, with unit tests
   using fake descriptors, leases, commands, and view streams.
2. Sonos client rewrite using the core manager, including volume rotary and
   resource-session action cleanup.
3. OpenHAB client rewrite using the core manager, including item action
   migration.
4. Kaj status bar migration after OpenHAB, so it can use the new Sonos and
   OpenHAB client APIs together.
5. Command session reuse and resource-session cleanup for OpenHAB and Kaj
   callers.
6. Documentation and examples update after implementation, replacing old
   service-use examples that show manual lease-loss handling.

The core API and message/state names should be landed before plugin agents
start, so Sonos and OpenHAB do not invent incompatible local abstractions.

## Test Plan

Core tests:

- Logical sessions receive `PENDING`, `READY`, `UNAVAILABLE`,
  `RECONNECTING`, and `ERROR` messages with resource identity.
- Multiple logical sessions for overlapping resources share one lease and one
  retained resource union.
- Dropping one logical session does not release a resource still retained by
  another session.
- Terminal lease loss triggers successor negotiation and reapplies the retained
  union.
- View absence under a valid lease emits `UNAVAILABLE`, not `RECONNECTING`.
- Command retry uses successor leases for service-use loss and does not retry
  ordinary service errors.

Sonos tests:

- Two zone consumers for the same zone result in one shared `setZoneScope` set.
- Adding/removing zones updates the retained union with a full `setZoneScope`
  replacement, including remove-only and empty-set updates.
- Zone-scoped Sonos commands carry `subscriberId`, target only retained zones,
  and do not fall back to a command pool when the retained session is absent.
- `joinGroup`, `joinAll`, and `splitAll` are not advertised or exposed; direct
  calls are rejected as unsupported. `leaveGroup` remains scoped to the
  retained zone.
- Volume rotary uses the active shared lease for `adjustVolume`.
- Zone-bound Sonos actions open `zone_subscription_session(...)` during
  mount/page-open lifecycle, include their needed command operations, and use
  `session.request(...)` for render and input commands.
- Zone-bound Sonos input handlers with no active session render unavailable or
  disconnected state and do not call the service through a command-pool fallback.
- Production Sonos action code does not import service-use-loss helper
  functions and routes ordinary zone-bound interactions through zone sessions.
- Existing service provider tests for contract cleanup still pass unchanged.

OpenHAB tests:

- Multiple item actions share one retained `setItemScope` set.
- Missing item view emits `UNAVAILABLE` to the item subscriber.
- Item watcher/action code no longer imports or directly calls service-use-loss
  helper functions.
- OpenHAB item commands carry `subscriberId`, target only retained items, and
  do not fall back to a command pool when the retained session is absent.
- Existing provider-side subscription cleanup behavior still passes.

Integration-style tests:

- Simulate service restart: old contract becomes invalid, managers emit
  `RECONNECTING`, negotiate a successor, reapply retained resources, and emit
  fresh `READY` messages.
- Simulate action unmount during reconnect: no leaked logical subscribers and no
  release command against an ended lease.
- Simulate many bindings for one service: Concord proposal count stays bounded
  by compatible shared scopes instead of binding count.

## Rollout Notes

Deckr is pre-v1, so this should replace the old internal API rather than adding
compatibility shims. Keep compatibility only at real external protocol
boundaries. Update all internal callers, tests, examples, and docs together.

Do the rewrite in a branch that can touch `deckr`, `deckr-plugin-sonos`,
`deckr-plugin-openhab`, and `deckr-plugin-kaj` together. Commit child repos
first, then update parent submodule pins only after the child commits exist.

The acceptance bar is that ordinary action/plugin code should not need to:

- import `service_unavailable_ends_service_use`
- import `service_reply_ends_service_use`
- track `lease_usable`
- manually sleep for successor leases
- decide whether a release command is safe after lease loss
- treat `None` payloads as a proxy for lifecycle state
