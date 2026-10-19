# Deployment responsibilities

These are deployment patterns, not separate wire protocols or bundled runtimes.
Every implementation uses the same schemas and authority rules.

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
and [NATS recovery requirements](nats.md). This repository has no launcher,
component entry-point system or installable runtime package.
