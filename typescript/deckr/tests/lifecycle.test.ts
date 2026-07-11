import assert from "node:assert/strict";
import test from "node:test";

import {
  BeaconDiscovery,
  BeaconService,
  BEACON_ADVERTISEMENT_STORE_POLICY,
  CandidateStatus,
  DEFAULT_BEACON_TTL_SECONDS,
} from "../src/beacon.ts";
import {
  canonicalJsonHash,
  CONCORD_CONTRACT_STORE_POLICY,
  CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON,
  CONCORD_TOKEN_STORE_POLICY,
  ConcordCoordinator,
  ConcordReaperService,
  ConcordService,
  ContractState,
  ContractValidityStatus,
  DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
  type ContractPointer,
  type ContractValidity,
  type ParticipantHandle,
} from "../src/concord.ts";
import { controllerAddress, serviceAddress } from "../src/endpoint.ts";
import {
  ServiceUnavailable,
  StateConflict,
  StateUnavailable,
  ValidationError,
} from "../src/errors.ts";
import type { JsonObject, JsonValue } from "../src/json.ts";
import { buildMessage, entitySubject } from "../src/lanes.ts";
import {
  AuthorizationDecision,
  DEFAULT_SERVICE_USE_INDEX_STORE_NAME,
  SERVICE_USE_INDEX_SCHEMA_ID,
  ServiceExchangePattern,
  ServiceAdvertiser,
  ServiceBackendStatus,
  ServiceMessageDirection,
  ServiceMessageIntent,
  ServiceUseAuthorizer,
  ServiceUseLeaseManager,
  ServiceViewReader,
  ServiceViewStoreWriter,
  ServiceViewWriter,
  ManagedServiceViewAccess,
  parseServiceDescriptor,
  serviceUseScopeIndexKey,
  serviceUseTerms,
  serviceViewKey,
  serviceViewStorageKey,
  type RegisteredEndpointLane,
  type ServiceDescriptor,
  type ServiceUseLease,
  type ServiceUseTerms,
  type ServiceProtocol,
  type ServiceViewReadContext,
  type ServiceViewRef,
  type ServiceViewWriteContext,
} from "../src/services.ts";
import {
  MAX_MEMORY_WATCH_CHANGED_KEYS,
  MAX_MEMORY_WATCHERS,
  MemoryStateStore,
} from "../src/state.ts";

const TEST_SERVICE_PROTOCOL: ServiceProtocol = {
  namespace: "dev.deckr.test.service",
  featureId: "dev.deckr.test.service",
  advertisementProfile: "dev.deckr.test.service.advertisement.v1",
  useProfile: "dev.deckr.test.service_use.v1",
  operations: {
    play: {},
    pause: {},
  },
  messages: {
    play: {
      operation: "play",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      direction: ServiceMessageDirection.CONSUMER_TO_SERVICE,
    },
    pause: {
      operation: "pause",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      direction: ServiceMessageDirection.CONSUMER_TO_SERVICE,
    },
  },
  viewFamilies: {
    status: {
      storeName: "dev_deckr_test_service_view_v1",
      keyPrefix: serviceViewKey("music", "status"),
      writer: ServiceViewWriter.SERVICE,
    },
  },
};

function testClientEndpoint(sessionId = "controller-session"): RegisteredEndpointLane {
  return {
    endpoint: controllerAddress("main"),
    sessionId,
  };
}

async function testServiceDescriptor(sessionId = "service-session"): Promise<ServiceDescriptor> {
  const beaconState = new MemoryStateStore({ policy: BEACON_ADVERTISEMENT_STORE_POLICY });
  const beacon = new BeaconService(new BeaconDiscovery(beaconState));
  const advertiser = new ServiceAdvertiser({
    protocol: TEST_SERVICE_PROTOCOL,
    serviceId: "music",
    endpoint: {
      endpoint: serviceAddress("music"),
      sessionId,
    },
    beacon,
  });
  await advertiser.publish(ServiceBackendStatus.AVAILABLE);
  const candidate = (await beacon.find(TEST_SERVICE_PROTOCOL.featureId))[0]!;
  const descriptor = parseServiceDescriptor(candidate, TEST_SERVICE_PROTOCOL);
  if (descriptor === null) {
    throw new Error("test service descriptor did not validate");
  }
  return descriptor;
}

function serviceUseTestRuntime(client = testClientEndpoint()): {
  concord: ConcordService;
  contractState: MemoryStateStore;
  tokenState: MemoryStateStore;
  serviceUseIndex: MemoryStateStore;
  manager: ServiceUseLeaseManager;
  client: RegisteredEndpointLane;
} {
  const contractState = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokenState = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const serviceUseIndex = new MemoryStateStore({
    name: DEFAULT_SERVICE_USE_INDEX_STORE_NAME,
  });
  const concord = new ConcordService(new ConcordCoordinator(contractState, tokenState));
  const manager = new ServiceUseLeaseManager({
    endpoint: client,
    concord,
    serviceUseIndex,
  });
  return { concord, contractState, tokenState, serviceUseIndex, manager, client };
}

async function acquireServiceUseLeaseWithServiceToken(options: {
  concord: ConcordService;
  manager: ServiceUseLeaseManager;
  descriptor: ServiceDescriptor;
  operations?: string[];
  timeoutMs?: number;
}): Promise<ServiceUseLease> {
  const result: { lease?: ServiceUseLease; error?: unknown } = {};
  void options.manager.ensure(options.descriptor, {
    operations: options.operations ?? ["play"],
    timeoutMs: options.timeoutMs ?? 500,
  }).then(
    (lease) => {
      result.lease = lease;
    },
    (error) => {
      result.error = error;
    },
  );

  while (result.lease === undefined && result.error === undefined) {
    const contracts = await options.concord.contracts(options.descriptor.useProfile, {
      participant: options.descriptor.endpoint,
      state: ContractState.OPEN,
    });
    for (const contract of contracts) {
      const record = await options.concord.contractRecord(contract);
      if (record === null || record.attachedParticipants.includes(options.descriptor.endpoint)) {
        continue;
      }
      try {
        await options.concord.attach(
          contract,
          options.descriptor.endpoint,
          options.descriptor.sessionId,
        );
      } catch (error) {
        if (!(error instanceof StateConflict)) {
          throw error;
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  if (result.error !== undefined) {
    throw result.error;
  }
  return result.lease!;
}

function serviceUseIndexRecord(
  terms: ServiceUseTerms,
  pointer: ContractPointer,
  client: RegisteredEndpointLane,
): JsonObject {
  const now = new Date().toISOString();
  return {
    schema: SERVICE_USE_INDEX_SCHEMA_ID,
    scopeId: terms.serviceUseScopeId,
    contract: {
      contractId: pointer.contractId,
      generation: pointer.generation,
    },
    termsHash: canonicalJsonHash(terms as unknown as JsonValue),
    serviceEndpoint: terms.serviceEndpoint,
    serviceSessionId: terms.serviceSessionId,
    clientEndpoint: terms.clientEndpoint,
    clientSessionId: client.sessionId,
    state: "candidate",
    createdAt: now,
    updatedAt: now,
  };
}

const VIEW_CONTRACT: ContractPointer = {
  contractId: "contract:alpha",
  generation: 7,
};

function serviceViewRef(family = "status", token = "deck"): ServiceViewRef {
  return {
    storeName: "dev_deckr_test_service_view_v1",
    key: serviceViewKey("music", family, token),
  };
}

function serviceViewReadContext(
  reader: ServiceViewWriter,
  views = TEST_SERVICE_PROTOCOL.viewFamilies,
  contract: ContractPointer = VIEW_CONTRACT,
): ServiceViewReadContext {
  return {
    reader,
    serviceId: "music",
    serviceNamespace: TEST_SERVICE_PROTOCOL.namespace,
    serviceEndpoint: serviceAddress("music"),
    serviceSessionId: "service-session",
    consumerEndpoint: controllerAddress("main"),
    consumerSessionId: "controller-session",
    contract,
    views,
  };
}

function serviceViewWriteContext(
  writer: ServiceViewWriter,
  views = TEST_SERVICE_PROTOCOL.viewFamilies,
  contract: ContractPointer = VIEW_CONTRACT,
): ServiceViewWriteContext {
  return {
    writer,
    serviceId: "music",
    serviceNamespace: TEST_SERVICE_PROTOCOL.namespace,
    serviceEndpoint: serviceAddress("music"),
    serviceSessionId: "service-session",
    consumerEndpoint: controllerAddress("main"),
    consumerSessionId: "controller-session",
    contract,
    views,
  };
}

function fencedViewPayload(
  view: ServiceViewRef,
  overrides: JsonObject = {},
): JsonObject {
  return {
    status: "playing",
    viewKey: view.key,
    serviceId: "music",
    serviceNamespace: TEST_SERVICE_PROTOCOL.namespace,
    serviceEndpoint: serviceAddress("music"),
    serviceSessionId: "service-session",
    consumerEndpoint: controllerAddress("main"),
    consumerSessionId: "controller-session",
    writer: ServiceViewWriter.SERVICE,
    contractId: VIEW_CONTRACT.contractId,
    generation: VIEW_CONTRACT.generation,
    ...overrides,
  };
}

function token(
  participant: string,
  sessionId: string,
): ParticipantHandle {
  return {
    key: `token.${participant}`,
    contractId: VIEW_CONTRACT.contractId,
    generation: VIEW_CONTRACT.generation,
    participant,
    sessionId,
    tokenId: `${participant}.token`,
    revision: 1,
    refreshSeq: 1,
    ttlSeconds: DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
  };
}

function validViewLease(descriptor: ServiceDescriptor): ServiceUseLease {
  const validity: ContractValidity = {
    status: ContractValidityStatus.VALID,
    valid: true,
    tokens: {
      [controllerAddress("main")]: token(controllerAddress("main"), "controller-session"),
      [serviceAddress("music")]: token(serviceAddress("music"), "service-session"),
    },
  };
  return {
    descriptor,
    terms: serviceUseTerms(descriptor, controllerAddress("main"), {
      views: ["status"],
    }),
    agreement: {
      contract: VIEW_CONTRACT,
      validity,
      localToken: validity.tokens[controllerAddress("main")]!,
      refresh: async () => validity,
    } as ServiceUseLease["agreement"],
  };
}

test("Beacon and Concord use the canonical shared-store TTLs", () => {
  assert.equal(DEFAULT_BEACON_TTL_SECONDS, 300);
  assert.equal(BEACON_ADVERTISEMENT_STORE_POLICY.brokerTtlSeconds, 300);
  assert.equal(DEFAULT_CONCORD_TOKEN_TTL_SECONDS, 120);
  assert.equal(CONCORD_TOKEN_STORE_POLICY.brokerTtlSeconds, 120);
});

test("memory state watches coalesce by key and resnapshot on overflow", async () => {
  const state = new MemoryStateStore();
  const iterator = state.watch()[Symbol.asyncIterator]();

  for (let index = 0; index <= MAX_MEMORY_WATCH_CHANGED_KEYS; index += 1) {
    await state.put(`key.${index}`, { index });
  }

  assert.deepEqual(await iterator.next(), {
    done: false,
    value: { operation: "resnapshot" },
  });
  await iterator.return?.();
});

test("memory state rejects a 257th watcher explicitly", async () => {
  const state = new MemoryStateStore();
  const iterators = Array.from(
    { length: MAX_MEMORY_WATCHERS },
    () => state.watch()[Symbol.asyncIterator](),
  );

  assert.throws(() => state.watch(), StateUnavailable);
  await Promise.all(iterators.map((iterator) => iterator.return?.()));
});

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

test("Concord attach requires exact token id to return an existing token", async () => {
  const contracts = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokens = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const concord = new ConcordService(new ConcordCoordinator(contracts, tokens));
  const coordinator = concord.coordinatorForTesting();
  const contract = await concord.createContract(["controller:main", "service:music"]);

  const first = await coordinator.attach(contract, "controller:main", "controller-session", {
    tokenId: "controller-token",
  });
  const repeated = await coordinator.attach(contract, "controller:main", "controller-session", {
    tokenId: "controller-token",
  });

  assert.equal(repeated.key, first.key);
  assert.equal(repeated.revision, first.revision);
  await assert.rejects(
    () => coordinator.attach(contract, "controller:main", "controller-session"),
    StateConflict,
  );
  await assert.rejects(
    () => coordinator.attach(contract, "controller:main", "controller-session", {
      tokenId: "controller-token-2",
    }),
    StateConflict,
  );
});

test("Concord participant lease cannot adopt without a local token handle", async () => {
  const contracts = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokens = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const concord = new ConcordService(new ConcordCoordinator(contracts, tokens));
  const contract = await concord.createContract(["controller:main", "service:music"]);
  const controllerLease = concord.participantLease({
    contract,
    participant: "controller:main",
    sessionId: "controller-session",
  });
  await controllerLease.attachOrRefresh();
  const serviceLease = concord.participantLease({
    contract,
    participant: "service:music",
    sessionId: "service-session",
  });
  await serviceLease.attachOrRefresh();
  const validity = await concord.validate(contract);
  const blankLease = concord.participantLease({
    contract,
    participant: "service:music",
    sessionId: "service-session",
  });

  assert.equal(validity.status, ContractValidityStatus.VALID);
  await assert.rejects(
    async () => blankLease.adopt(validity.tokens["service:music"]!),
    ValidationError,
  );
});

test("Concord participant manager cancels when same-session token handle is lost", async () => {
  const contracts = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokens = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const concord = new ConcordService(new ConcordCoordinator(contracts, tokens));
  const contract = await concord.createContract(["controller:main", "service:music"]);
  await concord.attach(contract, "controller:main", "controller-session");
  const manager = concord.participantManager({
    participant: "service:music",
    sessionId: "service-session",
    acceptContract: () => true,
  });
  const managed = await manager.reconcile();
  assert.equal(managed.length, 1);
  assert.notEqual(managed[0]!.token, null);
  const token = managed[0]!.token!;
  const restarted = concord.participantManager({
    participant: "service:music",
    sessionId: "service-session",
    acceptContract: () => true,
  });

  assert.deepEqual(await restarted.reconcile(), []);
  const record = await concord.contractRecord(contract);
  assert.equal(record?.state, ContractState.CANCELLED);
  assert.equal(record?.cancelReason, CONCORD_MANAGED_LOST_PARTICIPANT_TOKEN_REASON);
  assert.notEqual(await tokens.get(token.key), null);
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

test("service helpers advertise descriptors and authorize Concord-governed messages", async () => {
  const protocol = TEST_SERVICE_PROTOCOL;
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
    contract: {
      contractId: agreement.contract.contractId,
      generation: agreement.contract.generation,
    },
    messageType: "serviceMessage",
    subject: entitySubject("service", {
      serviceId: "music",
      namespace: protocol.namespace,
      name: "play",
    }),
    body: {
      serviceNamespace: protocol.namespace,
      name: "play",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      params: {},
    },
  });

  assert.equal(
    await authorizer.authorizeMessage(message, {
      serviceNamespace: protocol.namespace,
      name: "play",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      params: {},
    }),
    AuthorizationDecision.AUTHORIZED,
  );

  const mismatchedTerms = serviceUseTerms(descriptor!, clientEndpoint.endpoint, {
    operations: ["pause"],
  });
  const pauseMessage = buildMessage({
    lane: "services",
    sender: clientEndpoint.endpoint,
    senderSessionId: clientEndpoint.sessionId,
    recipient: descriptor!.endpoint,
    recipientSessionId: descriptor!.sessionId,
    contract: {
      contractId: agreement.contract.contractId,
      generation: agreement.contract.generation,
    },
    messageType: "serviceMessage",
    subject: entitySubject("service", {
      serviceId: "music",
      namespace: protocol.namespace,
      name: "pause",
    }),
    body: {
      serviceNamespace: protocol.namespace,
      name: "pause",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      params: {},
    },
  });

  assert.equal(
    await authorizer.authorizeMessage(
      pauseMessage,
      {
        serviceNamespace: protocol.namespace,
        name: "pause",
        intent: ServiceMessageIntent.COMMAND,
        exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
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
    currentSessions: {
      [descriptor!.endpoint]: descriptor!.sessionId,
      [otherClientEndpoint.endpoint]: otherClientEndpoint.sessionId,
    },
  });

  assert.equal(
    await authorizer.authorizeMessage(pauseMessage, {
      serviceNamespace: protocol.namespace,
      name: "pause",
      intent: ServiceMessageIntent.COMMAND,
      exchangePattern: ServiceExchangePattern.REQUEST_REPLY,
      params: {},
    }),
    AuthorizationDecision.DENIED,
  );
});

test("service view writer stores contract-fenced entries with fence metadata", async () => {
  const state = new MemoryStateStore({ name: "dev_deckr_test_service_view_v1" });
  const view = serviceViewRef();
  const writer = new ServiceViewStoreWriter({
    state,
    context: serviceViewWriteContext(ServiceViewWriter.SERVICE),
  });

  const entry = await writer.put(view, {
    status: "playing",
  });
  const storageKey = serviceViewStorageKey(view, VIEW_CONTRACT);
  const stored = await state.get(storageKey);

  assert.equal(storageKey, `${view.key}.contract.b64_Y29udHJhY3Q6YWxwaGE.7`);
  assert.equal(entry.storageKey, storageKey);
  assert.notEqual(stored, null);
  assert.deepEqual(stored!.value, {
    status: "playing",
    viewKey: view.key,
    serviceId: "music",
    serviceNamespace: TEST_SERVICE_PROTOCOL.namespace,
    serviceEndpoint: serviceAddress("music"),
    serviceSessionId: "service-session",
    consumerEndpoint: controllerAddress("main"),
    consumerSessionId: "controller-session",
    writer: ServiceViewWriter.SERVICE,
    contractId: VIEW_CONTRACT.contractId,
    generation: VIEW_CONTRACT.generation,
  });
  assert.equal(await state.get(view.key), null);
});

test("service view reads ignore unfenced and mismatched entries", async () => {
  const view = serviceViewRef();
  const readContext = serviceViewReadContext(ServiceViewWriter.CONSUMER);

  async function readAfter(entries: Array<{ key: string; value: JsonObject }>) {
    const state = new MemoryStateStore({ name: "dev_deckr_test_service_view_v1" });
    for (const entry of entries) {
      await state.put(entry.key, entry.value);
    }
    return new ManagedServiceViewAccess({ state, readContext }).read(view);
  }

  assert.equal(
    await readAfter([
      {
        key: view.key,
        value: {
          serviceId: "music",
          serviceNamespace: TEST_SERVICE_PROTOCOL.namespace,
          sessionId: "service-session",
          status: "legacy",
        },
      },
    ]),
    null,
  );
  assert.equal(
    await readAfter([
      {
        key: serviceViewStorageKey(view, { contractId: "successor", generation: 1 }),
        value: fencedViewPayload(view, {
          contractId: "successor",
          generation: 1,
        }),
      },
    ]),
    null,
  );
  assert.equal(
    await readAfter([
      {
        key: serviceViewStorageKey(view, VIEW_CONTRACT),
        value: fencedViewPayload(view, { serviceSessionId: "old-service-session" }),
      },
    ]),
    null,
  );
  assert.equal(
    await readAfter([
      {
        key: serviceViewStorageKey(view, VIEW_CONTRACT),
        value: fencedViewPayload(view, { consumerSessionId: "old-controller-session" }),
      },
    ]),
    null,
  );
  assert.equal(
    await readAfter([
      {
        key: serviceViewStorageKey(view, VIEW_CONTRACT),
        value: fencedViewPayload(view, { writer: ServiceViewWriter.CONSUMER }),
      },
    ]),
    null,
  );
});

test("service view reader uses the active lease fence", async () => {
  const state = new MemoryStateStore({ name: "dev_deckr_test_service_view_v1" });
  const descriptor = await testServiceDescriptor();
  const view = serviceViewRef();
  const reader = new ServiceViewReader({ stateFor: () => state });
  const lease = validViewLease(descriptor);

  await state.put(serviceViewStorageKey(view, VIEW_CONTRACT), fencedViewPayload(view));

  assert.deepEqual(await reader.read(lease, view), fencedViewPayload(view));
});

test("service view access enforces declared writer direction", async () => {
  const serviceView = serviceViewRef("status", "deck");
  const consumerView = serviceViewRef("preferences", "deck");
  const views = {
    status: TEST_SERVICE_PROTOCOL.viewFamilies.status!,
    preferences: {
      storeName: "dev_deckr_test_service_view_v1",
      keyPrefix: serviceViewKey("music", "preferences"),
      writer: ServiceViewWriter.CONSUMER,
    },
  };
  const state = new MemoryStateStore({ name: "dev_deckr_test_service_view_v1" });

  const serviceWriter = new ManagedServiceViewAccess({
    state,
    writeContext: serviceViewWriteContext(ServiceViewWriter.SERVICE, views),
  });
  const consumerReader = new ManagedServiceViewAccess({
    state,
    readContext: serviceViewReadContext(ServiceViewWriter.CONSUMER, views),
  });
  const consumerWriter = new ManagedServiceViewAccess({
    state,
    writeContext: serviceViewWriteContext(ServiceViewWriter.CONSUMER, views),
  });
  const serviceReader = new ManagedServiceViewAccess({
    state,
    readContext: serviceViewReadContext(ServiceViewWriter.SERVICE, views),
  });

  await serviceWriter.put(serviceView, { status: "playing" });
  assert.equal((await consumerReader.read(serviceView))?.value.status, "playing");
  await assert.rejects(
    () => consumerWriter.put(serviceView, { status: "paused" }),
    ValidationError,
  );
  await assert.rejects(
    () => serviceReader.read(serviceView),
    ValidationError,
  );

  await consumerWriter.put(consumerView, { mode: "compact" });
  assert.equal((await serviceReader.read(consumerView))?.value.mode, "compact");
  await assert.rejects(
    () => serviceWriter.put(consumerView, { mode: "full" }),
    ValidationError,
  );
  await assert.rejects(
    () => consumerReader.read(consumerView),
    ValidationError,
  );
});

test("service-use scope index replaces a valid same-session pointer without local token", async () => {
  const runtime = serviceUseTestRuntime();
  const descriptor = await testServiceDescriptor();
  const first = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: runtime.manager,
    descriptor,
  });
  const secondManager = new ServiceUseLeaseManager({
    endpoint: runtime.client,
    concord: runtime.concord,
    serviceUseIndex: runtime.serviceUseIndex,
  });

  const reused = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: secondManager,
    descriptor,
  });

  assert.notEqual(reused.agreement.contract.contractId, first.agreement.contract.contractId);
  assert.equal(
    reused.terms.serviceUseScopeId,
    first.terms.serviceUseScopeId,
  );
  const oldRecord = await runtime.concord.contractRecord(first.agreement.contract);
  assert.equal(oldRecord?.state, ContractState.CANCELLED);
  assert.equal((await runtime.concord.contracts(descriptor.useProfile)).length, 2);
});

test("service-use scope index replaces a stale pointer", async () => {
  const runtime = serviceUseTestRuntime();
  const descriptor = await testServiceDescriptor();
  const terms = serviceUseTerms(descriptor, runtime.client.endpoint, {
    operations: ["play"],
  });
  const stalePointer = { contractId: "missing-service-use", generation: 1 };
  await runtime.serviceUseIndex.create(
    serviceUseScopeIndexKey(terms.serviceUseScopeId),
    serviceUseIndexRecord(terms, stalePointer, runtime.client),
  );

  const lease = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: runtime.manager,
    descriptor,
  });

  assert.notEqual(lease.agreement.contract.contractId, stalePointer.contractId);
  const record = await runtime.concord.contractRecord(lease.agreement.contract);
  assert.deepEqual(record?.supersedes, stalePointer);
});

test("service-use scope index replaces a client session mismatch and cancels the old contract", async () => {
  const runtime = serviceUseTestRuntime(testClientEndpoint("controller-session-1"));
  const descriptor = await testServiceDescriptor();
  const first = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: runtime.manager,
    descriptor,
  });
  const nextClient = testClientEndpoint("controller-session-2");
  const nextManager = new ServiceUseLeaseManager({
    endpoint: nextClient,
    concord: runtime.concord,
    serviceUseIndex: runtime.serviceUseIndex,
  });

  const successor = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: nextManager,
    descriptor,
  });

  const oldPointer = {
    contractId: first.agreement.contract.contractId,
    generation: first.agreement.contract.generation,
  };
  assert.equal(
    successor.terms.serviceUseScopeId,
    first.terms.serviceUseScopeId,
  );
  assert.notEqual(
    successor.agreement.contract.contractId,
    first.agreement.contract.contractId,
  );
  const successorRecord = await runtime.concord.contractRecord(successor.agreement.contract);
  assert.deepEqual(successorRecord?.supersedes, oldPointer);
  const oldRecord = await runtime.concord.contractRecord(first.agreement.contract);
  assert.equal(oldRecord?.state, ContractState.CANCELLED);
  const index = await runtime.serviceUseIndex.get(
    serviceUseScopeIndexKey(successor.terms.serviceUseScopeId),
  );
  assert.deepEqual(index?.value.supersedes, oldPointer);
});

test("service-use scope index replaces a missing client token", async () => {
  const runtime = serviceUseTestRuntime();
  const descriptor = await testServiceDescriptor();
  const first = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: runtime.manager,
    descriptor,
  });
  const token = first.agreement.localToken;
  assert.notEqual(token, null);
  await runtime.tokenState.delete(token!.key, { revision: token!.revision });
  const secondManager = new ServiceUseLeaseManager({
    endpoint: runtime.client,
    concord: runtime.concord,
    serviceUseIndex: runtime.serviceUseIndex,
  });

  const successor = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: secondManager,
    descriptor,
  });

  assert.notEqual(
    successor.agreement.contract.contractId,
    first.agreement.contract.contractId,
  );
  const record = await runtime.concord.contractRecord(successor.agreement.contract);
  assert.deepEqual(record?.supersedes, {
    contractId: first.agreement.contract.contractId,
    generation: first.agreement.contract.generation,
  });
});

test("service-use scope index replaces a missing service token", async () => {
  const runtime = serviceUseTestRuntime();
  const descriptor = await testServiceDescriptor();
  const terms = serviceUseTerms(descriptor, runtime.client.endpoint, {
    operations: ["play"],
  });
  const pending = await runtime.concord.ensureAgreement({
    profile: descriptor.useProfile,
    participants: [runtime.client.endpoint, descriptor.endpoint],
    localParticipant: runtime.client.endpoint,
    localSessionId: runtime.client.sessionId,
    terms: terms as unknown as JsonObject,
    currentSessions: {
      [descriptor.endpoint]: descriptor.sessionId,
      [runtime.client.endpoint]: runtime.client.sessionId,
    },
  });
  const oldPointer = {
    contractId: pending.contract.contractId,
    generation: pending.contract.generation,
  };
  await runtime.serviceUseIndex.create(
    serviceUseScopeIndexKey(terms.serviceUseScopeId),
    serviceUseIndexRecord(terms, oldPointer, runtime.client),
  );

  await assert.rejects(
    () => runtime.manager.ensure(descriptor, { operations: ["play"], timeoutMs: 0 }),
    (error) => error instanceof ServiceUnavailable && error.code === "service_contract_pending",
  );

  const oldRecord = await runtime.concord.contractRecord(pending.contract);
  assert.equal(oldRecord?.state, ContractState.CANCELLED);
  const index = await runtime.serviceUseIndex.get(serviceUseScopeIndexKey(terms.serviceUseScopeId));
  assert.notEqual(index, null);
  assert.notDeepEqual(index!.value.contract, oldPointer);
  assert.deepEqual(index!.value.supersedes, oldPointer);
  const successor = await runtime.concord.getContract(index!.value.contract as unknown as ContractPointer);
  assert.notEqual(successor, null);
  const successorRecord = await runtime.concord.contractRecord(successor!);
  assert.deepEqual(successorRecord?.supersedes, oldPointer);
});

test("service-use scope index replaces a terms hash mismatch", async () => {
  const runtime = serviceUseTestRuntime();
  const descriptor = await testServiceDescriptor();
  const first = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: runtime.manager,
    descriptor,
  });
  const key = serviceUseScopeIndexKey(first.terms.serviceUseScopeId);
  const current = await runtime.serviceUseIndex.get(key);
  assert.notEqual(current, null);
  await runtime.serviceUseIndex.update(
    key,
    { ...current!.value, termsHash: "sha256:stale" },
    { revision: current!.revision },
  );
  const secondManager = new ServiceUseLeaseManager({
    endpoint: runtime.client,
    concord: runtime.concord,
    serviceUseIndex: runtime.serviceUseIndex,
  });

  const successor = await acquireServiceUseLeaseWithServiceToken({
    concord: runtime.concord,
    manager: secondManager,
    descriptor,
  });

  assert.notEqual(
    successor.agreement.contract.contractId,
    first.agreement.contract.contractId,
  );
  const oldRecord = await runtime.concord.contractRecord(first.agreement.contract);
  assert.equal(oldRecord?.state, ContractState.CANCELLED);
});

test("service-use scope index retries after a CAS conflict", async () => {
  class ConflictOnceStore extends MemoryStateStore {
    conflicts = 1;

    override async create(
      key: string,
      value: Parameters<MemoryStateStore["create"]>[1],
      options: Parameters<MemoryStateStore["create"]>[2] = {},
    ) {
      if (this.conflicts > 0) {
        this.conflicts -= 1;
        throw new StateConflict("simulated scope-index race");
      }
      return super.create(key, value, options);
    }
  }

  const client = testClientEndpoint();
  const contractState = new MemoryStateStore({ policy: CONCORD_CONTRACT_STORE_POLICY });
  const tokenState = new MemoryStateStore({ policy: CONCORD_TOKEN_STORE_POLICY });
  const serviceUseIndex = new ConflictOnceStore({
    name: DEFAULT_SERVICE_USE_INDEX_STORE_NAME,
  });
  const concord = new ConcordService(new ConcordCoordinator(contractState, tokenState));
  const manager = new ServiceUseLeaseManager({
    endpoint: client,
    concord,
    serviceUseIndex,
  });
  const descriptor = await testServiceDescriptor();

  const lease = await acquireServiceUseLeaseWithServiceToken({
    concord,
    manager,
    descriptor,
  });

  const contracts = await concord.contracts(descriptor.useProfile);
  assert.equal(contracts.length, 2);
  const cancelled = await Promise.all(
    contracts
      .filter((contract) => contract.contractId !== lease.agreement.contract.contractId)
      .map((contract) => concord.contractRecord(contract)),
  );
  assert.equal(cancelled[0]?.state, ContractState.CANCELLED);
});
