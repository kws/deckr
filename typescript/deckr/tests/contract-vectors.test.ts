import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import {
  ACTION_LIFECYCLE_REJECTED,
  contextSubject,
  parseSettingsTargetKey,
  settingsTargetKey,
  validateActionInstanceMetadata,
  validateActionLifecycleRejectedBody,
  validateSettingsTargetRef,
} from "../src/actions.ts";
import {
  beaconAdvertisementKey,
  parseBeaconAdvertisementKey,
  validateAdvertisementRecord,
} from "../src/beacon.ts";
import {
  canonicalJson,
  canonicalJsonHash,
  concordContractKey,
  concordParticipantTokenKey,
  parseConcordContractKey,
  parseConcordParticipantTokenKey,
  validateContractRecord,
  validateParticipantTokenRecord,
} from "../src/concord.ts";
import { decodeKeyToken, encodeKeyToken } from "../src/keys.ts";
import {
  headersFor,
  subjectFor,
  validateDeckrMessage,
} from "../src/lanes.ts";
import { validateServiceAdvertisementPayload } from "../src/services.ts";

const CONTRACT_ROOT = join(import.meta.dirname, "../../../contract/v1");

function json(path: string): any {
  return JSON.parse(readFileSync(join(CONTRACT_ROOT, path), "utf8"));
}

test("key token vectors match contract artifacts", () => {
  const vectors = json("vectors/key-tokens.v1.json");
  for (const item of vectors.cases) {
    assert.equal(encodeKeyToken(item.raw), item.token);
    assert.equal(decodeKeyToken(item.token), item.raw);
  }
});

test("Beacon and Concord key vectors match contract artifacts", () => {
  const vectors = json("vectors/beacon-concord-keys.v1.json");
  for (const item of vectors.cases) {
    let actual: string;
    switch (item.helper) {
      case "beacon_advertisement_key":
        actual = beaconAdvertisementKey({
          featureId: item.input.featureId,
          advertisementId: item.input.advertisementId,
        });
        assert.deepEqual(parseBeaconAdvertisementKey(actual), [
          item.input.featureId,
          item.input.advertisementId,
        ]);
        break;
      case "concord_contract_key":
        actual = concordContractKey({
          contractId: item.input.contractId,
          generation: item.input.generation,
        });
        assert.deepEqual(parseConcordContractKey(actual), [
          item.input.contractId,
          item.input.generation,
        ]);
        break;
      case "concord_participant_token_key":
        actual = concordParticipantTokenKey({
          contractId: item.input.contractId,
          generation: item.input.generation,
          participant: item.input.participant,
        });
        assert.deepEqual(parseConcordParticipantTokenKey(actual), [
          item.input.contractId,
          item.input.generation,
          item.input.participant,
        ]);
        break;
      default:
        throw new Error(`unknown helper ${item.helper}`);
    }
    assert.equal(actual, item.key);
  }
});

test("Concord terms hash vectors match contract artifacts", () => {
  const vectors = json("vectors/concord-terms-hash.v1.json");
  for (const item of vectors.cases) {
    const value = JSON.parse(item.canonicalJson);
    assert.equal(canonicalJson(value), item.canonicalJson);
    assert.equal(canonicalJsonHash(value), item.hash);
  }
});

test("NATS lane vectors match contract artifacts", () => {
  const vectors = json("vectors/nats-lane.v1.json");
  for (const item of vectors.cases) {
    const message = validateDeckrMessage(json(item.fixture));
    assert.equal(subjectFor(message), item.subject);
    assert.deepEqual(headersFor(message), item.headers);
  }
});

test("valid contract fixtures parse and invalid fixtures fail", () => {
  const actionRuntimeAdvertisement = validateAdvertisementRecord(
    json("fixtures/valid/beacon/action-runtime-advertisement.v1.json"),
  );
  validateAdvertisementRecord(json("fixtures/valid/beacon/hardware-advertisement.v1.json"));
  validateContractRecord(json("fixtures/valid/concord/hardware-claim-contract.v1.json"));
  validateParticipantTokenRecord(json("fixtures/valid/concord/hardware-claim-token.v1.json"));
  const actionRuntimePayload = validateServiceAdvertisementPayload(
    actionRuntimeAdvertisement.payload,
  );
  assert.equal(actionRuntimePayload.serviceId, "action-runtime.clock-main");

  assert.throws(() =>
    validateAdvertisementRecord(json("fixtures/invalid/beacon/advertisement-missing-session.v1.json")),
  );
  assert.throws(() =>
    validateParticipantTokenRecord(json("fixtures/invalid/concord/token-missing-participant.v1.json")),
  );
});

test("action instance metadata requires contextId", () => {
  assert.deepEqual(
    validateActionInstanceMetadata({
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      actionId: "dev.deckr.clock.show_time",
      actionInstanceId: "action-instance-1",
      configId: "config-1",
      contextId: "ctx-1",
    }),
    {
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      actionId: "dev.deckr.clock.show_time",
      actionInstanceId: "action-instance-1",
      configId: "config-1",
      contextId: "ctx-1",
    },
  );

  assert.throws(() =>
    validateActionInstanceMetadata({
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      actionId: "dev.deckr.clock.show_time",
      actionInstanceId: "action-instance-1",
      configId: "config-1",
    }),
  );
});

test("action lifecycle rejection body validates exact target and reason enum", () => {
  const actionInstance = validateActionInstanceMetadata({
    providerInstanceId: "clock-main",
    providerId: "dev.deckr.clock",
    actionId: "dev.deckr.clock.show_time",
    actionInstanceId: "action-instance-1",
    configId: "config-1",
    contextId: "ctx-1",
  });

  assert.equal(ACTION_LIFECYCLE_REJECTED, "actionLifecycleRejected");
  assert.deepEqual(
    validateActionLifecycleRejectedBody({
      targetKind: "action_instance",
      actionInstance,
      reason: "action_not_available",
      details: { actionId: "dev.deckr.clock.show_time" },
    }),
    {
      targetKind: "action_instance",
      actionInstance,
      reason: "action_not_available",
      retryable: false,
      details: { actionId: "dev.deckr.clock.show_time" },
    },
  );
  assert.throws(() =>
    validateActionLifecycleRejectedBody({
      targetKind: "binding",
      actionInstance,
      reason: "action_not_available",
    }),
  );
  assert.throws(() =>
    validateActionLifecycleRejectedBody({
      targetKind: "action_instance",
      actionInstance,
      reason: "binding_closed",
    }),
  );
});

test("settings target keys round trip with encoded tokens", () => {
  const actionTarget = validateSettingsTargetRef({
    scope: "action_instance",
    controllerId: "controller-main",
    configId: "deck.1",
    providerInstanceId: "elgato.com.example.plugin",
    providerId: "com.example.plugin",
    actionId: "com.example.action",
    actionInstanceId: "instance:1",
    stableId: "living room",
  });
  const providerTarget = validateSettingsTargetRef({
    scope: "action_provider_instance",
    controllerId: "controller-main",
    configId: "deck.1",
    providerInstanceId: "elgato.com.example.plugin",
    providerId: "com.example.plugin",
  });

  assert.equal(
    settingsTargetKey(actionTarget),
    "settings.target.action_instance.controller-main.b64_ZGVjay4x.b64_ZWxnYXRvLmNvbS5leGFtcGxlLnBsdWdpbg.b64_Y29tLmV4YW1wbGUucGx1Z2lu.b64_Y29tLmV4YW1wbGUuYWN0aW9u.b64_aW5zdGFuY2U6MQ.1.b64_bGl2aW5nIHJvb20",
  );
  assert.deepEqual(parseSettingsTargetKey(settingsTargetKey(actionTarget)), actionTarget);
  assert.deepEqual(parseSettingsTargetKey(settingsTargetKey(providerTarget)), providerTarget);
  assert.equal(parseSettingsTargetKey("settings.target.action_instance.bad"), null);
});

test("context subject carries explicit lifecycle identifiers", () => {
  assert.deepEqual(
    contextSubject("ctx-1", {
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      configId: "config-1",
      actionInstanceId: "action-instance-1",
      bindingId: "binding-1",
      pageSessionId: "page-session-1",
    }),
    {
      kind: "context",
      identifiers: {
        contextId: "ctx-1",
        providerInstanceId: "clock-main",
        providerId: "dev.deckr.clock",
        configId: "config-1",
        actionInstanceId: "action-instance-1",
        bindingId: "binding-1",
        pageSessionId: "page-session-1",
      },
    },
  );
});
