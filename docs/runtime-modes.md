# Deckr Runtime Modes

Deckr runtime modes are ordinary composition over the same primitives:

- `Deckr` owns lane contracts and lane buses.
- `resolve_component_host_plan(...)` resolves discovered or supplied component
  definitions into exact-prefix component instances.
- `start_components(deckr, plan)` hosts those instances against the existing
  runtime.

There are no role-specific runtimes, discovery systems, or hidden lane binding
rules.

## Full Stack Runtime

A full-stack process creates `Deckr`, then starts controller, plugin host,
hardware manager, and any configured lane substrate or adapter components from
one component host plan.

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
its configuration only includes a plugin host component and the lane substrate
needed to reach the controller domain. Distributed runtimes use the NATS
substrate; the old WebSocket/MQTT lane transport examples have been removed.

## Remote Hardware Manager Runtime

A remote hardware manager runtime likewise uses the same APIs, but includes one
or more hardware manager components plus the lane substrate needed for
`hardware_messages`. It does not need a local controller or plugin host.

## Embedded Or Manual Runtime

Embedded applications may create `Deckr` directly and use lane messaging without
component discovery. If they want component lifecycle supervision, they can pass
manual component definitions to `resolve_component_host_plan(...)` or construct a
`ComponentHostPlan.from_specs(...)`.
