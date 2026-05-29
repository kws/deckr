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
acting under live authority.

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
| `dev.deckr.profile.action_binding.v1` | Concord | live controller/provider action bindings |

For `dev.deckr.profile.hardware.v1`, the Beacon payload `sessionId` must match
the advertisement `sessionId`, `managerEndpoint` must match the advertisement
`endpoint`, and `managerId` must be the hardware-manager endpoint id.

For `dev.deckr.profile.actions.v1`, the Beacon payload `sessionId` must match
the advertisement `sessionId`, `providerEndpoint` must match the advertisement
`endpoint`, and `providerInstanceId` must be the action-provider endpoint id.

For `dev.deckr.profile.hardware_claim.v1`, the claim terms bind a controller
endpoint, hardware-manager endpoint, manager advertisement id, and one or more
claimed devices. Device ids in one claim must be unique. `instanceCount` must
be greater than zero. Hardware single-owner and capacity enforcement are
hardware-manager/profile policy over valid Concord claims.

For `dev.deckr.profile.action_binding.v1`, the binding terms bind a controller,
action provider, hardware claim, device/control reference, action identity, and
matched capabilities. A binding is live only while the matching Concord contract
is valid and the referenced provider advertisement/session remains acceptable to
controller policy.

Service packages may use generic Beacon and Concord with package-owned feature
ids, advertisement payload profiles, terms profiles, and private state. Deckr
core does not define service-specific domain semantics such as Sonos zones,
OpenHAB items, or package-owned view schemas.

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
