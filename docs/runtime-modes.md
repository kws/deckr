# Deckr Runtime Modes

> Live implementation reference: this document describes behavior currently
> implemented in `deckr`. It should stay in sync with code, tests, and generated
> schemas. If it differs from the implementation, treat that as a bug: either
> update the document to match current behavior or make an intentional
> code/schema/test change to match the intended v1 contract.

Deckr runtime modes are ordinary composition over the same primitives:

- `Deckr` owns lane contracts and lane buses.
- `resolve_component_host_plan(...)` resolves discovered or supplied component
  definitions plus explicit generic instance config into component instances.
- `build_runtime_substrate(...)` resolves the runtime substrate, currently NATS,
  from host configuration. The NATS broker may be external or supervised as a
  local child process, but the Deckr substrate kind and lane/current-state
  contract are the same.
- `start_components(deckr, plan)` hosts those instances against the existing
  runtime.

There are no role-specific runtimes, discovery systems, or hidden lane binding
rules.

The examples below use the public identifier rules from
[`namespaces.md`](namespaces.md): official Deckr component, source, provider,
and action ids use `dev.deckr.*`, while deployment-local endpoint ids remain
short configured addresses.

## Full Stack Runtime

A full-stack process creates `Deckr` with the configured runtime message bus, then
starts controller, action provider runtime, hardware manager, and any adapter
components from one component host plan. The NATS message bus itself is runtime
infrastructure, not a discovered component.

```python
from deckr.components import resolve_component_host_plan, start_components
from deckr.core.config import load_config_document
from deckr.launcher import build_runtime_substrate
from deckr.runtime import Deckr

document = load_config_document(None)
plan = resolve_component_host_plan(document)
substrate = build_runtime_substrate(document, lane_contracts=plan.lane_contracts)

async with Deckr(
    lane_contracts=plan.lane_contracts,
    lanes=plan.lane_names,
    substrate=substrate,
) as deckr:
    async with start_components(deckr, plan):
        ...
```

Launcher configuration for an external broker:

```toml
[deckr.runtime.substrate]
kind = "nats"
url = "nats://127.0.0.1:4222"
```

Launcher configuration for a supervised local broker:

```toml
[deckr.runtime.substrate]
kind = "nats"
supervised = true
```

Component instances use the same generic shape in every mode. Production
deployments can configure one action provider runtime instance explicitly:

```toml
[deckr.components.instances.clock_actions]
component = "dev.deckr.action_provider_runtime.python"
instance_id = "clock-main"

[deckr.components.instances.clock_actions.endpoints]
service = "action-runtime.python-dev.deckr.clock"

[deckr.components.instances.clock_actions.config]
provider_instance_id = "python-dev.deckr.clock"
provider_id = "dev.deckr.clock"
entrypoint = "deckr.plugins.clock"
```

Local development can use an explicitly configured component instance source to
activate installed Python action providers without adding Python-provider logic
to the launcher:

```toml
[[deckr.components.instance_sources]]
id = "python_actions"
source = "dev.deckr.action_provider_runtime.python.installed_providers"
allow = ["dev.deckr.clock", "dev.deckr.sonos", "dev.deckr.openhab", "com.k-si.deckr.kaj"]
```

That source expands the selected `deckr.plugins` entry points into ordinary
`dev.deckr.action_provider_runtime.python` component instances. With the
default templates, provider id `dev.deckr.clock` becomes instance
`dev.deckr.clock-main` with endpoint
`action_provider:python-dev.deckr.clock`.

When `supervised = true`, the launcher starts `nats-server` as a private child
process, enables JetStream, binds to localhost, lets NATS select the client port,
and wires `Deckr` to that selected URL. A configured absolute binary path can be
used when the host already installs `nats-server`:

```toml
[deckr.runtime.substrate]
kind = "nats"
supervised = true
server_path = "/usr/local/bin/nats-server"
```

## Skinny Action Provider Runtime

A skinny action provider runtime uses the same `Deckr` and component host APIs,
but its configuration only includes an action provider runtime component and the
message bus needed to reach the controller domain. Distributed runtimes use
the NATS substrate; the old WebSocket/MQTT lane transport examples have been
removed.

## Remote Hardware Manager Runtime

A remote hardware manager runtime likewise uses the same APIs, but includes one
or more hardware manager components plus the message bus needed for
`hardware_messages`. It does not need a local controller or action provider
runtime.

## Embedded Or Manual Runtime

Embedded applications may create `Deckr` directly and use lane messaging without
component discovery. If they want component lifecycle supervision, they can pass
manual component definitions to `resolve_component_host_plan(...)` or construct a
`ComponentHostPlan.from_specs(...)`. Installed component definitions remain
passive until explicit instance config or an explicit instance source creates a
planned instance.

Embedded applications can use the same external NATS substrate directly:

```python
from deckr.runtime import Deckr
from deckr.substrates.nats import NatsSubstrate

async with Deckr(
    message_bus=NatsSubstrate(url="nats://127.0.0.1:4222", lane_contracts=...),
) as deckr:
    ...
```

They can also supervise a private local `nats-server` process:

```python
from deckr.runtime import Deckr
from deckr.substrates.supervised_nats import SupervisedNatsSubstrate

message_bus = SupervisedNatsSubstrate(lane_contracts=...)

async with Deckr(message_bus=message_bus) as deckr:
    ...
```

The supervised form still delegates lane traffic and explicit KV buckets to
`NatsSubstrate` after startup. Unit tests may use mocks at the `MessageBus` and
KV-bucket boundaries, but supported runtime modes remain real NATS; there is no
separate in-memory lane bus runtime.
