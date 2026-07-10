import { randomUUID } from "node:crypto";

import { endpointAddress } from "./endpoint.ts";
import { StateConflict, StateUnavailable, ValidationError } from "./errors.ts";
import {
  cloneJson,
  parseUtcMillis,
  requireJsonObject,
  requirePositiveInteger,
  requireText,
  utcIsoNow,
  type JsonObject,
} from "./json.ts";
import { decodeKeyToken, encodeKeyToken } from "./keys.ts";
import {
  type StateChange,
  type StateEntry,
  type StateStore,
  type StateStorePolicy,
} from "./state.ts";

export const BEACON_ADVERTISEMENT_SCHEMA_ID = "dev.deckr.beacon.advertisement.v1";
export const DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME = "deckr_beacon_advertisement_v1";
export const DEFAULT_BEACON_TTL_SECONDS = 300;
export const BEACON_ADVERTISEMENT_STORE_POLICY: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: DEFAULT_BEACON_TTL_SECONDS,
  allowWriteTtl: true,
  description: "Beacon advertisement state",
});

export const CandidateStatus = Object.freeze({
  CANDIDATE: "candidate",
  MISSING: "missing",
  SCHEMA_INVALID: "schema_invalid",
  FEATURE_MISMATCH: "feature_mismatch",
  SESSION_MISMATCH: "session_mismatch",
  UNAVAILABLE: "unavailable",
});
export type CandidateStatus = (typeof CandidateStatus)[keyof typeof CandidateStatus];

export const BeaconFeatureEventType = Object.freeze({
  ADVERTISED: "advertised",
  UPDATED: "updated",
  WITHDRAWN: "withdrawn",
  EXPIRED: "expired",
  INVALID: "invalid",
});
export type BeaconFeatureEventType =
  (typeof BeaconFeatureEventType)[keyof typeof BeaconFeatureEventType];

export interface BeaconProtocol {
  namespace: string;
  version: string;
}

export interface AdvertisementRecord {
  schema: typeof BEACON_ADVERTISEMENT_SCHEMA_ID;
  advertisementId: string;
  featureId: string;
  advertiser: string;
  endpoint: string;
  sessionId: string;
  refreshSeq: number;
  ttlSeconds: number;
  protocol?: BeaconProtocol;
  operations: string[];
  labels: Record<string, string>;
  hints: JsonObject;
  payload?: JsonObject;
  createdAt?: string;
  updatedAt?: string;
}

export interface AdvertisementHandle {
  key: string;
  advertisementId: string;
  featureId: string;
  advertiser: string;
  endpoint: string;
  sessionId: string;
  revision: number;
  refreshSeq: number;
}

export interface Candidate {
  key: string;
  advertisement: AdvertisementRecord;
  revision: number;
  observedAt: string;
}

export interface BeaconFeatureEvent {
  eventType: BeaconFeatureEventType;
  featureId: string;
  key: string;
  candidate?: Candidate;
  previous?: Candidate;
  reason?: string;
  change?: StateChange;
}

export type AdvertisementFilter = (advertisement: AdvertisementRecord) => boolean;

export function beaconAdvertisementKey(input: {
  featureId: string;
  advertisementId: string;
}): string {
  return [
    "advertisements",
    "by_feature",
    encodeKeyToken(input.featureId),
    encodeKeyToken(input.advertisementId),
  ].join(".");
}

export function parseBeaconAdvertisementKey(key: string): [string, string] | null {
  const parts = key.split(".");
  if (parts.length !== 4 || parts[0] !== "advertisements" || parts[1] !== "by_feature") {
    return null;
  }
  return [decodeKeyToken(parts[2]!), decodeKeyToken(parts[3]!)];
}

export function beaconFeaturePrefix(featureId: string): string {
  return ["advertisements", "by_feature", encodeKeyToken(featureId), ""].join(".");
}

export function validateAdvertisementRecord(value: unknown): AdvertisementRecord {
  const raw = requireJsonObject(value, "Beacon advertisement");
  if (raw.schema !== undefined && raw.schema !== BEACON_ADVERTISEMENT_SCHEMA_ID) {
    throw new ValidationError("Beacon advertisement schema is invalid");
  }
  const record: AdvertisementRecord = {
    schema: BEACON_ADVERTISEMENT_SCHEMA_ID,
    advertisementId: requireText(raw.advertisementId, "advertisementId"),
    featureId: requireText(raw.featureId, "featureId"),
    advertiser: endpointAddress(requireText(raw.advertiser, "advertiser")),
    endpoint: endpointAddress(requireText(raw.endpoint, "endpoint")),
    sessionId: requireText(raw.sessionId, "sessionId"),
    refreshSeq: requirePositiveInteger(raw.refreshSeq, "refreshSeq"),
    ttlSeconds: requirePositiveInteger(raw.ttlSeconds, "ttlSeconds"),
    operations: validateTextList(raw.operations ?? [], "Beacon operation"),
    labels: validateLabels(raw.labels ?? {}),
    hints: requireJsonObject(raw.hints ?? {}, "Beacon hints"),
  };
  if (raw.protocol !== undefined) {
    const protocol = requireJsonObject(raw.protocol, "Beacon protocol");
    record.protocol = {
      namespace: requireText(protocol.namespace, "Beacon protocol namespace"),
      version: requireText(protocol.version, "Beacon protocol version"),
    };
  }
  if (raw.payload !== undefined && raw.payload !== null) {
    record.payload = requireJsonObject(raw.payload, "Beacon payload");
  }
  if (raw.createdAt !== undefined) {
    record.createdAt = requireText(raw.createdAt, "createdAt");
  }
  if (raw.updatedAt !== undefined) {
    record.updatedAt = requireText(raw.updatedAt, "updatedAt");
  }
  beaconAdvertisementKey({
    featureId: record.featureId,
    advertisementId: record.advertisementId,
  });
  return record;
}

export class BeaconDiscovery {
  private readonly state: StateStore;
  private readonly defaultTtlSeconds: number;

  constructor(state: StateStore, options: { defaultTtlSeconds?: number } = {}) {
    this.state = state;
    this.defaultTtlSeconds = options.defaultTtlSeconds ?? DEFAULT_BEACON_TTL_SECONDS;
    if (this.defaultTtlSeconds <= 0) {
      throw new ValidationError("defaultTtlSeconds must be greater than zero");
    }
  }

  async advertise(
    featureId: string,
    endpoint: string,
    sessionId: string,
    options: {
      advertiser?: string;
      advertisementId?: string;
      protocol?: BeaconProtocol;
      operations?: string[];
      labels?: Record<string, string>;
      hints?: JsonObject;
      payload?: JsonObject;
      ttlSeconds?: number;
    } = {},
  ): Promise<AdvertisementHandle> {
    const now = utcIsoNow();
    const record = validateAdvertisementRecord({
      schema: BEACON_ADVERTISEMENT_SCHEMA_ID,
      advertisementId: options.advertisementId ?? randomUUID(),
      featureId,
      advertiser: options.advertiser ?? endpoint,
      endpoint,
      sessionId,
      refreshSeq: 1,
      ttlSeconds: options.ttlSeconds ?? this.defaultTtlSeconds,
      protocol: options.protocol,
      operations: options.operations ?? [],
      labels: options.labels ?? {},
      hints: options.hints ?? {},
      payload: options.payload,
      createdAt: now,
      updatedAt: now,
    });
    const key = beaconAdvertisementKey({
      featureId: record.featureId,
      advertisementId: record.advertisementId,
    });
    const entry = await this.state.create(key, advertisementToJson(record), {
      ttl: record.ttlSeconds,
    });
    return advertisementHandle(key, record, entry.revision);
  }

  async refresh(
    handle: AdvertisementHandle,
    options: { hints?: JsonObject; labels?: Record<string, string>; payload?: JsonObject } = {},
  ): Promise<AdvertisementHandle> {
    const current = await this.state.get(handle.key);
    if (current === null) {
      throw new StateConflict(`Beacon advertisement ${JSON.stringify(handle.key)} is missing`);
    }
    const record = validateAdvertisementRecord(current.value);
    if (!advertisementMatchesHandle(record, handle)) {
      throw new StateConflict(`Beacon advertisement ${JSON.stringify(handle.key)} changed owner`);
    }
    const refreshed = validateAdvertisementRecord({
      ...record,
      refreshSeq: record.refreshSeq + 1,
      hints: options.hints ?? record.hints,
      labels: options.labels ?? record.labels,
      payload: options.payload ?? record.payload,
      updatedAt: utcIsoNow(),
    });
    const entry = await this.state.update(handle.key, advertisementToJson(refreshed), {
      revision: current.revision,
      ttl: refreshed.ttlSeconds,
    });
    return advertisementHandle(handle.key, refreshed, entry.revision);
  }

  async withdraw(handle: AdvertisementHandle): Promise<boolean> {
    const current = await this.state.get(handle.key);
    if (current === null) {
      return false;
    }
    const record = validateAdvertisementRecord(current.value);
    if (!advertisementMatchesHandle(record, handle)) {
      throw new StateConflict(`Beacon advertisement ${JSON.stringify(handle.key)} changed owner`);
    }
    await this.state.delete(handle.key, { revision: current.revision });
    return true;
  }

  async find(featureId: string, selector?: AdvertisementFilter): Promise<Candidate[]> {
    const candidates: Candidate[] = [];
    for (const entry of await this.state.items(beaconFeaturePrefix(featureId))) {
      const candidate = candidateFromEntry(entry);
      if (candidate === null || candidate.advertisement.featureId !== featureId) {
        continue;
      }
      if (selector !== undefined && !selector(candidate.advertisement)) {
        continue;
      }
      candidates.push(candidate);
    }
    return candidates.sort(candidateNewestSort);
  }

  async validate(
    candidate: Candidate,
    options: { currentSessions?: Record<string, string> } = {},
  ): Promise<CandidateStatus> {
    let entry: StateEntry | null;
    try {
      entry = await this.state.get(candidate.key);
    } catch (error) {
      if (error instanceof StateUnavailable) {
        return CandidateStatus.UNAVAILABLE;
      }
      throw error;
    }
    if (entry === null) {
      return CandidateStatus.MISSING;
    }
    let advertisement: AdvertisementRecord;
    try {
      advertisement = validateAdvertisementRecord(entry.value);
    } catch {
      return CandidateStatus.SCHEMA_INVALID;
    }
    if (advertisement.featureId !== candidate.advertisement.featureId) {
      return CandidateStatus.FEATURE_MISMATCH;
    }
    const currentSession = options.currentSessions?.[advertisement.advertiser];
    if (currentSession !== undefined && currentSession !== advertisement.sessionId) {
      return CandidateStatus.SESSION_MISMATCH;
    }
    return CandidateStatus.CANDIDATE;
  }

  watch(featureId: string): AsyncIterable<StateChange> {
    if (this.state.watch === undefined) {
      throw new StateUnavailable("State store does not support watch");
    }
    return this.state.watch(beaconFeaturePrefix(featureId));
  }
}

export interface BeaconAdvertisementSpec {
  featureId: string;
  endpoint: string;
  sessionId: string;
  advertiser?: string;
  advertisementId?: string;
  protocol?: BeaconProtocol;
  operations?: string[];
  labels?: Record<string, string>;
  hints?: JsonObject;
  payload?: JsonObject;
  ttlSeconds?: number;
  refreshIntervalSeconds?: number;
}

export class BeaconAdvertisement {
  readonly spec: BeaconAdvertisementSpec;

  private readonly service: BeaconService;
  private handleValue: AdvertisementHandle | null = null;
  private closedValue = false;

  constructor(service: BeaconService, spec: BeaconAdvertisementSpec) {
    this.service = service;
    this.spec = {
      ...spec,
      endpoint: endpointAddress(spec.endpoint),
      advertiser: spec.advertiser === undefined ? undefined : endpointAddress(spec.advertiser),
      sessionId: requireText(spec.sessionId, "Beacon session id"),
      featureId: requireText(spec.featureId, "Beacon feature id"),
      refreshIntervalSeconds: spec.refreshIntervalSeconds ?? 5,
    };
    if (this.spec.refreshIntervalSeconds! <= 0) {
      throw new ValidationError("refreshIntervalSeconds must be greater than zero");
    }
  }

  get closed(): boolean {
    return this.closedValue;
  }

  get handle(): AdvertisementHandle | null {
    return this.handleValue;
  }

  async publish(options: {
    payload?: JsonObject;
    labels?: Record<string, string>;
    hints?: JsonObject;
  } = {}): Promise<AdvertisementHandle> {
    if (this.closedValue) {
      throw new StateConflict("Beacon advertisement is closed");
    }
    if (this.handleValue === null) {
      this.handleValue = await this.service.advertise(this.spec, options);
      return this.handleValue;
    }
    try {
      this.handleValue = await this.service.refresh(this.handleValue, options);
      return this.handleValue;
    } catch (error) {
      if (!(error instanceof StateConflict)) {
        throw error;
      }
      this.handleValue = await this.service.advertise(this.spec, options);
      return this.handleValue;
    }
  }

  async close(): Promise<void> {
    this.closedValue = true;
    const handle = this.handleValue;
    this.handleValue = null;
    if (handle !== null) {
      await this.service.withdraw(handle);
    }
    this.service.forgetAdvertisement(this);
  }
}

export class BeaconService {
  private readonly discovery: BeaconDiscovery;
  private readonly advertisements = new Map<string, BeaconAdvertisement>();

  constructor(discovery: BeaconDiscovery) {
    this.discovery = discovery;
  }

  async ensureAdvertisement(spec: BeaconAdvertisementSpec): Promise<BeaconAdvertisement> {
    const key = advertisementCacheKey(spec);
    const existing = this.advertisements.get(key);
    if (existing !== undefined && !existing.closed) {
      return existing;
    }
    const advertisement = new BeaconAdvertisement(this, spec);
    this.advertisements.set(key, advertisement);
    return advertisement;
  }

  async find(featureId: string, selector?: AdvertisementFilter): Promise<Candidate[]> {
    return this.discovery.find(featureId, selector);
  }

  async validate(
    candidate: Candidate,
    options: { currentSessions?: Record<string, string> } = {},
  ): Promise<CandidateStatus> {
    return this.discovery.validate(candidate, options);
  }

  watchFeature(featureId: string): AsyncIterable<BeaconFeatureEvent> {
    return beaconFeatureEvents(featureId, this.discovery.watch(featureId));
  }

  async advertise(
    spec: BeaconAdvertisementSpec,
    overrides: { payload?: JsonObject; labels?: Record<string, string>; hints?: JsonObject } = {},
  ): Promise<AdvertisementHandle> {
    return this.discovery.advertise(spec.featureId, spec.endpoint, spec.sessionId, {
      advertiser: spec.advertiser,
      advertisementId: spec.advertisementId,
      protocol: spec.protocol,
      operations: spec.operations,
      labels: overrides.labels ?? spec.labels,
      hints: overrides.hints ?? spec.hints,
      payload: overrides.payload ?? spec.payload,
      ttlSeconds: spec.ttlSeconds,
    });
  }

  async refresh(
    handle: AdvertisementHandle,
    options: { hints?: JsonObject; labels?: Record<string, string>; payload?: JsonObject } = {},
  ): Promise<AdvertisementHandle> {
    return this.discovery.refresh(handle, options);
  }

  async withdraw(handle: AdvertisementHandle): Promise<boolean> {
    return this.discovery.withdraw(handle);
  }

  forgetAdvertisement(advertisement: BeaconAdvertisement): void {
    for (const [key, current] of this.advertisements) {
      if (current === advertisement) {
        this.advertisements.delete(key);
      }
    }
  }
}

export function candidateFromEntry(entry: StateEntry): Candidate | null {
  let advertisement: AdvertisementRecord;
  try {
    advertisement = validateAdvertisementRecord(entry.value);
  } catch {
    return null;
  }
  const parsed = parseBeaconAdvertisementKey(entry.key);
  if (parsed === null) {
    return null;
  }
  const [featureId, advertisementId] = parsed;
  if (
    featureId !== advertisement.featureId ||
    advertisementId !== advertisement.advertisementId
  ) {
    return null;
  }
  return {
    key: entry.key,
    advertisement,
    revision: entry.revision,
    observedAt: utcIsoNow(),
  };
}

async function* beaconFeatureEvents(
  featureId: string,
  changes: AsyncIterable<StateChange>,
): AsyncIterable<BeaconFeatureEvent> {
  const known = new Map<string, Candidate>();
  for await (const change of changes) {
    const event = beaconFeatureEvent(featureId, change, known);
    if (event !== null) {
      yield event;
    }
  }
}

function beaconFeatureEvent(
  featureId: string,
  change: StateChange,
  known: Map<string, Candidate>,
): BeaconFeatureEvent | null {
  if (change.operation === "put" && change.entry !== undefined) {
    const previous = known.get(change.key);
    const candidate = candidateFromEntry(change.entry);
    if (candidate === null || candidate.advertisement.featureId !== featureId) {
      known.delete(change.key);
      return {
        eventType: BeaconFeatureEventType.INVALID,
        featureId,
        key: change.key,
        ...(previous === undefined ? {} : { previous }),
        reason: "invalid_advertisement",
        change,
      };
    }
    known.set(change.key, candidate);
    return {
      eventType:
        previous === undefined
          ? BeaconFeatureEventType.ADVERTISED
          : BeaconFeatureEventType.UPDATED,
      featureId,
      key: change.key,
      candidate,
      ...(previous === undefined ? {} : { previous }),
      change,
    };
  }
  if (change.operation === "delete" || change.operation === "expire") {
    const previous = known.get(change.key);
    known.delete(change.key);
    return {
      eventType:
        change.operation === "expire"
          ? BeaconFeatureEventType.EXPIRED
          : BeaconFeatureEventType.WITHDRAWN,
      featureId,
      key: change.key,
      ...(previous === undefined ? {} : { previous }),
      change,
    };
  }
  return null;
}

function advertisementHandle(
  key: string,
  record: AdvertisementRecord,
  revision: number,
): AdvertisementHandle {
  return {
    key,
    advertisementId: record.advertisementId,
    featureId: record.featureId,
    advertiser: record.advertiser,
    endpoint: record.endpoint,
    sessionId: record.sessionId,
    revision,
    refreshSeq: record.refreshSeq,
  };
}

function advertisementMatchesHandle(record: AdvertisementRecord, handle: AdvertisementHandle): boolean {
  return (
    record.advertisementId === handle.advertisementId &&
    record.featureId === handle.featureId &&
    record.advertiser === handle.advertiser &&
    record.endpoint === handle.endpoint &&
    record.sessionId === handle.sessionId
  );
}

function candidateNewestSort(left: Candidate, right: Candidate): number {
  return (
    right.revision - left.revision ||
    parseUtcMillis(right.advertisement.updatedAt) - parseUtcMillis(left.advertisement.updatedAt) ||
    right.advertisement.refreshSeq - left.advertisement.refreshSeq ||
    left.advertisement.advertisementId.localeCompare(right.advertisement.advertisementId)
  );
}

function advertisementCacheKey(spec: BeaconAdvertisementSpec): string {
  return JSON.stringify({
    featureId: spec.featureId,
    endpoint: endpointAddress(spec.endpoint),
    advertiser: spec.advertiser === undefined ? endpointAddress(spec.endpoint) : endpointAddress(spec.advertiser),
    sessionId: spec.sessionId,
    advertisementId: spec.advertisementId ?? null,
    operations: spec.operations ?? [],
  });
}

function validateTextList(value: unknown, fieldName: string): string[] {
  if (!Array.isArray(value)) {
    throw new ValidationError(`${fieldName} list must be an array`);
  }
  return value.map((item) => requireText(item, fieldName));
}

function validateLabels(value: unknown): Record<string, string> {
  const raw = requireJsonObject(value, "Beacon labels");
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(raw)) {
    out[requireText(key, "Beacon label key")] = requireText(item, "Beacon label value");
  }
  return out;
}

export function advertisementToJson(record: AdvertisementRecord): JsonObject {
  return cloneJson(record as unknown as JsonObject);
}
