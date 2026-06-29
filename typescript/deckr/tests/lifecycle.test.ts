import assert from "node:assert/strict";
import test from "node:test";

import {
  BeaconDiscovery,
  BeaconService,
  BEACON_ADVERTISEMENT_STORE_POLICY,
  CandidateStatus,
} from "../src/beacon.ts";
import {
  CONCORD_CONTRACT_STORE_POLICY,
  CONCORD_TOKEN_STORE_POLICY,
  ConcordCoordinator,
  ConcordReaperService,
  ConcordService,
  ContractValidityStatus,
} from "../src/concord.ts";
import { controllerAddress, serviceAddress } from "../src/endpoint.ts";
import { buildMessage, entitySubject } from "../src/lanes.ts";
import {
  AuthorizationDecision,
  ServiceAdvertiser,
  ServiceBackendStatus,
  ServiceUseAuthorizer,
  parseServiceDescriptor,
  serviceUseTerms,
  serviceViewKey,
  type ServiceProtocol,
} from "../src/services.ts";
import { MemoryStateStore } from "../src/state.ts";

test("Beacon advertises, refreshes, validates, and withdraws candidates", async () => {
  const state = new MemoryStateStore({ policy: BEACON_ADVERTISEMENT_STORE_POLICY });
  const beacon = new BeaconService(new BeaconDiscovery(state));
  const advertisement = await beacon.ensureAdvertisement({
    featureId: "dev.deckr.test.feature",
    endpoint: "service:test-service",
    sessionId: "session-1",
    payload: { ok: true },
  });

  const first = await advertisement.publish();
  assert.equal(first.refreshSeq, 1);
  const second = await advertisement.publish({ hints: { refreshed: true } });
  assert.equal(second.refreshSeq, 2);

  const candidates = await beacon.find("dev.deckr.test.feature");
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]!.advertisement.endpoint, "service:test-service");
  assert.equal(await beacon.validate(candidates[0]!), CandidateStatus.CANDIDATE);

  await advertisement.close();
  assert.equal(await beacon.find("dev.deckr.test.feature").then((items) => items.length), 0);
});

test("Concord validates lifecycle and does not recreate a lost attached token", async () => {
  const contracts = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokens = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const concord = new ConcordService(new ConcordCoordinator(contracts, tokens));

  const contract = await concord.createContract([
    "controller:main",
    "service:music",
  ]);
  let validity = await concord.validate(contract);
  assert.equal(validity.status, ContractValidityStatus.NOT_YET_FULFILLED);

  const controllerLease = concord.participantLease({
    contract,
    participant: "controller:main",
    sessionId: "controller-session",
  });
  const serviceLease = concord.participantLease({
    contract,
    participant: "service:music",
    sessionId: "service-session",
  });
  const controllerToken = await controllerLease.attachOrRefresh();
  await serviceLease.attachOrRefresh();

  validity = await concord.validate(contract);
  assert.equal(validity.status, ContractValidityStatus.VALID);

  await tokens.delete(controllerToken.key, { revision: controllerToken.revision });
  validity = await concord.validate(contract);
  assert.equal(validity.status, ContractValidityStatus.MISSING_TOKEN);
  await assert.rejects(() => controllerLease.attachOrRefresh());
});

test("Concord reaper records stale contracts and cancels after grace", async () => {
  const contracts = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokens = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const maintenance = new MemoryStateStore();
  const concord = new ConcordService(new ConcordCoordinator(contracts, tokens));
  let now = "2026-04-29T10:00:00.000Z";
  const reaper = new ConcordReaperService(concord, {
    contractState: contracts,
    tokenState: tokens,
    maintenanceState: maintenance,
    staleGraceSeconds: 10,
    clock: () => now,
  });
  await concord.createContract(["controller:main", "service:music"], {
    contractId: "stale-service-use",
    profile: "dev.deckr.test.service_use.v1",
  });

  let result = await reaper.scanOnce();
  assert.equal(result.staleObservationsCreated, 1);
  assert.equal(result.contractsCancelled, 0);

  now = "2026-04-29T10:00:11.000Z";
  result = await reaper.scanOnce();
  assert.equal(result.contractsCancelled, 1);
});

test("service helpers advertise descriptors and authorize Concord-governed commands", async () => {
  const protocol: ServiceProtocol = {
    namespace: "dev.deckr.test.service",
    featureId: "dev.deckr.test.service",
    advertisementProfile: "dev.deckr.test.service.advertisement.v1",
    useProfile: "dev.deckr.test.service_use.v1",
    operations: ["play", "pause"],
    viewFamilies: {
      status: {
        storeName: "dev_deckr_test_service_view_v1",
        keyPrefix: serviceViewKey("music", "status"),
      },
    },
  };
  const beaconState = new MemoryStateStore({ policy: BEACON_ADVERTISEMENT_STORE_POLICY });
  const beacon = new BeaconService(new BeaconDiscovery(beaconState));
  const serviceEndpoint = {
    endpoint: serviceAddress("music"),
    sessionId: "service-session",
  };
  const advertiser = new ServiceAdvertiser({
    protocol,
    serviceId: "music",
    endpoint: serviceEndpoint,
    beacon,
  });
  await advertiser.publish(ServiceBackendStatus.AVAILABLE);
  const candidate = (await beacon.find(protocol.featureId))[0]!;
  const descriptor = parseServiceDescriptor(candidate, protocol);
  assert.notEqual(descriptor, null);

  const contractState = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokenState = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const concord = new ConcordService(new ConcordCoordinator(contractState, tokenState));
  const clientEndpoint = {
    endpoint: controllerAddress("main"),
    sessionId: "controller-session",
  };
  const terms = serviceUseTerms(descriptor!, clientEndpoint.endpoint, {
    operations: ["play"],
    views: ["status"],
  });
  const agreement = await concord.ensureAgreement({
    profile: descriptor!.useProfile,
    participants: [descriptor!.endpoint, clientEndpoint.endpoint],
    localParticipant: clientEndpoint.endpoint,
    localSessionId: clientEndpoint.sessionId,
    terms: terms as any,
    stableContractId: terms.serviceUseId,
    currentSessions: {
      [descriptor!.endpoint]: descriptor!.sessionId,
      [clientEndpoint.endpoint]: clientEndpoint.sessionId,
    },
  });
  assert.equal(agreement.validity.status, ContractValidityStatus.NOT_YET_FULFILLED);

  const authorizer = new ServiceUseAuthorizer({
    protocol,
    serviceId: "music",
    endpoint: serviceEndpoint,
    concord,
  });
  await authorizer.reconcileContracts();

  const message = buildMessage({
    lane: "services",
    sender: clientEndpoint.endpoint,
    senderSessionId: clientEndpoint.sessionId,
    recipient: descriptor!.endpoint,
    recipientSessionId: descriptor!.sessionId,
    messageType: "serviceCommand",
    subject: entitySubject("service", {
      serviceId: "music",
      namespace: protocol.namespace,
      operation: "play",
    }),
    body: { serviceNamespace: protocol.namespace, operation: "play", params: {} },
  });

  assert.equal(
    await authorizer.authorizeCommand(message, {
      serviceNamespace: protocol.namespace,
      operation: "play",
      params: {},
    }),
    AuthorizationDecision.AUTHORIZED,
  );

  const mismatchedTerms = serviceUseTerms(descriptor!, clientEndpoint.endpoint, {
    operations: ["pause"],
  });
  await concord.ensureAgreement({
    profile: descriptor!.useProfile,
    participants: [descriptor!.endpoint, clientEndpoint.endpoint],
    localParticipant: clientEndpoint.endpoint,
    localSessionId: clientEndpoint.sessionId,
    terms: mismatchedTerms as any,
    stableContractId: "service-use:wrong",
    currentSessions: {
      [descriptor!.endpoint]: descriptor!.sessionId,
      [clientEndpoint.endpoint]: clientEndpoint.sessionId,
    },
  });

  const pauseMessage = buildMessage({
    lane: "services",
    sender: clientEndpoint.endpoint,
    senderSessionId: clientEndpoint.sessionId,
    recipient: descriptor!.endpoint,
    recipientSessionId: descriptor!.sessionId,
    messageType: "serviceCommand",
    subject: entitySubject("service", {
      serviceId: "music",
      namespace: protocol.namespace,
      operation: "pause",
    }),
    body: {
      serviceNamespace: protocol.namespace,
      operation: "pause",
      params: {},
    },
  });

  assert.equal(
    await authorizer.authorizeCommand(
      pauseMessage,
      {
        serviceNamespace: protocol.namespace,
        operation: "pause",
        params: {},
      },
    ),
    AuthorizationDecision.DENIED,
  );

  const otherClientEndpoint = {
    endpoint: controllerAddress("other"),
    sessionId: "other-controller-session",
  };
  await concord.ensureAgreement({
    profile: descriptor!.useProfile,
    participants: [descriptor!.endpoint, otherClientEndpoint.endpoint],
    localParticipant: otherClientEndpoint.endpoint,
    localSessionId: otherClientEndpoint.sessionId,
    terms: mismatchedTerms as any,
    stableContractId: mismatchedTerms.serviceUseId,
    currentSessions: {
      [descriptor!.endpoint]: descriptor!.sessionId,
      [otherClientEndpoint.endpoint]: otherClientEndpoint.sessionId,
    },
  });

  assert.equal(
    await authorizer.authorizeCommand(pauseMessage, {
      serviceNamespace: protocol.namespace,
      operation: "pause",
      params: {},
    }),
    AuthorizationDecision.DENIED,
  );
});
