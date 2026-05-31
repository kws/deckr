# Beacon And Concord

> Normative v1 implementor guide: this document defines the language-neutral
> Beacon and Concord protocol semantics implemented by `deckr`. If this guide,
> the generated `contract/v1` artifacts, and the implementation disagree, treat
> that as a contract bug and fix the docs, artifacts, or implementation together.

Beacon and Concord are Deckr's shared discovery and agreement protocols:

- Beacon provides weak feature discovery through temporary advertisements.
- Concord provides live multi-participant agreements through contracts and
  participant tokens.

Beacon finds candidates. Concord binds participants.

## Sources Of Truth

Use these references together:

- this guide: protocol semantics and conformance rules
- [`nats-bus.md`](nats-bus.md): NATS subjects, KV buckets, store policies, and
  broker operations
- [`../contract/v1`](../contract/v1): language-neutral schemas, fixtures, and
  acceptance vectors
- Python models in `deckr.beacon`, `deckr.concord`,
  `deckr.hardware.profiles`, and `deckr.profiles`: current reference
  implementation and source for generated artifacts

The JSON Schemas are structural wire contracts. They intentionally do not carry
every semantic rule enforced by the Python models. Non-Python implementations
must also enforce the semantic rules in this guide, including endpoint parsing,
positive counters and TTLs, canonical participant ordering, terms-hash
validation, and profile identity checks.

Retired current-state authorities are not part of v1. Do not treat endpoint
presence, action catalogs, hardware inventory, unilateral device claims, lease
buckets, or discovery buckets as parallel authority for Beacon or Concord.

## Discovery And Agreement Boundary

Beacon is discovery only. A Beacon advertisement can make an endpoint a
candidate for a new Concord negotiation, but it is not part of any existing
Concord contract's validity after that contract is negotiated.

An advertiser may withdraw or stop refreshing Beacon advertisements when it is
not accepting new Concord negotiations. That affects only future discovery and
negotiation. It does not invalidate, withdraw, cancel, degrade, or pause any
already negotiated Concord contract.

After a Concord contract exists, live authority and withdrawal are Concord
concerns. A participant withdraws by cancelling the contract, stopping its own
participant token, or allowing its participant token to expire. Profile-specific
loss of authority, such as a disconnected claimed device or a stopped service,
must be expressed through Concord cancellation, token loss, or profile-owned
Concord validation failure. Missing, withdrawn, expired, replaced, or changed
Beacon advertisements must not by themselves invalidate an existing Concord
contract or stop live rendering, routing, commands, or view consumption.

## Shared Rules

Endpoint addresses serialize as:

```text
<endpoint-family>:<endpoint-id>
```

The core endpoint families are `controller`, `hardware_manager`,
`action_provider`, and `service`. Endpoint addresses must not contain leading or
trailing whitespace. The id must be non-empty and must not contain `:`. Action
provider endpoint ids must match `[A-Za-z0-9][A-Za-z0-9._-]*` and must not use
reserved provider identities such as `dev.deckr.controller.builtin`.

State keys use the shared key-token encoding:

- safe tokens matching `[A-Za-z0-9][A-Za-z0-9_-]*` stay unchanged, except values
  beginning with `b64_`
- all other tokens are UTF-8 bytes encoded as URL-safe base64 without padding,
  prefixed with `b64_`

Concord terms hashes use canonical JSON:

- dump the JSON value with object keys sorted
- use compact separators with no insignificant whitespace
- preserve Unicode as UTF-8 JSON text
- hash the resulting bytes with SHA-256
- serialize as `sha256:<hex-digest>`

State transitions use compare-and-set when the substrate supports it. Initial
creation uses `create`; refresh and cancellation use revision-guarded `update`;
withdrawal/deletion should use revision-guarded `delete`. Watches are wakeups.
Exact reads and recomputed validity are the authority path.

## Beacon

A Beacon advertisement is a fresh candidate record:

```text
I advertise feature F at endpoint E. You may try me.
```

Beacon does not prove endpoint liveness, health, authorization, resource
availability, ownership, future success, or command safety. Consumers must treat
Beacon results as candidates and establish any required Concord agreement before
acting under live authority. Once that agreement exists, continued Beacon
advertisement presence is not live-use authority.

Beacon advertisements use the schema
`dev.deckr.beacon.advertisement.v1`. The NATS-backed store and key shape are
specified in [`nats-bus.md`](nats-bus.md#beacon-store).

An advertisement record must have:

- `advertisementId`, `featureId`, and `sessionId` as non-empty strings with no
  leading or trailing whitespace
- valid `advertiser` and `endpoint` endpoint addresses
- `refreshSeq > 0`
- `ttlSeconds > 0`
- optional `operations`, `labels`, `hints`, and `payload`

Create an advertisement with a unique advertisement id using `create`. Refresh
it by exact-read, owner-check, incrementing `refreshSeq`, and revision-guarded
`update`. Withdraw it by exact-read, owner-check, and revision-guarded delete.
If the advertisement is no longer refreshed, the TTL-bound store removes it.

A Beacon candidate is usable only if:

- the record exists
- the record validates against the Beacon envelope schema and semantic rules
- the record key and advertisement identity agree
- `featureId` matches the queried feature
- any configured selector accepts the record
- any configured current-session check accepts the advertiser session

Selectors, labels, hints, and capacity fields are policy hints, not authority.
Duplicate or overlapping advertisements are allowed. Consumer policy chooses
which candidate to try.

## Concord

A Concord contract is a temporary runtime agreement between named participants.
It is valid only while the contract is open and every named participant
maintains an acceptable live token for the same contract id, generation,
participant, session, and terms hash. Any named participant may cancel the
contract. Cancellation is terminal.

Concord contracts use schema `dev.deckr.concord.contract.v1`. Concord
participant tokens use schema `dev.deckr.concord.participant-token.v1`. The
NATS-backed stores and key shapes are specified in
[`nats-bus.md`](nats-bus.md#concord-stores).

A contract record must have:

- `contractId` as a non-empty string with no leading or trailing whitespace
- `generation > 0`
- a non-empty, unique, lexicographically sorted `participants` list
- a unique, lexicographically sorted `attachedParticipants` list whose entries
  are a subset of `participants`
- `state` as `open` or `cancelled`
- optional `profile`, `terms`, `termsHash`, diagnostics, and `supersedes`

If `terms` is present, `termsHash` must be present and must equal the canonical
JSON hash of `terms`. If both `profile` and `terms.profile` are present, they
must match. `supersedes` is diagnostic and does not affect validity.

A participant token record must have:

- `contractId`, `sessionId`, and `tokenId` as non-empty strings with no leading
  or trailing whitespace
- `generation > 0`
- a valid `participant` endpoint address
- `refreshSeq > 0`
- `ttlSeconds > 0`
- optional `termsHash`, `contractHash`, and `observed`

Create a candidate contract with `create(contractKey, contractRecord)` and an
empty `attachedParticipants` list. Creating a contract does not make it valid.
A participant attaches by exact-reading the contract, confirming it is open and
names that participant, creating its own participant-token key, then adding
itself to `attachedParticipants` with a revision-guarded contract update. A
participant must not write another participant's token.

Refresh a token by exact-reading the contract and token, confirming both still
match the local handle, incrementing `refreshSeq`, and revision-guarded
`update(tokenKey, tokenRecord)`. If the contract is cancelled, the token is
missing, or the token has changed owner/session/token id/terms hash, the
participant no longer maintains authority for that contract generation.

Once a participant has successfully attached a token for a contract generation,
loss of that token means loss of authority for that generation. The participant
must not silently recreate a token for the same contract generation. It should
cancel when possible or negotiate a successor contract using a new generation or
new contract id.

A missing token for a participant that is not yet in `attachedParticipants`
means the contract is not yet fulfilled. A missing token for a participant that
is already in `attachedParticipants` means authority was lost and the contract is
invalid for that generation.

Validate a contract by exact-reading the contract and every named participant
token. The result is valid only if:

- the contract exists and is `open`
- every named participant is in `attachedParticipants`
- every named participant token exists
- every token names the same `contractId` and `generation`
- every token belongs to the participant whose key is being checked
- every token has the expected `termsHash` when the contract has one
- every token session matches any configured current-session evidence

If no external current-session evidence is available, token existence and
internal consistency are the session evidence. A missing, invalid, stale, or
generation-mismatched token means the contract is not valid. A cancelled
contract is never resumed; recovery uses a successor contract.

## Profiles

Generic Beacon validates only the advertisement envelope. Generic Concord
validates only contract and token mechanics. Profile owners validate payload and
terms meaning.

Deckr core owns these profiles:

| Profile | Protocol | Purpose |
| --- | --- | --- |
| `dev.deckr.profile.hardware.v1` | Beacon | hardware devices, controls, capabilities |
| `dev.deckr.profile.actions.v1` | Beacon | action provider actions and requirements |
| `dev.deckr.profile.hardware_claim.v1` | Concord | controller ownership of hardware devices |
| `dev.deckr.profile.action_provider_session.v1` | Concord | live controller/provider runtime sessions |

For `dev.deckr.profile.hardware.v1`, the Beacon payload `sessionId` must match
the advertisement `sessionId`, `managerEndpoint` must match the advertisement
`endpoint`, and `managerId` must be the hardware-manager endpoint id.

For `dev.deckr.profile.actions.v1`, the Beacon payload `sessionId` must match
the advertisement `sessionId`, `providerEndpoint` must match the advertisement
`endpoint`, and `providerInstanceId` must be the action-provider endpoint id.

For `dev.deckr.profile.hardware_claim.v1`, the claim terms bind a controller
endpoint, hardware-manager endpoint, and one or more claimed devices. Device
ids in one claim must be unique. `instanceCount` must be greater than zero.
Beacon may discover a candidate device before a claim is created, but Beacon is
not part of claim validity. Participants validate ownership through the Concord
contract, participant tokens, endpoint, session, and device refs. Hardware
single-owner and capacity enforcement are hardware-manager/profile policy over
valid Concord claims. If a claimed device disconnects, the hardware manager
must cancel/end the Concord claim or stop maintaining its participant token; a
missing hardware Beacon advertisement alone is not a claim withdrawal.

For `dev.deckr.profile.action_provider_session.v1`, the terms bind a
controller endpoint to one action-provider runtime endpoint and the provider
runtime session advertised through Beacon. Individual control bindings are
controller-owned routing state. Once the controller and provider have negotiated
the provider-session Concord contract, Beacon is no longer part of that
contract's lifecycle or validity. Existing bindings remain usable while the
provider-session contract is valid; Beacon may only discover candidates for new
or successor negotiations.

Service packages may use generic Beacon and Concord with package-owned feature
ids, advertisement payload profiles, terms profiles, and private state. Deckr
core does not define service-specific domain semantics such as Sonos zones,
OpenHAB items, or package-owned view schemas. After a service-use Concord
contract is negotiated, service command and view authority follows that Concord
contract and its participant tokens, not continued Beacon advertisement
presence. A service may withdraw its Beacon advertisement when it cannot accept
new service-use contracts; existing service-use contracts remain governed only
by Concord validity and participant tokens.

## Contract Artifacts And Conformance

`contract/v1` contains:

- JSON Schemas for Beacon, Concord, Deckr profiles, and lane messages
- valid and invalid fixtures
- key-token vectors
- Beacon and Concord key vectors
- canonical Concord terms-hash vectors
- NATS lane subject/header vectors

Non-Python implementations should use those artifacts as acceptance inputs.
Passing schema validation alone is not sufficient; implementations must also
enforce this guide's semantic rules. In particular, verify endpoint parsing,
key-token encoding, Beacon/Concord key generation, terms hashing, positive
counters and TTLs, contract participant canonicalization, and profile identity
cross-checks.
