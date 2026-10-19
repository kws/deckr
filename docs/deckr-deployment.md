# Deckr deployment guide

These are deployment patterns, not separate wire protocols or bundled runtimes.
Every implementation uses the same schemas and authority rules.

The [Deckr architecture proposal for v1](deckr-architecture.md) gives an optional
baseline for component boundaries and lifecycle ownership. It is informative,
not normative.

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
