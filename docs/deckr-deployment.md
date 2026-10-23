# Deckr deployment guide

These are deployment patterns, not separate wire protocols or bundled runtimes.
Every implementation uses the same schemas and authority rules.

The [Deckr architecture proposal for v1](deckr-architecture.md) gives an optional
baseline for component boundaries and lifecycle ownership. It is informative,
not normative.

The [project repository map](../README.md#project-repositories) lists the
reference, language cores, and integration harness. Choose a core for each host
language and supply the application components described below. Core package
boundaries do not require separate processes. The integration harness's Compose
stack runs test applications; it is not an installation of the controller,
hardware drivers, or Action Runtime applications.

| Deployment | Responsibilities |
| --- | --- |
| Full installation | Host controllers, hardware managers and services; supply NATS and the shared protocol stores. |
| Remote action runtime | Connect to the shared broker, advertise an Action Runtime service, maintain Concord participation and publish protected availability. |
| Remote hardware manager | Advertise inventory, maintain hardware claims, and send/receive the hardware lane. |
| Embedded application | Supply the same protocol and transport lifecycle within another host process. |

Resolve required lanes/stores before starting dependent participants. Share
infrastructure where appropriate; avoid one redundant broker consumer per logical
handler. Keep task ownership, bounded cleanup and recovery explicit. Implementors
choose their language APIs, configuration, dependency injection and process setup.

Beacon records are discovery candidates. Concord tokens determine live authority.
Disconnect/restart behavior must satisfy the [Concord specification](concord.md)
and [Deckr NATS binding recovery requirements](deckr-nats-binding.md). This repository has no launcher,
component entry-point system or installable runtime package.


## Verified restart handoff

A trusted supervisor can supply a local restart handoff to a configured runtime
instance. The [handoff schema](../contract/v1/schemas/deckr/runtime/restart-handoff.v1.schema.json)
defines its structure. This is local host input, not a lane message or KV lease.

`instanceId` identifies the configured process instance. `predecessors` retains
each unresolved exited lifetime. Each entry has a `predecessorId` identifying one
exact process lifetime, for example a container ID together with its start time.
Its signed 32-bit `exitCode` records the confirmed exit result. Its `participants` lists the
endpoint and session pairs that the supervisor assigned to, or obtained directly
from, that process. It must not infer this list from endpoint names or Beacon.

The supervisor must wait for confirmed exit before it writes the handoff. It must
write atomically to a channel that application participants cannot change. The
replacement must compare `instanceId` with its trusted local configuration,
validate the complete handoff, and generate fresh session IDs. A missing handoff
means that ordinary bootstrap applies. An invalid or mismatched handoff must fail
startup. Reading a file or validating its schema does not prove process exit.

A host can configure `DECKR_RESTART_HANDOFF` as the absolute path to that trusted
file and `DECKR_INSTANCE_ID` as the expected configured instance. Both settings are
necessary when this file adapter is used. A nonexistent file permits first boot;
a malformed file does not. Hosts that use another supervisor channel must apply
the same checks. Keep the handoff available until cleanup succeeds. A supervisor
must not discard unresolved evidence when another replacement exits.

The managed runtime applies [verified restart cleanup](concord.md#111-verified-process-restart)
before endpoint readiness. Evidence is scoped to the named sessions. It gives no
permission to cancel other sessions at the same endpoint. Old evidence cannot
cancel a successor with a fresh session. Deployment without this supervisor
integration continues to use the ordinary token-expiry recovery path.
