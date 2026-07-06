# Service Subscription Rewrite Plan

> Design plan, not current implementation. This document captures the intended
> service client rewrite for reducing service-use boilerplate and Concord churn
> before v1. When implemented, the resulting behavior should be promoted into
> BAU docs, code, tests, and examples.

## Implementation Status

As of July 6, 2026, the first implementation slice has landed in the working
tree but the full rewrite plan below is not complete.

Completed so far:

- `deckr.services` now exports the shared subscription state enum, generic
  subscription message model, logical resource subscription session, shared
  resource subscription manager, and shared service command pool.
- `DeckrServices` now owns a runtime-scoped shared manager cache and closes
  shared managers during service shutdown before closing remaining direct
  service-use leases.
- Core tests cover overlapping logical sessions, retained resource union
  behavior, last-subscriber release, missing view to `UNAVAILABLE`, lease-loss
  reconnect and re-ensure, command-pool lease reuse, and command-pool
  service-use-loss retry.
- `SonosServiceClient.zone_subscription_session()` now returns a logical
  message session backed by a shared Sonos zone manager, accepts initial
  `zones`, and uses a provider-level Sonos zone subscriber id for
  `ensureZones` / `releaseZones`.
- Sonos zone sessions now expose `messages`, `ensure_zones()`, `drop_zones()`,
  and lease-backed `command()` behavior. Sonos view absence is converted into a
  subscription `UNAVAILABLE` message instead of being treated as service-use
  loss.
- `SonosServiceClient.command()` now uses a shared command pool, so Sonos
  command-only callers reuse compatible service-use leases and retry
  service-use-loss replies through the shared helper layer.
- Sonos volume rotary now consumes subscription messages and no longer owns
  explicit `ensureZones`, `watch_zone`, release, or service-use-loss
  classification boilerplate.
- Sonos media, group, and shortcut command actions now use shared
  `SonosServiceClient.command()` behavior where they do not need zone views.
  Ordinary Sonos action code no longer imports service-use-loss helper
  functions.
- Existing Sonos provider-side subscription cleanup tests pass without provider
  protocol changes.
- `deckr/docs/usage.md` now documents managed subscriptions and shared command
  pools, and demotes direct service-use-loss helper usage to low-level
  infrastructure guidance.

Known remaining work against this plan:

- OpenHAB has not yet been migrated to the shared item subscription manager.
- Kaj status bar has not yet been migrated to the new Sonos message session.
- Command pooling is implemented, but the explicit one-shot fallback policy for
  scopes that should not be pooled still needs to be formalized.

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
- Reuse service-use contracts across same-provider logical subscribers where
  possible, especially for long-lived item and zone subscriptions.
- Reduce command-only Concord churn by reusing compatible active service-use
  leases or shared command leases.
- Keep Beacon and Concord semantics strict: no reattaching, no Beacon-as-
  liveness, no reused cancelled contracts, and no parallel lifecycle authority.
- Keep service provider implementations mostly intact. OpenHAB and Sonos
  already support contract-bound subscription sets through `ensureItems` /
  `releaseItems` and `ensureZones` / `releaseZones`.

## Current Problems

Subscription consumers duplicate lifecycle code in several places:

- Kaj status bar owns Sonos retry/release/clear logic directly.
- Sonos volume rotary owns its own zone subscribe loop and lease-loss handling.
- OpenHAB item actions use a mixin that still owns retry, release, multi-watch
  task cancellation, and service-use loss classification.
- Sonos command clients retry after service-use loss, while OpenHAB command
  clients currently do not.

This creates bugs and inconsistent behavior:

- `None` from a fenced service view is sometimes treated as "successor lease
  required", but it can also mean ordinary view absence or deletion under the
  current valid lease.
- Retry delays and logging policies differ across consumers.
- Each mounted action can open its own Concord service-use contract even when
  many actions need the same service, zones, or items.
- Actions must know about `service_unavailable_ends_service_use()` and
  `service_command_reply_ends_service_use()`, which should be service-client
  internals for ordinary feature code.

## Target Consumer API

The author-facing API should preserve the simple shape:

```python
async with sonos.zone_subscription_session(
    SONOS_SERVICE_ID,
    zones=zones,
) as session:
    await session.ensure_zones(dynamic_zones)

    async for message in session.messages:
        if message.state is ServiceSubscriptionState.READY:
            await update_view(message.resource, message.payload)
        else:
            await render_pending_or_unavailable(message.resource, message)
```

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
- Call one `ensureZones` or `ensureItems` with the retained union when the
  shared set changes or a successor contract is negotiated.
- Call `releaseZones` or `releaseItems` only for resources that leave the
  retained union.
- Watch each retained service view once and fan out messages to interested
  logical sessions.
- Emit `RECONNECTING` on lease loss, then negotiate a successor contract and
  re-ensure the current retained union.
- On session exit, unregister only that logical subscriber and shrink the
  retained set if no other subscriber needs the same resource.

Single `ensureZones` / `ensureItems` calls with the retained union are
acceptable. Because current service providers treat ensure as the set for the
subscriber under that contract, the shared manager must send the full retained
union for its provider-level subscriber id, not only newly added resources.

Dynamic resource methods should have explicit local semantics:

- `ensure_zones(zones)` / `ensure_items(items)` adds or retains resources for
  the logical session.
- `drop_zones(zones)` / `drop_items(items)` removes resources from the logical
  session without affecting other sessions.
- Context exit drops all resources owned by the logical session.

The service command names can remain `ensureZones`, `releaseZones`,
`ensureItems`, and `releaseItems` on the wire unless the provider protocols are
renamed deliberately as part of v1 cleanup. The public client method can be
`drop_*` even if the wire operation is `release*`.

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
- Ensure retained resources for the active lease.
- Release resources removed from the retained set.
- Build `ServiceViewRef` for a resource.
- Convert view payload or view absence into a subscription message.
- Optionally expose lease-backed commands allowed by the same subscription
  scope.

## Command-Only Actions

Command-only clients currently open short service-use contracts for each call.
This should remain a fallback, but not the only path.

Command execution should prefer:

1. A compatible active subscription lease when the command is related to an
   already-retained resource, such as Sonos `adjustVolume` for a watched zone.
2. A provider-shared command lease keyed by service id and compatible operation
   set when no subscription lease applies.
3. A one-shot service-use contract when no shared lease exists or the command
   scope is too specific to pool safely.

Shared command leases should still obey Concord semantics:

- Refresh before use.
- Treat lease-loss replies and `ServiceUnavailable` codes as authority loss.
- Cancel/close ended leases and negotiate successors; never reattach.
- Return ordinary `ServiceCommandReplyBody` statuses to feature code without
  exposing Concord details.

This should make OpenHAB command behavior match Sonos command retry behavior
and reduce per-click Concord churn for common actions.

## Plugin-Specific Changes

### Sonos

Rewrite `SonosServiceClient.zone_subscription_session()` to return a logical
session backed by a shared Sonos zone manager.

The Sonos manager should:

- Use one retained union of zones per service id and compatible operation set.
- Include additional operations requested by consumers, such as `adjustVolume`,
  `play`, and `pause`, in the shared lease scope.
- Fan out zone view messages by zone name.
- Use the active shared lease for zone-related commands when possible.
- Keep service-side `ensureZones` / `releaseZones` semantics unless a v1 rename
  is chosen separately.

Expected consumer simplifications:

- Kaj status bar stops managing `lease_usable`, explicit release, and
  successor-loop boilerplate.
- Sonos volume rotary consumes zone messages and calls session/manager commands
  instead of owning its own subscription loop.
- Sonos media and group command actions can reuse shared command behavior where
  they do not need zone views.

### OpenHAB

Rewrite `OpenHABServiceClient.item_subscription_session()` and the item watcher
mixin around a shared OpenHAB item manager.

The OpenHAB manager should:

- Use one retained union of items per service id.
- Fan out item view messages by item name.
- Convert missing item views into `UNAVAILABLE`, not service-use loss.
- Hide ensure/release replies and lease-loss retries from actions.
- Keep action callbacks focused on item state changes or subscription-state
  messages.

Expected consumer simplifications:

- `OpenHABItemWatcherMixin` becomes a thin adapter over logical sessions, or is
  replaced by direct session usage in actions.
- Item actions no longer import service-use-loss helper functions.
- OpenHAB command calls gain the same successor retry behavior as Sonos.

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
the manager reveals a concrete bug in `ensure*` / `release*` behavior.

## Failure Modes

The manager should provide consistent behavior for these cases:

- Discovery pending: logical sessions emit `PENDING` until a usable descriptor is
  available.
- Service backend unavailable: logical sessions emit `UNAVAILABLE` with service
  diagnostics.
- Ensure command reports ordinary unavailable/rejected: affected resources emit
  `UNAVAILABLE` or `ERROR`, depending on service error code.
- Ensure command or view watch reports service-use loss: all retained resources
  on that lease emit `RECONNECTING`; the manager negotiates a successor and
  re-ensures the retained union.
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
2. Sonos client rewrite using the core manager, including volume rotary and Kaj
   status bar migration.
3. OpenHAB client rewrite using the core manager, including item watcher/action
   migration.
4. Command lease reuse and command-only cleanup for Sonos, OpenHAB, and Kaj
   callers.
5. Documentation and examples update after implementation, replacing old
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
- Lease loss triggers successor negotiation and re-ensures the retained union.
- View absence under a valid lease emits `UNAVAILABLE`, not `RECONNECTING`.
- Command retry uses successor leases for service-use loss and does not retry
  ordinary service errors.

Sonos tests:

- Two zone consumers for the same zone result in one shared `ensureZones` set.
- Adding/removing zones updates the retained union and calls `releaseZones` only
  when the last logical subscriber drops a zone.
- Volume rotary uses the active shared lease for `adjustVolume`.
- Kaj status bar no longer clears media state on service-use reconnect, but does
  mark/clear appropriately on ordinary unavailable messages.
- Existing service provider tests for contract cleanup still pass unchanged.

OpenHAB tests:

- Multiple item actions share one retained `ensureItems` set.
- Missing item view emits `UNAVAILABLE` to the item subscriber.
- Item watcher/action code no longer imports or directly calls service-use-loss
  helper functions.
- OpenHAB command calls retry on service-use loss consistently with Sonos.
- Existing provider-side subscription cleanup behavior still passes.

Integration-style tests:

- Simulate service restart: old contract becomes invalid, managers emit
  `RECONNECTING`, negotiate a successor, re-ensure retained resources, and emit
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
- import `service_command_reply_ends_service_use`
- track `lease_usable`
- manually sleep for successor leases
- decide whether a release command is safe after lease loss
- treat `None` payloads as a proxy for lifecycle state
