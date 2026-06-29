import {
  BeaconAdvertisement,
  BeaconService,
  type Candidate,
  type BeaconAdvertisementSpec,
} from "./beacon.ts";
import {
  ConcordAgreement,
  ConcordService,
  ContractState,
  ContractValidityStatus,
  canonicalJsonHash,
  type ContractHandle,
  type ContractRecord,
  type ConcordAgreementSpec,
} from "./concord.ts";
import { endpointAddress, endpointTarget, serviceAddress } from "./endpoint.ts";
import { ServiceUnavailable, StateConflict, StateUnavailable, ValidationError } from "./errors.ts";
import {
  cloneJson,
  requireJsonObject,
  requireText,
  type JsonObject,
  type JsonValue,
} from "./json.ts";
import { encodeKeyToken } from "./keys.ts";
import {
  entitySubject,
  type DeckrMessage,
  type EntitySubject,
} from "./lanes.ts";
import type { StateStore } from "./state.ts";

export const DEFAULT_SERVICE_CONTRACT_RECONCILE_SECONDS = 300;
export const DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS = 5;
export const DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS = 5;

export const ServiceBackendStatus = Object.freeze({
  AVAILABLE: "available",
  DEGRADED: "degraded",
  UNAVAILABLE: "unavailable",
});
export type ServiceBackendStatus =
  (typeof ServiceBackendStatus)[keyof typeof ServiceBackendStatus];

export const AuthorizationDecision = Object.freeze({
  AUTHORIZED: "authorized",
  DENIED: "denied",
  NOT_APPLICABLE: "not_applicable",
});
export type AuthorizationDecision =
  (typeof AuthorizationDecision)[keyof typeof AuthorizationDecision];

export const ServiceCommandStatus = Object.freeze({
  OK: "ok",
  REJECTED: "rejected",
  UNAVAILABLE: "unavailable",
});
export type ServiceCommandStatus =
  (typeof ServiceCommandStatus)[keyof typeof ServiceCommandStatus];

export const SERVICE_COMMAND = "serviceCommand";
export const SERVICE_COMMAND_REPLY = "serviceCommandReply";

export interface ServiceViewFamily {
  storeName: string;
  keyPrefix: string;
}

export interface ServiceViewRef {
  storeName: string;
  key: string;
}

export interface ServiceProtocol {
  namespace: string;
  featureId: string;
  advertisementProfile: string;
  useProfile: string;
  operations: string[];
  viewFamilies: Record<string, ServiceViewFamily>;
}

export interface ServiceAdvertisementPayload {
  profile: string;
  serviceId: string;
  serviceEndpoint: string;
  serviceNamespace: string;
  sessionId: string;
  serviceUseProfile: string;
  backendStatus: ServiceBackendStatus;
  supportedOperations: string[];
  views: Record<string, ServiceViewFamily>;
  diagnostics: JsonObject;
}

export interface ServiceUseTerms {
  profile: string;
  serviceUseId: string;
  serviceId: string;
  serviceEndpoint: string;
  serviceNamespace: string;
  serviceSessionId: string;
  clientEndpoint: string;
  allowedOperations: string[];
  allowedViews: Record<string, string[]>;
}

export interface ServiceDescriptor {
  candidate: Candidate;
  serviceId: string;
  namespace: string;
  endpoint: string;
  sessionId: string;
  advertisementProfile: string;
  useProfile: string;
  supportedOperations: Set<string>;
  views: Record<string, ServiceViewFamily>;
  backendStatus: ServiceBackendStatus;
  diagnostics: JsonObject;
}

export interface ServiceUseLease {
  agreement: ConcordAgreement;
  descriptor: ServiceDescriptor;
  terms: ServiceUseTerms;
}

export interface ServiceCommandBody {
  serviceNamespace: string;
  operation: string;
  params: JsonObject;
}

export interface ServiceError {
  code: string;
  message: string;
  diagnostics: JsonObject;
}

export interface ServiceCommandReplyBody {
  serviceNamespace: string;
  operation: string;
  status: ServiceCommandStatus;
  result?: JsonValue;
  error?: ServiceError;
}

export interface RegisteredEndpointLane {
  endpoint: string;
  sessionId: string;
  request?(options: {
    recipient: string;
    recipientSessionId?: string;
    subject: EntitySubject;
    messageType: string;
    body: JsonObject;
    timeout?: number;
    accept?: (message: DeckrMessage) => boolean;
  }): Promise<DeckrMessage>;
}

export function validateServiceProtocol(input: ServiceProtocol): ServiceProtocol {
  const operations = input.operations.map((item) => requireText(item, "service operation"));
  if (operations.length === 0) {
    throw new ValidationError("service protocol operations must not be empty");
  }
  const viewFamilies: Record<string, ServiceViewFamily> = {};
  for (const [name, family] of Object.entries(input.viewFamilies)) {
    viewFamilies[requireText(name, "service view family")] = {
      storeName: requireText(family.storeName, "service view storeName"),
      keyPrefix: requireText(family.keyPrefix, "service view keyPrefix"),
    };
  }
  return {
    namespace: requireText(input.namespace, "service namespace"),
    featureId: requireText(input.featureId, "service feature id"),
    advertisementProfile: requireText(
      input.advertisementProfile,
      "service advertisement profile",
    ),
    useProfile: requireText(input.useProfile, "service use profile"),
    operations,
    viewFamilies,
  };
}

export function serviceAdvertisementPayload(
  protocol: ServiceProtocol,
  input: {
    serviceId: string;
    sessionId: string;
    backendStatus: ServiceBackendStatus;
    diagnostics?: JsonObject;
  },
): ServiceAdvertisementPayload {
  const serviceId = requireText(input.serviceId, "service id");
  return validateServiceAdvertisementPayload({
    profile: protocol.advertisementProfile,
    serviceId,
    serviceEndpoint: serviceAddress(serviceId),
    serviceNamespace: protocol.namespace,
    sessionId: input.sessionId,
    serviceUseProfile: protocol.useProfile,
    backendStatus: input.backendStatus,
    supportedOperations: protocol.operations,
    views: protocol.viewFamilies,
    diagnostics: input.diagnostics ?? {},
  });
}

export function validateServiceAdvertisementPayload(value: unknown): ServiceAdvertisementPayload {
  const raw = requireJsonObject(value, "service advertisement payload");
  const backendStatus = requireText(raw.backendStatus, "backendStatus") as ServiceBackendStatus;
  if (!Object.values(ServiceBackendStatus).includes(backendStatus)) {
    throw new ValidationError("invalid service backendStatus");
  }
  const serviceId = requireText(raw.serviceId, "serviceId");
  const serviceEndpoint = endpointAddress(requireText(raw.serviceEndpoint, "serviceEndpoint"));
  if (serviceEndpoint !== serviceAddress(serviceId)) {
    throw new ValidationError("serviceEndpoint must equal service:<serviceId>");
  }
  const supportedOperations = validateTextArray(raw.supportedOperations, "supportedOperations");
  if (supportedOperations.length === 0) {
    throw new ValidationError("service advertisement requires operations");
  }
  const viewsRaw = requireJsonObject(raw.views, "views");
  const views: Record<string, ServiceViewFamily> = {};
  for (const [name, item] of Object.entries(viewsRaw)) {
    const family = requireJsonObject(item, "service view family");
    views[requireText(name, "service view family name")] = {
      storeName: requireText(family.storeName, "storeName"),
      keyPrefix: requireText(family.keyPrefix, "keyPrefix"),
    };
  }
  return {
    profile: requireText(raw.profile, "profile"),
    serviceId,
    serviceEndpoint,
    serviceNamespace: requireText(raw.serviceNamespace, "serviceNamespace"),
    sessionId: requireText(raw.sessionId, "sessionId"),
    serviceUseProfile: requireText(raw.serviceUseProfile, "serviceUseProfile"),
    backendStatus,
    supportedOperations,
    views,
    diagnostics: requireJsonObject(raw.diagnostics ?? {}, "diagnostics"),
  };
}

export function validateServiceUseTerms(value: unknown): ServiceUseTerms {
  const raw = requireJsonObject(value, "service use terms");
  const serviceId = requireText(raw.serviceId, "serviceId");
  const serviceEndpoint = endpointAddress(requireText(raw.serviceEndpoint, "serviceEndpoint"));
  if (serviceEndpoint !== serviceAddress(serviceId)) {
    throw new ValidationError("serviceEndpoint must equal service:<serviceId>");
  }
  const allowedViewsRaw = requireJsonObject(raw.allowedViews ?? {}, "allowedViews");
  const allowedViews: Record<string, string[]> = {};
  for (const [family, prefixes] of Object.entries(allowedViewsRaw)) {
    allowedViews[requireText(family, "service view family")] = validateTextArray(
      prefixes,
      "service view prefix",
    );
  }
  return {
    profile: requireText(raw.profile, "profile"),
    serviceUseId: requireText(raw.serviceUseId, "serviceUseId"),
    serviceId,
    serviceEndpoint,
    serviceNamespace: requireText(raw.serviceNamespace, "serviceNamespace"),
    serviceSessionId: requireText(raw.serviceSessionId, "serviceSessionId"),
    clientEndpoint: endpointAddress(requireText(raw.clientEndpoint, "clientEndpoint")),
    allowedOperations: validateTextArray(raw.allowedOperations ?? [], "allowedOperations"),
    allowedViews,
  };
}

export class ServiceAdvertiser {
  readonly protocol: ServiceProtocol;
  readonly serviceId: string;
  readonly endpoint: RegisteredEndpointLane;

  private readonly beacon: BeaconService;
  private readonly refreshIntervalSeconds: number;
  private advertiser: BeaconAdvertisement | null = null;
  private advertisementHandle: Awaited<ReturnType<BeaconAdvertisement["publish"]>> | null = null;
  private backendStatusValue: ServiceBackendStatus = ServiceBackendStatus.UNAVAILABLE;
  private backendDiagnosticsValue: JsonObject = {};

  constructor(options: {
    protocol: ServiceProtocol;
    serviceId: string;
    endpoint: RegisteredEndpointLane;
    beacon: BeaconService;
    refreshIntervalSeconds?: number;
  }) {
    this.protocol = validateServiceProtocol(options.protocol);
    this.serviceId = requireText(options.serviceId, "service id");
    this.endpoint = {
      endpoint: endpointAddress(options.endpoint.endpoint),
      sessionId: requireText(options.endpoint.sessionId, "endpoint session id"),
      request: options.endpoint.request,
    };
    this.beacon = options.beacon;
    this.refreshIntervalSeconds =
      options.refreshIntervalSeconds ?? DEFAULT_SERVICE_ADVERTISEMENT_REFRESH_SECONDS;
  }

  get advertisement() {
    return this.advertisementHandle;
  }

  get backendStatus(): ServiceBackendStatus {
    return this.backendStatusValue;
  }

  get backendDiagnostics(): JsonObject {
    return this.backendDiagnosticsValue;
  }

  async publish(
    status: ServiceBackendStatus,
    options: { diagnostics?: JsonObject } = {},
  ): Promise<void> {
    this.backendStatusValue = status;
    this.backendDiagnosticsValue = cloneJson(options.diagnostics ?? {});
    const payload = serviceAdvertisementPayload(this.protocol, {
      serviceId: this.serviceId,
      sessionId: this.endpoint.sessionId,
      backendStatus: status,
      diagnostics: this.backendDiagnosticsValue,
    });
    if (this.advertiser === null || this.advertiser.closed) {
      this.advertiser = await this.beacon.ensureAdvertisement({
        featureId: this.protocol.featureId,
        endpoint: this.endpoint.endpoint,
        sessionId: this.endpoint.sessionId,
        payload: payload as unknown as JsonObject,
        operations: this.protocol.operations,
        refreshIntervalSeconds: this.refreshIntervalSeconds,
      } satisfies BeaconAdvertisementSpec);
    }
    this.advertisementHandle = await this.advertiser.publish({
      payload: payload as unknown as JsonObject,
    });
  }

  async withdraw(): Promise<void> {
    if (this.advertiser !== null) {
      await this.advertiser.close();
    }
    this.advertiser = null;
    this.advertisementHandle = null;
  }
}

export class ServiceUseLeaseManager {
  private readonly endpoint: RegisteredEndpointLane;
  private readonly concord: ConcordService;
  private readonly refreshIntervalSeconds: number;
  private readonly leases = new Map<string, ServiceUseLease>();
  private closed = false;

  constructor(options: {
    endpoint: RegisteredEndpointLane;
    concord: ConcordService;
    refreshIntervalSeconds?: number;
  }) {
    this.endpoint = {
      endpoint: endpointAddress(options.endpoint.endpoint),
      sessionId: requireText(options.endpoint.sessionId, "endpoint session id"),
      request: options.endpoint.request,
    };
    this.concord = options.concord;
    this.refreshIntervalSeconds =
      options.refreshIntervalSeconds ?? DEFAULT_SERVICE_TOKEN_REFRESH_SECONDS;
  }

  async close(): Promise<void> {
    this.closed = true;
    const leases = [...this.leases.values()];
    this.leases.clear();
    for (const lease of leases) {
      await lease.agreement.cancel("service lease manager closed");
    }
  }

  async ensure(
    descriptor: ServiceDescriptor,
    options: {
      operations?: string[];
      views?: string[] | Record<string, string[]>;
      timeoutMs?: number;
    } = {},
  ): Promise<ServiceUseLease> {
    if (this.closed) {
      throw new ServiceUnavailable("client_closed", "The service lease manager is closed");
    }
    const terms = serviceUseTerms(descriptor, this.endpoint.endpoint, {
      operations: options.operations ?? [],
      views: options.views ?? [],
    });
    const key = leaseKey(this.endpoint, descriptor, terms);
    let lease = this.leases.get(key);
    if (lease === undefined || JSON.stringify(lease.terms) !== JSON.stringify(terms)) {
      if (lease !== undefined) {
        await lease.agreement.cancel("service terms changed");
      }
      lease = await this.createOrReuseLease(descriptor, terms);
      this.leases.set(key, lease);
    }
    return this.validLease(lease, options.timeoutMs ?? 2000);
  }

  async cached(options: {
    serviceId: string;
    namespace: string;
    operations?: string[];
    views?: string[] | Record<string, string[]>;
    timeoutMs?: number;
  }): Promise<ServiceUseLease | null> {
    for (const lease of [...this.leases.values()].sort((left, right) =>
      serviceDescriptorSortKey(right.descriptor).localeCompare(
        serviceDescriptorSortKey(left.descriptor),
      ),
    )) {
      if (
        lease.descriptor.serviceId !== options.serviceId ||
        lease.descriptor.namespace !== options.namespace ||
        !leaseCoversScope(lease, {
          operations: options.operations ?? [],
          views: options.views ?? [],
        })
      ) {
        continue;
      }
      try {
        return await this.validLease(lease, options.timeoutMs ?? 0);
      } catch (error) {
        if (error instanceof ServiceUnavailable) {
          this.leases.delete(leaseKey(this.endpoint, lease.descriptor, lease.terms));
          continue;
        }
        throw error;
      }
    }
    return null;
  }

  private async createOrReuseLease(
    descriptor: ServiceDescriptor,
    terms: ServiceUseTerms,
  ): Promise<ServiceUseLease> {
    const agreement = await this.concord.ensureAgreement({
      profile: descriptor.useProfile,
      participants: [descriptor.endpoint, this.endpoint.endpoint],
      localParticipant: this.endpoint.endpoint,
      localSessionId: this.endpoint.sessionId,
      terms: terms as unknown as JsonObject,
      stableContractId: terms.serviceUseId,
      currentSessions: {
        [this.endpoint.endpoint]: this.endpoint.sessionId,
        [descriptor.endpoint]: descriptor.sessionId,
      },
      refreshIntervalSeconds: this.refreshIntervalSeconds,
    } satisfies ConcordAgreementSpec);
    return { agreement, descriptor, terms };
  }

  private async validLease(lease: ServiceUseLease, timeoutMs: number): Promise<ServiceUseLease> {
    const deadline = Date.now() + Math.max(timeoutMs, 0);
    while (true) {
      let validity;
      try {
        validity = await lease.agreement.refresh();
      } catch (error) {
        if (error instanceof StateConflict) {
          throw new ServiceUnavailable("contract_invalid_token", "Service-use contract could not be refreshed", {
            status: ContractValidityStatus.INVALID_TOKEN,
            reason: error.message,
          });
        }
        throw error;
      }
      if (validity.valid) {
        return lease;
      }
      if (validity.status !== ContractValidityStatus.NOT_YET_FULFILLED) {
        throw new ServiceUnavailable(
          `contract_${validity.status}`,
          "Service-use contract is not valid",
          { status: validity.status, reason: validity.reason ?? "" },
        );
      }
      if (Date.now() >= deadline) {
        throw new ServiceUnavailable(
          "service_contract_pending",
          "Service-use contract is pending the service token",
          { status: validity.status },
        );
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
}

export class ServiceUseAuthorizer {
  readonly protocol: ServiceProtocol;
  readonly serviceId: string;
  readonly endpoint: RegisteredEndpointLane;

  private readonly manager;

  constructor(options: {
    protocol: ServiceProtocol;
    serviceId: string;
    endpoint: RegisteredEndpointLane;
    concord: ConcordService;
  }) {
    this.protocol = validateServiceProtocol(options.protocol);
    this.serviceId = requireText(options.serviceId, "service id");
    this.endpoint = {
      endpoint: endpointAddress(options.endpoint.endpoint),
      sessionId: requireText(options.endpoint.sessionId, "endpoint session id"),
      request: options.endpoint.request,
    };
    this.manager = options.concord.participantManager({
      participant: this.endpoint.endpoint,
      sessionId: this.endpoint.sessionId,
      profile: this.protocol.useProfile,
      cancelTerminalStatuses: [
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
      ],
      acceptContract: (contract, record) =>
        this.matchingTermsRecord(contract, record) !== null,
      currentSessions: () => ({ [this.endpoint.endpoint]: this.endpoint.sessionId }),
    });
  }

  async reconcileContracts(): Promise<void> {
    await this.manager.reconcile();
  }

  async matchingTerms(contract: ContractHandle): Promise<ServiceUseTerms | null> {
    const managed = this.manager.managedContract(contract);
    if (managed === null) {
      return null;
    }
    return this.matchingTermsRecord(contract, managed.record);
  }

  async authorizeCommand(
    message: DeckrMessage,
    body: ServiceCommandBody,
  ): Promise<AuthorizationDecision> {
    if (
      !commandAppliesToService(message, body, {
        serviceId: this.serviceId,
        namespace: this.protocol.namespace,
        endpoint: this.endpoint.endpoint,
      })
    ) {
      return AuthorizationDecision.NOT_APPLICABLE;
    }
    await this.reconcileContracts();
    for (const managed of this.manager.managedContracts()) {
      const terms = this.matchingTermsRecord(managed.contract, managed.record);
      if (terms === null) {
        continue;
      }
      if (terms.clientEndpoint !== message.sender) {
        continue;
      }
      if (!terms.allowedOperations.includes(body.operation)) {
        continue;
      }
      const validity = await this.manager.validate(managed.contract, {
        currentSessions: {
          [terms.serviceEndpoint]: this.endpoint.sessionId,
          [terms.clientEndpoint]: message.senderSessionId,
        },
      });
      if (validity.status === ContractValidityStatus.VALID) {
        return AuthorizationDecision.AUTHORIZED;
      }
    }
    return AuthorizationDecision.DENIED;
  }

  private matchingTermsRecord(
    contract: ContractHandle,
    record: ContractRecord,
  ): ServiceUseTerms | null {
    if (record.state !== ContractState.OPEN || record.terms === undefined) {
      return null;
    }
    let terms: ServiceUseTerms;
    try {
      terms = validateServiceUseTerms(record.terms);
    } catch {
      return null;
    }
    if (contract.contractId !== terms.serviceUseId) {
      return null;
    }
    if (!serviceUseParticipantsMatch(record, terms)) {
      return null;
    }
    if (
      terms.profile !== this.protocol.useProfile ||
      terms.serviceNamespace !== this.protocol.namespace ||
      terms.serviceId !== this.serviceId ||
      terms.serviceEndpoint !== this.endpoint.endpoint ||
      terms.serviceSessionId !== this.endpoint.sessionId
    ) {
      return null;
    }
    return terms;
  }
}

function serviceUseParticipantsMatch(record: ContractRecord, terms: ServiceUseTerms): boolean {
  const expected = [terms.clientEndpoint, terms.serviceEndpoint].sort();
  return (
    record.participants.length === expected.length &&
    record.participants.every((participant, index) => participant === expected[index])
  );
}

export class ServiceCommandChannel {
  private readonly endpoint: RegisteredEndpointLane;

  constructor(options: { endpoint: RegisteredEndpointLane }) {
    this.endpoint = options.endpoint;
  }

  async command(
    lease: ServiceUseLease,
    operation: string,
    params: JsonObject = {},
    options: { timeoutMs?: number } = {},
  ): Promise<ServiceCommandReplyBody> {
    operation = requireText(operation, "service operation");
    if (!lease.terms.allowedOperations.includes(operation)) {
      return rejectedReply(lease.descriptor.namespace, operation, {
        code: "operation_not_authorized",
        message: `Service-use lease does not authorize operation ${JSON.stringify(operation)}`,
      });
    }
    try {
      await lease.agreement.refresh();
    } catch (error) {
      return unavailableReply(lease.descriptor.namespace, operation, {
        code: error instanceof ServiceUnavailable ? error.code : "contract_unavailable",
        message: error instanceof Error ? error.message : String(error),
      });
    }
    if (this.endpoint.request === undefined) {
      return unavailableReply(lease.descriptor.namespace, operation, {
        code: "client_endpoint_unavailable",
        message: "The services endpoint is unavailable",
      });
    }
    const reply = await this.endpoint.request({
      recipient: lease.descriptor.endpoint,
      recipientSessionId: lease.descriptor.sessionId,
      subject: entitySubject("service", {
        serviceId: lease.descriptor.serviceId,
        namespace: lease.descriptor.namespace,
        operation,
      }),
      messageType: SERVICE_COMMAND,
      body: {
        serviceNamespace: lease.descriptor.namespace,
        operation,
        params,
      },
      timeout: options.timeoutMs,
    });
    return validateServiceCommandReplyBody(reply.body);
  }
}

export class ServiceViewReader {
  private readonly stateFor: (storeName: string) => StateStore;

  constructor(options: { stateFor: (storeName: string) => StateStore }) {
    this.stateFor = options.stateFor;
  }

  async read(lease: ServiceUseLease, view: ServiceViewRef): Promise<JsonObject | null> {
    if (!viewRefAuthorized(lease, view)) {
      throw new ValidationError(`Service-use lease does not authorize view ${JSON.stringify(view.key)}`);
    }
    const validity = await lease.agreement.refresh();
    if (!validity.valid) {
      return null;
    }
    const entry = await this.stateFor(view.storeName).get(view.key);
    if (entry === null) {
      return null;
    }
    const value = requireJsonObject(entry.value, "service view value");
    if (
      value.serviceId !== lease.descriptor.serviceId ||
      value.serviceNamespace !== lease.descriptor.namespace ||
      value.sessionId !== lease.descriptor.sessionId
    ) {
      return null;
    }
    return value;
  }
}

export class ServiceViewWriter {
  readonly protocol: ServiceProtocol;
  readonly serviceId: string;
  readonly endpoint: RegisteredEndpointLane;

  private readonly state: StateStore;
  private readonly revisions = new Map<string, number>();

  constructor(options: {
    protocol: ServiceProtocol;
    serviceId: string;
    endpoint: RegisteredEndpointLane;
    state: StateStore;
  }) {
    this.protocol = validateServiceProtocol(options.protocol);
    this.serviceId = requireText(options.serviceId, "service id");
    this.endpoint = options.endpoint;
    this.state = options.state;
  }

  async put(key: string, payload: JsonObject): Promise<void> {
    const entry = await this.state.put(key, {
      ...payload,
      serviceId: this.serviceId,
      serviceNamespace: this.protocol.namespace,
      sessionId: this.endpoint.sessionId,
    });
    this.revisions.set(key, entry.revision);
  }

  async withdraw(): Promise<void> {
    for (const [key, revision] of [...this.revisions]) {
      try {
        await this.state.delete(key, { revision });
      } catch (error) {
        if (!(error instanceof StateConflict || error instanceof StateUnavailable)) {
          throw error;
        }
      }
      this.revisions.delete(key);
    }
  }
}

export function serviceViewKey(serviceId: string, family: string, ...tokens: string[]): string {
  return ["views", encodeKeyToken(serviceId), encodeKeyToken(family), ...tokens.map(encodeKeyToken)].join(".");
}

export function serviceViewPrefix(serviceId: string, family: string): string {
  return `${serviceViewKey(serviceId, family)}.`;
}

export function parseServiceDescriptor(
  candidate: Candidate,
  protocol: ServiceProtocol,
): ServiceDescriptor | null {
  const normalized = validateServiceProtocol(protocol);
  const advertisement = candidate.advertisement;
  if (advertisement.featureId !== normalized.featureId || advertisement.payload === undefined) {
    return null;
  }
  let payload: ServiceAdvertisementPayload;
  try {
    payload = validateServiceAdvertisementPayload(advertisement.payload);
  } catch {
    return null;
  }
  if (
    payload.profile !== normalized.advertisementProfile ||
    payload.serviceNamespace !== normalized.namespace ||
    payload.serviceUseProfile !== normalized.useProfile ||
    payload.serviceEndpoint !== advertisement.endpoint ||
    payload.sessionId !== advertisement.sessionId
  ) {
    return null;
  }
  if (!payload.supportedOperations.every((item) => normalized.operations.includes(item))) {
    return null;
  }
  if (JSON.stringify(payload.views) !== JSON.stringify(normalized.viewFamilies)) {
    return null;
  }
  return {
    candidate,
    serviceId: payload.serviceId,
    namespace: payload.serviceNamespace,
    endpoint: payload.serviceEndpoint,
    sessionId: payload.sessionId,
    advertisementProfile: payload.profile,
    useProfile: payload.serviceUseProfile,
    supportedOperations: new Set(payload.supportedOperations),
    views: payload.views,
    backendStatus: payload.backendStatus,
    diagnostics: payload.diagnostics,
  };
}

export function serviceUseTerms(
  descriptor: ServiceDescriptor,
  clientEndpoint: string,
  options: {
    operations?: string[];
    views?: string[] | Record<string, string[]>;
  } = {},
): ServiceUseTerms {
  const allowedOperations = [...normalizeOperations(descriptor, options.operations ?? [])].sort();
  const allowedViews = normalizeViewScope(descriptor, options.views ?? []);
  const identity = {
    profile: descriptor.useProfile,
    serviceId: descriptor.serviceId,
    serviceEndpoint: descriptor.endpoint,
    serviceNamespace: descriptor.namespace,
    serviceSessionId: descriptor.sessionId,
    clientEndpoint: endpointAddress(clientEndpoint),
    allowedOperations,
    allowedViews,
  };
  const digest = canonicalJsonHash(identity as unknown as JsonValue)
    .replace("sha256:", "")
    .slice(0, 32);
  return validateServiceUseTerms({
    ...identity,
    serviceUseId: `service-use:${digest}`,
  });
}

export function serviceDescriptorSortKey(descriptor: ServiceDescriptor): string {
  const ad = descriptor.candidate.advertisement;
  return `${ad.updatedAt ?? ad.createdAt ?? ""}:${String(ad.refreshSeq).padStart(12, "0")}:${descriptor.candidate.key}`;
}

export function newestServiceDescriptor(
  descriptors: Iterable<ServiceDescriptor>,
): ServiceDescriptor | null {
  let newest: ServiceDescriptor | null = null;
  for (const descriptor of descriptors) {
    if (newest === null || serviceDescriptorSortKey(descriptor) > serviceDescriptorSortKey(newest)) {
      newest = descriptor;
    }
  }
  return newest;
}

export function validateServiceCommandBody(value: unknown): ServiceCommandBody {
  const raw = requireJsonObject(value, "service command body");
  return {
    serviceNamespace: requireText(raw.serviceNamespace, "serviceNamespace"),
    operation: requireText(raw.operation, "operation"),
    params: requireJsonObject(raw.params ?? {}, "params"),
  };
}

export function validateServiceCommandReplyBody(value: unknown): ServiceCommandReplyBody {
  const raw = requireJsonObject(value, "service command reply body");
  const status = requireText(raw.status, "status") as ServiceCommandStatus;
  if (!Object.values(ServiceCommandStatus).includes(status)) {
    throw new ValidationError("invalid service command reply status");
  }
  const body: ServiceCommandReplyBody = {
    serviceNamespace: requireText(raw.serviceNamespace, "serviceNamespace"),
    operation: requireText(raw.operation, "operation"),
    status,
  };
  if (raw.result !== undefined) {
    body.result = raw.result as JsonValue;
  }
  if (raw.error !== undefined) {
    const error = requireJsonObject(raw.error, "service error");
    body.error = {
      code: requireText(error.code, "error code"),
      message: requireText(error.message, "error message"),
      diagnostics: requireJsonObject(error.diagnostics ?? {}, "error diagnostics"),
    };
  }
  return body;
}

function validateTextArray(value: unknown, fieldName: string): string[] {
  if (!Array.isArray(value)) {
    throw new ValidationError(`${fieldName} must be an array`);
  }
  return value.map((item) => requireText(item, fieldName));
}

function normalizeOperations(descriptor: ServiceDescriptor, operations: string[]): Set<string> {
  const normalized = new Set(operations.map((item) => requireText(item, "service operation")));
  for (const operation of normalized) {
    if (!descriptor.supportedOperations.has(operation)) {
      throw new ValidationError(
        `Service ${JSON.stringify(descriptor.serviceId)} does not advertise operation ${JSON.stringify(operation)}`,
      );
    }
  }
  return normalized;
}

function normalizeViewScope(
  descriptor: ServiceDescriptor,
  views: string[] | Record<string, string[]>,
): Record<string, string[]> {
  const requested: Record<string, string[] | null> = {};
  if (Array.isArray(views)) {
    for (const family of views) {
      requested[requireText(family, "service view family")] = null;
    }
  } else {
    for (const [family, prefixes] of Object.entries(views)) {
      requested[requireText(family, "service view family")] = prefixes.map((prefix) =>
        requireText(prefix, "service view prefix"),
      );
    }
  }
  const result: Record<string, string[]> = {};
  for (const [family, prefixes] of Object.entries(requested)) {
    const viewFamily = descriptor.views[family];
    if (viewFamily === undefined) {
      throw new ValidationError(
        `Service ${JSON.stringify(descriptor.serviceId)} does not advertise view family ${JSON.stringify(family)}`,
      );
    }
    const normalized = [...new Set(prefixes ?? [viewFamily.keyPrefix])].sort();
    for (const prefix of normalized) {
      if (!prefix.startsWith(viewFamily.keyPrefix)) {
        throw new ValidationError(
          `View prefix ${JSON.stringify(prefix)} is outside service view family ${JSON.stringify(family)}`,
        );
      }
    }
    result[family] = normalized;
  }
  return result;
}

function viewRefAuthorized(lease: ServiceUseLease, view: ServiceViewRef): boolean {
  for (const [family, prefixes] of Object.entries(lease.terms.allowedViews)) {
    const viewFamily = lease.descriptor.views[family];
    if (viewFamily === undefined || view.storeName !== viewFamily.storeName) {
      continue;
    }
    if (prefixes.some((prefix) => view.key.startsWith(prefix))) {
      return true;
    }
  }
  return false;
}

function leaseCoversScope(
  lease: ServiceUseLease,
  options: { operations: string[]; views: string[] | Record<string, string[]> },
): boolean {
  try {
    const requestedOperations = normalizeOperations(lease.descriptor, options.operations);
    const requestedViews = normalizeViewScope(lease.descriptor, options.views);
    for (const operation of requestedOperations) {
      if (!lease.terms.allowedOperations.includes(operation)) {
        return false;
      }
    }
    for (const [family, prefixes] of Object.entries(requestedViews)) {
      const allowed = lease.terms.allowedViews[family] ?? [];
      if (!prefixes.every((prefix) => allowed.some((allowedPrefix) => prefix.startsWith(allowedPrefix)))) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}

function leaseKey(
  endpoint: RegisteredEndpointLane,
  descriptor: ServiceDescriptor,
  terms: ServiceUseTerms,
): string {
  return JSON.stringify([
    endpoint.endpoint,
    endpoint.sessionId,
    descriptor.serviceId,
    descriptor.namespace,
    descriptor.endpoint,
    descriptor.sessionId,
    descriptor.useProfile,
    terms.serviceUseId,
  ]);
}

function commandAppliesToService(
  message: DeckrMessage,
  body: ServiceCommandBody,
  options: { serviceId: string; namespace: string; endpoint: string },
): boolean {
  if (body.serviceNamespace !== options.namespace) {
    return false;
  }
  if (message.subject.kind !== "service") {
    return false;
  }
  if (
    message.subject.identifiers.serviceId !== options.serviceId ||
    message.subject.identifiers.namespace !== options.namespace
  ) {
    return false;
  }
  if (message.recipient.targetType === "endpoint") {
    return message.recipient.endpoint === options.endpoint;
  }
  return JSON.stringify(message.recipient) === JSON.stringify(endpointTarget(options.endpoint));
}

function rejectedReply(
  serviceNamespace: string,
  operation: string,
  options: { code: string; message: string; diagnostics?: JsonObject },
): ServiceCommandReplyBody {
  return {
    serviceNamespace,
    operation,
    status: ServiceCommandStatus.REJECTED,
    error: {
      code: options.code,
      message: options.message,
      diagnostics: options.diagnostics ?? {},
    },
  };
}

function unavailableReply(
  serviceNamespace: string,
  operation: string,
  options: { code: string; message: string; diagnostics?: JsonObject },
): ServiceCommandReplyBody {
  return {
    serviceNamespace,
    operation,
    status: ServiceCommandStatus.UNAVAILABLE,
    error: {
      code: options.code,
      message: options.message,
      diagnostics: options.diagnostics ?? {},
    },
  };
}
