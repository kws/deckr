import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

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
  validateAdvertisementRecord(json("fixtures/valid/beacon/hardware-advertisement.v1.json"));
  validateContractRecord(json("fixtures/valid/concord/hardware-claim-contract.v1.json"));
  validateParticipantTokenRecord(json("fixtures/valid/concord/hardware-claim-token.v1.json"));

  assert.throws(() =>
    validateAdvertisementRecord(json("fixtures/invalid/beacon/advertisement-missing-session.v1.json")),
  );
  assert.throws(() =>
    validateParticipantTokenRecord(json("fixtures/invalid/concord/token-missing-participant.v1.json")),
  );
});
