# Migration Guide

> Alpha rewrite guide: Deckr intentionally removes incorrect API shapes instead
> of carrying compatibility shims. Use this guide to rewrite components and
> runtime integrations onto the current architecture. Do not preserve old and
> new paths side by side.

This guide is for projects that previously used Deckr's older lane endpoint,
state-store, discovery, inventory, or role-specific runtime shapes.

The current model is:

- `Deckr` owns lane contracts, the message bus, endpoint sessions, Beacon, and
  Concord.
- `ComponentHost` resolves component instances, lanes, endpoint slots, and
  duplicate endpoint ids.
- `ComponentContext` exposes component-scoped access to lanes, declared endpoint
  slots, Beacon, Concord, and explicit KV buckets.
- Components open endpoint sessions from declared slots with
  `context.open_endpoint(...)`.
- Beacon is discovery only. Concord is live agreement authority.

## Component Hosting

Rewrite components to use the generic component instance shape:

```toml
[deckr.components.instances.worker_main]
component = "com.example.worker"
instance_id = "main"

[deckr.components.instances.worker_main.endpoints]
service = "worker-main"
```

Declare endpoint slots in the component manifest. The slot name is the endpoint
family the component is allowed to open.

```python
component = ComponentDefinition(
    manifest=ComponentManifest(
        component_id="com.example.worker",
        consumes=("actions",),
        publishes=("actions",),
        endpoint_slots=("service",),
    ),
    factory=component_factory,
)
```

Do not create role-specific loaders, transport-specific discovery, or hidden
endpoint binding rules. Controllers, hardware managers, action providers, and
services are all components.

## Endpoint Sessions

Replace `Lane.register_endpoint(...)` and any endpoint-presence state with
`ComponentContext.open_endpoint(...)` for hosted components:

```python
class WorkerComponent(BaseComponent):
    def __init__(self, context: ComponentContext) -> None:
        super().__init__(context.runtime_name)
        self._context = context

    async def start(self, ctx: RunContext) -> None:
        ctx.start_task(self._run, ctx)

    async def stop(self) -> None:
        return

    async def _run(self, ctx: RunContext) -> None:
        async with self._context.open_endpoint("service") as endpoint:
            await ctx.report_ready()
            await ctx.stopping.wait()
```

`open_endpoint(...)` accepts only declared, resolved endpoint slots. This keeps
endpoint/session ownership in the core runtime and preserves duplicate endpoint
validation from the component host plan.

For embedded code that is not running as a hosted component, use `Deckr` directly:

```python
async with Deckr() as deckr:
    async with deckr.endpoint("service:worker-main") as endpoint:
        ...
```

Do not reintroduce lane-level endpoint registration. A `Lane` is now only a
message namespace and contract selector.

## Lanes And Messages

Keep lane usage explicit and contract-based:

```python
await endpoint.send(
    lane="actions",
    recipient="controller:main",
    subject=entity_subject("extension", contextId="ctx"),
    message_type="actionExtension",
    body={
        "extensionType": "dev.deckr.example.ping",
        "extensionSchemaId": "dev.deckr.example.ping.v1",
        "data": {},
    },
)
```

The endpoint session stamps `sender` and `senderSessionId`, validates the
message against the lane contract, and applies recipient/session delivery
filtering.

Rewrite code that manually constructs transport subjects, route tables, route
leases, remote endpoint hints, WebSocket lane transports, or MQTT lane
transports. Those are not Deckr lane primitives. Adapter-private protocols may
still exist behind a component boundary, but Deckr lane traffic uses endpoint
sessions and `DeckrMessage` envelopes.

## Beacon And Concord

Replace old discovery services and current-state authorities with the managed
runtime objects:

```python
beacon = context.require_beacon()
concord = context.require_concord()
```

For embedded code:

```python
async with Deckr() as deckr:
    candidates = deckr.beacon.candidates("com.example.feature")
```

Removed shapes include:

- `BeaconDiscovery`
- `BeaconService`
- `ConcordCoordinator`
- `ConcordService`
- discovery inventory state
- endpoint presence state
- unilateral claim or lease state

Beacon answers "who is a candidate?" only. It does not grant ownership or live
authority. Concord contracts and participant tokens are the authority for live
agreements. Missing or withdrawn Beacon advertisements must not invalidate an
existing Concord agreement.

## State And KV

The generic `deckr.state` surface has been removed. Do not open generic state
stores for core protocol data.

Use:

- `context.require_beacon()` for Beacon advertisements
- `context.require_concord()` for Concord agreements and tokens
- `context.kv_bucket(policy)` only for explicit package-owned KV buckets
- service-view helpers for service-owned protected views

Package-owned KV buckets must be owner-qualified, purpose-specific, and
versioned. They must not redefine Beacon, Concord, or Deckr core hardware/action
profile authority.

## Hardware Managers

Python hardware managers should use `deckr.hardware.runtime.HardwareManagerRuntime`.
The manager component owns concrete hardware discovery and command execution;
the shared runtime owns Beacon advertisement, Concord claim participation, input
routing, command authorization, claim reconciliation, and command rejection.

Hosted hardware manager shape:

```python
class HardwareComponent(BaseComponent):
    def __init__(self, context: ComponentContext) -> None:
        super().__init__(context.runtime_name)
        self._context = context
        self._runtime: HardwareManagerRuntime | None = None

    async def start(self, ctx: RunContext) -> None:
        ctx.start_task(self._run, ctx)

    async def stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop()

    async def _run(self, ctx: RunContext) -> None:
        async with self._context.open_endpoint("hardware_manager") as endpoint:
            runtime = HardwareManagerRuntime(
                endpoint=endpoint,
                beacon=self._context.require_beacon(),
                concord=self._context.require_concord(),
                manager_id=endpoint.address.endpoint_id,
                command_handler=handle_command,
                reset_handler=reset_device,
            )
            self._runtime = runtime
            await runtime.start(ctx.tg)
            await ctx.report_ready()
            await ctx.stopping.wait()
            await runtime.stop()
```

Rewrite hardware inventory and traffic as follows:

| Old shape | Current shape |
| --- | --- |
| lane messages such as device available/unavailable | hardware Beacon profile |
| endpoint presence as liveness authority | endpoint sessions only stamp messages |
| unilateral device claim state | `dev.deckr.profile.hardware_claim.v1` Concord contract |
| key/dial/touch-specific message types | `controlInput` targeting a capability |
| set-image/clear/sleep/wake message types | `controlCommand` targeting a capability |

The `hardware_messages` lane carries input, commands, capability state, and
replies. It is not the inventory authority. If a claimed device disappears,
cancel the matching Concord claim or stop maintaining the manager participant
token.

## Actions And Services

Action providers are exposed as Action Runtime services. Provider runtimes
advertise `dev.deckr.action_runtime.provider`, publish the
`action_availability` service view, and exchange lifecycle, binding, page, and
output messages over the `services` lane under service-use Concord contracts.
Existing bindings are controller-owned routing state; continued Beacon presence
is not live authority after a service-use contract exists.

Services should use package-owned feature ids, payload/use profiles, and
explicit service-view buckets. Service messages are ordinary lane
messages when the optional `services` lane is enabled. Protected service-view
authority follows Concord service-use contracts, not continued Beacon presence.

## Test Rewrites

For component tests, prefer testing the hosted path:

```python
plan = resolve_component_host_plan(document, definitions=definitions)
async with mock_deckr(
    lane_contracts=plan.lane_contracts,
    lanes=plan.lane_names,
) as deckr, start_components(deckr, plan):
    ...
```

For focused Concord tests, use the shipped `deckr.testing` surfaces:

```python
from deckr.testing import ConcordRuntimeHarness, MemoryJsonKvBucket

contracts = MemoryJsonKvBucket(bucket="contracts")
tokens = MemoryJsonKvBucket(bucket="tokens", ttl_seconds=120)
harness = ConcordRuntimeHarness(
    contract_store=contracts,
    token_store=tokens,
)

contract = await harness.seed_contract(contract_record)
await harness.seed_token(participant_token_record)
await harness.materialize()

validity = await harness.concord.validate(contract)
assert await harness.contract_entry(contract.key) is not None
```

`contract_record` and `participant_token_record` above are ordinary
`ContractRecord` and `ParticipantTokenRecord` fixtures. Use
`seed_raw_contract(...)` and `seed_raw_token(...)` when testing malformed or
identity-mismatched external JSON. `materialize()` deterministically rebuilds
the cached view without starting a watcher. The harness also exposes exact
entry inspection and deterministic token expiry.

Use `ConcordMaintenanceHarness` only for reaper and maintenance tests that must
inspect all three stores:

```python
from deckr.testing import ConcordMaintenanceHarness

maintenance = ConcordMaintenanceHarness()
concord = maintenance.concord
contract_store = maintenance.contract_store
token_store = maintenance.token_store
maintenance_store = maintenance.maintenance_store
```

Do not call `Concord(contract_store, token_store, maintenance_store)` directly
in workspace tests, and do not recreate that constructor in a local helper.
`ConcordRuntimeHarness` intentionally hides its temporary maintenance store;
only `ConcordMaintenanceHarness` exposes one. This testing boundary does not add
a production `ConcordMaintenance` API: the production maintenance capability
remains deferred, and the existing reaper wiring remains in place meanwhile.

Mocks should sit at the `MessageBus` and explicit KV-bucket boundaries. Avoid
test helpers that recreate removed runtime surfaces such as `deckr.state` or
`Lane.register_endpoint(...)`.

Useful assertions:

- `Lane` does not expose `register_endpoint`.
- hosted components can open only declared endpoint slots.
- `context.require_beacon()` and `context.require_concord()` return the runtime
  protocol objects.
- endpoint metadata includes component identity and endpoint slot.
- old hardware inventory and command message names are absent.

## Validation

After a rewrite, run the relevant checks:

```bash
uv run ruff check .
uv run lint-imports
uv run pytest
```

For language-neutral implementors, also validate against `contract/v1` schemas,
fixtures, and vectors. Passing JSON Schema validation alone is not enough; the
semantic rules in `docs/beacon-concord.md`, `docs/nats-bus.md`, and
`docs/runtime-architecture.md` are part of the contract.
