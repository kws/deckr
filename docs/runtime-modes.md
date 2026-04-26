# Deckr Runtime Modes

Deckr runtime modes are ordinary composition over the same primitives:

- `Deckr` owns lane contracts, one shared route table, lane buses, and route
  lease expiry.
- `resolve_component_host_plan(...)` resolves discovered or supplied component
  definitions into exact-prefix component instances.
- `start_components(deckr, plan)` hosts those instances against the existing
  runtime.

There are no role-specific runtimes, discovery systems, or hidden lane binding
rules.

## Full Stack Runtime

A full-stack process creates `Deckr`, then starts controller, plugin host,
hardware driver, and transport components from one component host plan.

```python
from deckr.components import resolve_component_host_plan, start_components
from deckr.core.config import load_config_document
from deckr.runtime import Deckr

document = load_config_document(None)
plan = resolve_component_host_plan(document)

async with Deckr(lane_contracts=plan.lane_contracts, lanes=plan.lane_names) as deckr:
    async with start_components(deckr, plan):
        ...
```

## Skinny Plugin Host Runtime

A skinny plugin host runtime uses the same `Deckr` and component host APIs, but
its configuration only includes a plugin host component and the transports needed
to reach the controller domain. The transport bindings still name lanes
explicitly, for example `plugin_messages`.

## Remote Driver Runtime

A remote driver runtime likewise uses the same APIs, but includes a driver or
hardware manager component plus explicit transport bindings for the hardware
lane. It does not need a local controller or plugin host.

## Embedded Or Manual Runtime

Embedded applications may create `Deckr` directly and use lane messaging without
component discovery. If they want component lifecycle supervision, they can pass
manual component definitions to `resolve_component_host_plan(...)` or construct a
`ComponentHostPlan.from_specs(...)`.
