import { randomUUID } from "node:crypto";

import { endpointAddress } from "./endpoint.ts";
import { StateConflict, StateUnavailable, ValidationError } from "./errors.ts";
import {
  canonicalJsonHash,
  cloneJson,
  requireJsonObject,
  requirePositiveInteger,
  requireText,
  utcIsoNow,
  type JsonObject,
  type JsonValue,
} from "./json.ts";
import { decodeKeyToken, encodeKeyToken } from "./keys.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateChange,
  type StateEntry,
  type StateStore,
  type StateStorePolicy,
} from "./state.ts";

export { canonicalJson, canonicalJsonBytes, canonicalJsonHash } from "./json.ts";

export const CONCORD_CONTRACT_SCHEMA_ID = "dev.deckr.concord.contract.v1";
export const CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID =
  "dev.deckr.concord.participant-token.v1";
export const CONCORD_STALE_OBSERVATION_SCHEMA_ID =
  "dev.deckr.concord.stale-observation.v1";

export const DEFAULT_CONCORD_CONTRACT_STORE_NAME = "deckr_concord_contract_v1";
export const DEFAULT_CONCORD_TOKEN_STORE_NAME = "deckr_concord_token_v1";
export const DEFAULT_CONCORD_MAINTENANCE_STORE_NAME = "deckr_concord_maintenance_v1";
export const DEFAULT_CONCORD_TOKEN_TTL_SECONDS = 30;
export const DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS = 900;
export const DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS = 3600;
export const DEFAULT_CONCORD_REAPER_SCAN_INTERVAL_SECONDS = 60;
export const CONCORD_MAINTENANCE_ACTOR = "concord:maintenance";
export const CONCORD_REAPER_STALE_CONTRACT_REASON = "concord_reaper_stale_contract";
export const ACTION_PROVIDER_SESSION_PROFILE_ID =
  "dev.deckr.profile.action_provider_session.v1";

export const CONCORD_CONTRACT_STORE_POLICY: StateStorePolicy =
  PERSISTENT_STATE_STORE_POLICY;
export const CONCORD_MAINTENANCE_STORE_POLICY: StateStorePolicy =
  PERSISTENT_STATE_STORE_POLICY;
export const CONCORD_TOKEN_STORE_POLICY: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: DEFAULT_CONCORD_TOKEN_TTL_SECONDS,
  allowWriteTtl: true,
  description: "Concord participant token state",
});

export const ContractState = Object.freeze({
  OPEN: "open",
  CANCELLED: "cancelled",
});
export type ContractState = (typeof ContractState)[keyof typeof ContractState];

export const ContractValidityStatus = Object.freeze({
  VALID: "valid",
  NOT_YET_FULFILLED: "not_yet_fulfilled",
  CANCELLED: "cancelled",
  MISSING_CONTRACT: "missing_contract",
  INVALID_CONTRACT: "invalid_contract",
  INVALID_TOKEN: "invalid_token",
  MISSING_TOKEN: "missing_token",
  GENERATION_MISMATCH: "generation_mismatch",
  SESSION_MISMATCH: "session_mismatch",
  TERMS_HASH_MISMATCH: "terms_hash_mismatch",
  UNAVAILABLE: "unavailable",
});
export type ContractValidityStatus =
  (typeof ContractValidityStatus)[keyof typeof ContractValidityStatus];

export const ConcordManagedContractEventType = Object.freeze({
  VALID: "valid",
  PENDING: "pending",
  INVALID: "invalid",
  CANCELLED: "cancelled",
  RELEASED: "released",
});
export type ConcordManagedContractEventType =
  (typeof ConcordManagedContractEventType)[keyof typeof ConcordManagedContractEventType];

export interface ContractPointer {
  contractId: string;
  generation: number;
}

export interface TokenObservation {
  generation: number;
  refreshSeq?: number;
  revision?: number;
  tokenHash?: string;
}

export interface ConcordStaleObservationRecord {
  schema: typeof CONCORD_STALE_OBSERVATION_SCHEMA_ID;
  contractId: string;
  generation: number;
  firstObservedStaleAt: string;
  status: ContractValidityStatus;
  reason?: string;
  contractRevision?: number;
}

export interface ContractRecord {
  schema: typeof CONCORD_CONTRACT_SCHEMA_ID;
  contractId: string;
  generation: number;
  participants: string[];
  attachedParticipants: string[];
  state: ContractState;
  profile?: string;
  termsHash?: string;
  terms?: JsonObject;
  createdBy?: string;
  createdAt?: string;
  cancelledBy?: string;
  cancelledAt?: string;
  cancelRevision?: number;
  cancelReason?: string;
  supersedes?: ContractPointer;
}

export interface ParticipantTokenRecord {
  schema: typeof CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID;
  contractId: string;
  generation: number;
  participant: string;
  sessionId: string;
  tokenId: string;
  refreshSeq: number;
  ttlSeconds: number;
  termsHash?: string;
  contractHash?: string;
  observed: Record<string, TokenObservation>;
}

export interface ContractHandle {
  key: string;
  contractId: string;
  generation: number;
  participants: string[];
  attachedParticipants: string[];
  revision: number;
  state: ContractState;
  profile?: string;
  termsHash?: string;
}

export interface ParticipantHandle {
  key: string;
  contractId: string;
  generation: number;
  participant: string;
  sessionId: string;
  tokenId: string;
  revision: number;
  refreshSeq: number;
  ttlSeconds: number;
  termsHash?: string;
}

export interface ContractValidity {
  status: ContractValidityStatus;
  contract?: ContractRecord;
  tokens: Record<string, ParticipantHandle>;
  reason?: string;
  valid: boolean;
}

export interface ConcordMaintenanceDeletionResult {
  deleted: boolean;
  deletedTokenKeyCount: number;
}

export interface ConcordReaperScanResult {
  scannedContractCount: number;
  staleObservationCount: number;
  staleObservationsCreated: number;
  staleObservationsCleared: number;
  contractsCancelled: number;
  contractsDeleted: number;
  tokenKeysDeleted: number;
}

export interface ConcordManagedContract {
  contract: ContractHandle;
  record: ContractRecord;
  validity: ContractValidity;
  token?: ParticipantHandle;
}

export interface ConcordManagedContractEvent {
  eventType: ConcordManagedContractEventType;
  contract: ContractHandle;
  record?: ContractRecord;
  validity?: ContractValidity;
  token?: ParticipantHandle;
  reason?: string;
}

export function concordContractKey(input: {
  contractId: string;
  generation: number;
}): string {
  return ["contracts", encodeKeyToken(input.contractId), String(input.generation), "meta"].join(".");
}

export function parseConcordContractKey(key: string): [string, number] | null {
  const parts = key.split(".");
  if (parts.length !== 4 || parts[0] !== "contracts" || parts[3] !== "meta") {
    return null;
  }
  const generation = Number(parts[2]);
  if (!Number.isInteger(generation)) {
    return null;
  }
  return [decodeKeyToken(parts[1]!), generation];
}

export function concordParticipantTokenKey(input: {
  contractId: string;
  generation: number;
  participant: string;
}): string {
  return [
    "contracts",
    encodeKeyToken(input.contractId),
    String(input.generation),
    "participants",
    encodeKeyToken(endpointAddress(input.participant)),
  ].join(".");
}

export function parseConcordParticipantTokenKey(key: string): [string, number, string] | null {
  const parts = key.split(".");
  if (parts.length !== 5 || parts[0] !== "contracts" || parts[3] !== "participants") {
    return null;
  }
  const generation = Number(parts[2]);
  if (!Number.isInteger(generation)) {
    return null;
  }
  return [decodeKeyToken(parts[1]!), generation, endpointAddress(decodeKeyToken(parts[4]!))];
}

export function concordContractPrefix(input: { contractId: string; generation: number }): string {
  return ["contracts", encodeKeyToken(input.contractId), String(input.generation), ""].join(".");
}

export function concordContractsPrefix(): string {
  return "contracts.";
}

export function concordContractIdPrefix(input: { contractId: string }): string {
  return ["contracts", encodeKeyToken(input.contractId), ""].join(".");
}

export function concordStaleObservationKey(input: {
  contractId: string;
  generation: number;
}): string {
  return ["stale", encodeKeyToken(input.contractId), String(input.generation)].join(".");
}

export function validateContractPointer(value: unknown): ContractPointer {
  const raw = requireJsonObject(value, "Concord contract pointer");
  return {
    contractId: requireText(raw.contractId, "contractId"),
    generation: requirePositiveInteger(raw.generation, "generation"),
  };
}

export function validateContractRecord(value: unknown): ContractRecord {
  const raw = requireJsonObject(value, "Concord contract");
  if (raw.schema !== undefined && raw.schema !== CONCORD_CONTRACT_SCHEMA_ID) {
    throw new ValidationError("Concord contract schema is invalid");
  }
  const participants = validateEndpointList(raw.participants, "participants");
  if (participants.length === 0) {
    throw new ValidationError("Concord contracts require at least one participant");
  }
  requireCanonicalUnique(participants, "Concord contract participants");
  const attachedParticipants = validateEndpointList(
    raw.attachedParticipants ?? [],
    "attachedParticipants",
  );
  requireCanonicalUnique(attachedParticipants, "Concord attached participants");
  const participantSet = new Set(participants);
  for (const participant of attachedParticipants) {
    if (!participantSet.has(participant)) {
      throw new ValidationError("attachedParticipants must be a subset of participants");
    }
  }
  const state = raw.state === undefined ? ContractState.OPEN : requireText(raw.state, "state");
  if (state !== ContractState.OPEN && state !== ContractState.CANCELLED) {
    throw new ValidationError("Concord contract state must be open or cancelled");
  }
  const record: ContractRecord = {
    schema: CONCORD_CONTRACT_SCHEMA_ID,
    contractId: requireText(raw.contractId, "contractId"),
    generation: requirePositiveInteger(raw.generation, "generation"),
    participants,
    attachedParticipants,
    state,
  };
  setOptionalText(record, "profile", raw.profile, "profile");
  setOptionalText(record, "termsHash", raw.termsHash, "termsHash");
  if (raw.terms !== undefined && raw.terms !== null) {
    record.terms = requireJsonObject(raw.terms, "terms");
    if (record.termsHash === undefined) {
      throw new ValidationError("Concord contract terms require termsHash");
    }
    if (record.termsHash !== canonicalJsonHash(record.terms as JsonValue)) {
      throw new ValidationError("Concord contract termsHash does not match terms");
    }
    const termsProfile = record.terms.profile;
    if (record.profile !== undefined && termsProfile !== undefined && termsProfile !== record.profile) {
      throw new ValidationError("Concord contract profile must match terms.profile");
    }
  }
  setOptionalEndpoint(record, "createdBy", raw.createdBy);
  setOptionalText(record, "createdAt", raw.createdAt, "createdAt");
  if (raw.cancelledBy !== undefined) {
    record.cancelledBy =
      raw.cancelledBy === CONCORD_MAINTENANCE_ACTOR
        ? CONCORD_MAINTENANCE_ACTOR
        : endpointAddress(requireText(raw.cancelledBy, "cancelledBy"));
  }
  setOptionalText(record, "cancelledAt", raw.cancelledAt, "cancelledAt");
  if (raw.cancelRevision !== undefined) {
    if (!Number.isInteger(raw.cancelRevision) || Number(raw.cancelRevision) < 0) {
      throw new ValidationError("cancelRevision must be non-negative");
    }
    record.cancelRevision = Number(raw.cancelRevision);
  }
  setOptionalText(record, "cancelReason", raw.cancelReason, "cancelReason");
  if (raw.supersedes !== undefined && raw.supersedes !== null) {
    record.supersedes = validateContractPointer(raw.supersedes);
  }
  return record;
}

export function validateParticipantTokenRecord(value: unknown): ParticipantTokenRecord {
  const raw = requireJsonObject(value, "Concord participant token");
  if (raw.schema !== undefined && raw.schema !== CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID) {
    throw new ValidationError("Concord participant token schema is invalid");
  }
  const observedRaw = requireJsonObject(raw.observed ?? {}, "observed");
  const observed: Record<string, TokenObservation> = {};
  for (const [key, item] of Object.entries(observedRaw)) {
    const observation = requireJsonObject(item, "token observation");
    const record: TokenObservation = {
      generation: requirePositiveInteger(observation.generation, "token observation generation"),
    };
    if (observation.refreshSeq !== undefined) {
      record.refreshSeq = nonNegativeInteger(observation.refreshSeq, "refreshSeq");
    }
    if (observation.revision !== undefined) {
      record.revision = nonNegativeInteger(observation.revision, "revision");
    }
    if (observation.tokenHash !== undefined) {
      record.tokenHash = requireText(observation.tokenHash, "tokenHash");
    }
    observed[key] = record;
  }
  const record: ParticipantTokenRecord = {
    schema: CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
    contractId: requireText(raw.contractId, "contractId"),
    generation: requirePositiveInteger(raw.generation, "generation"),
    participant: endpointAddress(requireText(raw.participant, "participant")),
    sessionId: requireText(raw.sessionId, "sessionId"),
    tokenId: requireText(raw.tokenId, "tokenId"),
    refreshSeq: requirePositiveInteger(raw.refreshSeq, "refreshSeq"),
    ttlSeconds: requirePositiveInteger(raw.ttlSeconds, "ttlSeconds"),
    observed,
  };
  setOptionalText(record, "termsHash", raw.termsHash, "termsHash");
  setOptionalText(record, "contractHash", raw.contractHash, "contractHash");
  return record;
}

export function validateStaleObservationRecord(value: unknown): ConcordStaleObservationRecord {
  const raw = requireJsonObject(value, "Concord stale observation");
  if (raw.schema !== undefined && raw.schema !== CONCORD_STALE_OBSERVATION_SCHEMA_ID) {
    throw new ValidationError("Concord stale observation schema is invalid");
  }
  const status = requireText(raw.status, "status") as ContractValidityStatus;
  if (!Object.values(ContractValidityStatus).includes(status)) {
    throw new ValidationError("invalid Concord stale observation status");
  }
  const record: ConcordStaleObservationRecord = {
    schema: CONCORD_STALE_OBSERVATION_SCHEMA_ID,
    contractId: requireText(raw.contractId, "contractId"),
    generation: requirePositiveInteger(raw.generation, "generation"),
    firstObservedStaleAt: requireText(raw.firstObservedStaleAt, "firstObservedStaleAt"),
    status,
  };
  setOptionalText(record, "reason", raw.reason, "reason");
  if (raw.contractRevision !== undefined) {
    record.contractRevision = nonNegativeInteger(raw.contractRevision, "contractRevision");
  }
  return record;
}

export class ConcordCoordinator {
  private readonly contractState: StateStore;
  private readonly tokenState: StateStore;
  private readonly tokenTtlSeconds: number;

  constructor(
    contractState: StateStore,
    tokenState: StateStore,
    options: { tokenTtlSeconds?: number } = {},
  ) {
    this.contractState = contractState;
    this.tokenState = tokenState;
    this.tokenTtlSeconds = options.tokenTtlSeconds ?? DEFAULT_CONCORD_TOKEN_TTL_SECONDS;
    if (this.tokenTtlSeconds <= 0) {
      throw new ValidationError("tokenTtlSeconds must be greater than zero");
    }
  }

  async createContract(
    participants: string[],
    options: {
      contractId?: string;
      generation?: number;
      profile?: string;
      terms?: JsonObject;
      createdBy?: string;
      supersedes?: ContractPointer;
    } = {},
  ): Promise<ContractHandle> {
    const parsedParticipants = participants.map(endpointAddress).sort();
    requireCanonicalUnique(parsedParticipants, "Concord contract participants");
    if (parsedParticipants.length === 0) {
      throw new ValidationError("Concord contracts require at least one participant");
    }
    const terms = options.terms === undefined ? undefined : cloneJson(options.terms);
    const termsHash = terms === undefined ? undefined : canonicalJsonHash(terms as JsonValue);
    const now = utcIsoNow();
    const record = validateContractRecord({
      schema: CONCORD_CONTRACT_SCHEMA_ID,
      contractId: options.contractId ?? randomUUID(),
      generation: options.generation ?? 1,
      participants: parsedParticipants,
      attachedParticipants: [],
      state: ContractState.OPEN,
      profile: options.profile,
      terms,
      termsHash,
      createdBy: options.createdBy,
      createdAt: now,
      supersedes: options.supersedes,
    });
    const key = concordContractKey({
      contractId: record.contractId,
      generation: record.generation,
    });
    const entry = await this.contractState.create(key, recordToJson(record));
    return contractHandle(key, record, entry.revision);
  }

  async getContract(pointer: ContractPointer): Promise<ContractHandle | null> {
    const key = concordContractKey(pointer);
    const entry = await this.contractState.get(key);
    if (entry === null) {
      return null;
    }
    const record = validateContractRecord(entry.value);
    if (record.contractId !== pointer.contractId || record.generation !== pointer.generation) {
      return null;
    }
    return contractHandle(key, record, entry.revision);
  }

  async contractRecord(contract: ContractHandle): Promise<ContractRecord | null> {
    const entry = await this.contractState.get(contract.key);
    if (entry === null) {
      return null;
    }
    const record = validateContractRecord(entry.value);
    if (record.contractId !== contract.contractId || record.generation !== contract.generation) {
      return null;
    }
    return record;
  }

  async contracts(
    profile?: string,
    options: { contractId?: string; participant?: string; state?: ContractState } = {},
  ): Promise<ContractHandle[]> {
    const participant =
      options.participant === undefined ? undefined : endpointAddress(options.participant);
    const prefix =
      options.contractId === undefined
        ? concordContractsPrefix()
        : concordContractIdPrefix({ contractId: requireText(options.contractId, "contractId") });
    const contracts: ContractHandle[] = [];
    for (const entry of await this.contractState.items(prefix)) {
      const parsed = parseConcordContractKey(entry.key);
      if (parsed === null) {
        continue;
      }
      const [contractId, generation] = parsed;
      if (options.contractId !== undefined && contractId !== options.contractId) {
        continue;
      }
      let record: ContractRecord;
      try {
        record = validateContractRecord(entry.value);
      } catch {
        continue;
      }
      if (profile !== undefined && record.profile !== profile) {
        continue;
      }
      if (participant !== undefined && !record.participants.includes(participant)) {
        continue;
      }
      if (options.state !== undefined && record.state !== options.state) {
        continue;
      }
      if (record.contractId !== contractId || record.generation !== generation) {
        continue;
      }
      contracts.push(contractHandle(entry.key, record, entry.revision));
    }
    return contracts.sort((left, right) => left.key.localeCompare(right.key));
  }

  watchContracts(): AsyncIterable<StateChange> {
    if (this.contractState.watch === undefined) {
      throw new StateUnavailable("Contract state store does not support watch");
    }
    return this.contractState.watch(concordContractsPrefix());
  }

  async attach(
    contract: ContractHandle,
    participant: string,
    sessionId: string,
    options: { tokenId?: string; ttlSeconds?: number } = {},
  ): Promise<ParticipantHandle> {
    const current = await this.contractState.get(contract.key);
    if (current === null) {
      throw new StateConflict(`Concord contract ${JSON.stringify(contract.key)} is missing`);
    }
    const record = validateContractRecord(current.value);
    if (record.state === ContractState.CANCELLED) {
      throw new StateConflict(`Concord contract ${JSON.stringify(contract.key)} is cancelled`);
    }
    const parsedParticipant = endpointAddress(participant);
    if (!record.participants.includes(parsedParticipant)) {
      throw new ValidationError("participant is not named by the Concord contract");
    }
    if (record.attachedParticipants.includes(parsedParticipant)) {
      throw new StateConflict("Concord participant is already attached");
    }
    const ttlSeconds = options.ttlSeconds ?? this.tokenTtlSeconds;
    const token = validateParticipantTokenRecord({
      schema: CONCORD_PARTICIPANT_TOKEN_SCHEMA_ID,
      contractId: record.contractId,
      generation: record.generation,
      participant: parsedParticipant,
      sessionId,
      tokenId: options.tokenId ?? randomUUID(),
      refreshSeq: 1,
      ttlSeconds,
      termsHash: record.termsHash,
      observed: {},
    });
    const key = concordParticipantTokenKey({
      contractId: record.contractId,
      generation: record.generation,
      participant: parsedParticipant,
    });
    let entry: StateEntry;
    try {
      entry = await this.tokenState.create(key, recordToJson(token), {
        ttl: token.ttlSeconds,
      });
    } catch (error) {
      if (!(error instanceof StateConflict)) {
        throw error;
      }
      const tokenEntry = await this.tokenState.get(key);
      if (tokenEntry === null) {
        throw new StateConflict("Concord participant token changed during attach");
      }
      const existing = validateParticipantTokenRecord(tokenEntry.value);
      if (!tokenMatchesAttachRequest(existing, record, parsedParticipant, sessionId, options.tokenId)) {
        throw new StateConflict("Concord participant token already exists");
      }
      entry = tokenEntry;
    }
    await this.markParticipantAttached(contract.key, parsedParticipant);
    return participantHandle(key, token, entry.revision);
  }

  async refresh(handle: ParticipantHandle): Promise<ParticipantHandle> {
    const contractEntry = await this.contractState.get(
      concordContractKey({ contractId: handle.contractId, generation: handle.generation }),
    );
    if (contractEntry === null) {
      throw new StateConflict("Concord contract is missing");
    }
    const contract = validateContractRecord(contractEntry.value);
    if (contract.state === ContractState.CANCELLED) {
      throw new StateConflict("Concord contract is cancelled");
    }
    const tokenEntry = await this.tokenState.get(handle.key);
    if (tokenEntry === null) {
      throw new StateConflict("Concord participant token is missing");
    }
    const token = validateParticipantTokenRecord(tokenEntry.value);
    if (!tokenMatchesHandle(token, handle)) {
      throw new StateConflict("Concord participant token changed owner");
    }
    const refreshed = validateParticipantTokenRecord({
      ...token,
      refreshSeq: token.refreshSeq + 1,
    });
    try {
      const entry = await this.tokenState.update(handle.key, recordToJson(refreshed), {
        revision: tokenEntry.revision,
        ttl: refreshed.ttlSeconds,
      });
      return participantHandle(handle.key, refreshed, entry.revision);
    } catch (error) {
      if (!(error instanceof StateConflict) || !String(error.message).includes("revision changed")) {
        throw error;
      }
      const latestEntry = await this.tokenState.get(handle.key);
      if (latestEntry === null) {
        throw new StateConflict("Concord participant token is missing");
      }
      const latest = validateParticipantTokenRecord(latestEntry.value);
      if (!tokenMatchesHandle(latest, handle)) {
        throw new StateConflict("Concord participant token changed owner");
      }
      return participantHandle(handle.key, latest, latestEntry.revision);
    }
  }

  async cancel(
    contract: ContractHandle,
    participant: string,
    options: { reason?: string } = {},
  ): Promise<boolean> {
    const current = await this.contractState.get(contract.key);
    if (current === null) {
      return false;
    }
    const record = validateContractRecord(current.value);
    if (record.state === ContractState.CANCELLED) {
      return false;
    }
    const parsedParticipant = endpointAddress(participant);
    if (!record.participants.includes(parsedParticipant)) {
      throw new ValidationError("participant is not named by the Concord contract");
    }
    const cancelled = validateContractRecord({
      ...record,
      state: ContractState.CANCELLED,
      cancelledBy: parsedParticipant,
      cancelledAt: utcIsoNow(),
      cancelRevision: current.revision,
      cancelReason: options.reason,
    });
    await this.contractState.update(contract.key, recordToJson(cancelled), {
      revision: current.revision,
    });
    return true;
  }

  async maintenanceCancel(
    contract: ContractHandle,
    options: { reason?: string; now?: string } = {},
  ): Promise<boolean> {
    const current = await this.contractState.get(contract.key);
    if (current === null) {
      return false;
    }
    const record = validateContractRecord(current.value);
    if (record.contractId !== contract.contractId || record.generation !== contract.generation) {
      throw new StateConflict(`Concord contract ${JSON.stringify(contract.key)} changed identity`);
    }
    if (record.state === ContractState.CANCELLED) {
      return false;
    }
    const cancelled = validateContractRecord({
      ...record,
      state: ContractState.CANCELLED,
      cancelledBy: CONCORD_MAINTENANCE_ACTOR,
      cancelledAt: options.now ?? utcIsoNow(),
      cancelRevision: current.revision,
      cancelReason: options.reason ?? CONCORD_REAPER_STALE_CONTRACT_REASON,
    });
    await this.contractState.update(contract.key, recordToJson(cancelled), {
      revision: current.revision,
    });
    return true;
  }

  async validate(
    contract: ContractHandle,
    options: { currentSessions?: Record<string, string> } = {},
  ): Promise<ContractValidity> {
    let contractEntry: StateEntry | null;
    try {
      contractEntry = await this.contractState.get(contract.key);
    } catch (error) {
      if (error instanceof StateUnavailable) {
        return validity(ContractValidityStatus.UNAVAILABLE);
      }
      throw error;
    }
    if (contractEntry === null) {
      return validity(ContractValidityStatus.MISSING_CONTRACT);
    }
    let record: ContractRecord;
    try {
      record = validateContractRecord(contractEntry.value);
    } catch (error) {
      return validity(ContractValidityStatus.INVALID_CONTRACT, {
        reason: error instanceof Error ? error.message : String(error),
      });
    }
    if (record.state === ContractState.CANCELLED) {
      return validity(ContractValidityStatus.CANCELLED, { contract: record });
    }
    const attached = new Set(record.attachedParticipants);
    const tokens: Record<string, ParticipantHandle> = {};
    let pendingParticipant: string | undefined;
    for (const participant of record.participants) {
      const tokenKey = concordParticipantTokenKey({
        contractId: record.contractId,
        generation: record.generation,
        participant,
      });
      let tokenEntry: StateEntry | null;
      try {
        tokenEntry = await this.tokenState.get(tokenKey);
      } catch (error) {
        if (error instanceof StateUnavailable) {
          return validity(ContractValidityStatus.UNAVAILABLE, { contract: record });
        }
        throw error;
      }
      if (tokenEntry === null) {
        if (attached.has(participant)) {
          return validity(ContractValidityStatus.MISSING_TOKEN, {
            contract: record,
            tokens,
            reason: participant,
          });
        }
        pendingParticipant ??= participant;
        continue;
      }
      let token: ParticipantTokenRecord;
      try {
        token = validateParticipantTokenRecord(tokenEntry.value);
      } catch (error) {
        return validity(ContractValidityStatus.INVALID_TOKEN, {
          contract: record,
          tokens,
          reason: error instanceof Error ? error.message : String(error),
        });
      }
      const status = tokenValidityStatus(token, record, participant, options.currentSessions);
      tokens[participant] = participantHandle(tokenKey, token, tokenEntry.revision);
      if (status !== null) {
        return validity(status, { contract: record, tokens, reason: participant });
      }
      if (!attached.has(participant)) {
        pendingParticipant ??= participant;
      }
    }
    if (pendingParticipant !== undefined) {
      return validity(ContractValidityStatus.NOT_YET_FULFILLED, {
        contract: record,
        tokens,
        reason: pendingParticipant,
      });
    }
    return validity(ContractValidityStatus.VALID, { contract: record, tokens });
  }

  tokenStore(): StateStore {
    return this.tokenState;
  }

  contractStore(): StateStore {
    return this.contractState;
  }

  private async markParticipantAttached(contractKey: string, participant: string): Promise<void> {
    while (true) {
      const current = await this.contractState.get(contractKey);
      if (current === null) {
        throw new StateConflict(`Concord contract ${JSON.stringify(contractKey)} is missing`);
      }
      const record = validateContractRecord(current.value);
      if (record.state === ContractState.CANCELLED) {
        throw new StateConflict(`Concord contract ${JSON.stringify(contractKey)} is cancelled`);
      }
      if (!record.participants.includes(participant)) {
        throw new StateConflict("participant is not named by the Concord contract");
      }
      if (record.attachedParticipants.includes(participant)) {
        return;
      }
      const attachedParticipants = [...record.attachedParticipants, participant].sort();
      const updated = validateContractRecord({ ...record, attachedParticipants });
      try {
        await this.contractState.update(contractKey, recordToJson(updated), {
          revision: current.revision,
        });
        return;
      } catch (error) {
        if (error instanceof StateConflict) {
          continue;
        }
        throw error;
      }
    }
  }
}

export class ConcordParticipantLease {
  readonly contract: ContractHandle;
  readonly participant: string;
  readonly sessionId: string;

  private readonly service: ConcordService;
  private tokenValue: ParticipantHandle | null = null;
  private closedValue = false;

  constructor(
    service: ConcordService,
    options: { contract: ContractHandle; participant: string; sessionId: string },
  ) {
    this.service = service;
    this.contract = options.contract;
    this.participant = endpointAddress(options.participant);
    this.sessionId = requireText(options.sessionId, "Concord session id");
  }

  get token(): ParticipantHandle | null {
    return this.tokenValue;
  }

  get closed(): boolean {
    return this.closedValue;
  }

  adopt(token: ParticipantHandle): void {
    if (
      token.contractId !== this.contract.contractId ||
      token.generation !== this.contract.generation ||
      token.participant !== this.participant ||
      token.sessionId !== this.sessionId
    ) {
      throw new ValidationError("participant token does not match lease");
    }
    this.tokenValue = token;
  }

  async attachOrRefresh(): Promise<ParticipantHandle> {
    if (this.closedValue) {
      throw new StateConflict("Concord participant lease is closed");
    }
    const token = this.tokenValue;
    if (token !== null) {
      try {
        this.tokenValue = await this.service.refreshToken(token);
        return this.tokenValue;
      } catch (error) {
        this.tokenValue = null;
        if (error instanceof StateConflict && isTerminalParticipantConflict(error)) {
          this.closedValue = true;
        }
        throw error;
      }
    }
    this.tokenValue = await this.service.attach(this.contract, this.participant, this.sessionId);
    return this.tokenValue;
  }

  async close(): Promise<void> {
    this.closedValue = true;
  }
}

export interface ConcordAgreementSpec {
  profile?: string;
  participants: string[];
  localParticipant: string;
  localSessionId: string;
  terms?: JsonObject;
  supersedes?: ContractPointer;
  currentSessions?: Record<string, string> | (() => Record<string, string> | Promise<Record<string, string>>);
  refreshIntervalSeconds?: number;
  createdBy?: string;
}

export class ConcordAgreement {
  readonly spec: ConcordAgreementSpec;
  readonly contract: ContractHandle;

  private readonly service: ConcordService;
  private readonly lease: ConcordParticipantLease;
  private validityValue: ContractValidity;
  private closedValue = false;

  constructor(
    service: ConcordService,
    options: {
      spec: ConcordAgreementSpec;
      contract: ContractHandle;
      lease: ConcordParticipantLease;
      validity: ContractValidity;
    },
  ) {
    this.service = service;
    this.spec = options.spec;
    this.contract = options.contract;
    this.lease = options.lease;
    this.validityValue = options.validity;
  }

  get validity(): ContractValidity {
    return this.validityValue;
  }

  get valid(): boolean {
    return this.validityValue.valid;
  }

  get localToken(): ParticipantHandle | null {
    return this.lease.token;
  }

  get closed(): boolean {
    return this.closedValue;
  }

  async refresh(): Promise<ContractValidity> {
    this.validityValue = await this.service.refreshAgreement(this);
    return this.validityValue;
  }

  async cancel(reason?: string): Promise<boolean> {
    this.closedValue = true;
    await this.lease.close();
    return this.service.cancelContract(this.contract, this.spec.localParticipant, { reason });
  }

  async close(): Promise<void> {
    this.closedValue = true;
    await this.lease.close();
  }
}

export class ConcordService {
  private readonly coordinator: ConcordCoordinator;

  constructor(coordinator: ConcordCoordinator) {
    this.coordinator = coordinator;
  }

  async ensureAgreement(spec: ConcordAgreementSpec): Promise<ConcordAgreement> {
    const normalized = normalizeAgreementSpec(spec);
    while (true) {
      const [contract, initialValidity] = await this.selectOrCreateAgreementContract(normalized);
      const lease = new ConcordParticipantLease(this, {
        contract,
        participant: normalized.localParticipant,
        sessionId: normalized.localSessionId,
      });
      const existing = initialValidity.tokens[normalized.localParticipant];
      if (existing !== undefined && existing.sessionId === normalized.localSessionId) {
        lease.adopt(existing);
      }
      const agreement = new ConcordAgreement(this, {
        spec: normalized,
        contract,
        lease,
        validity: initialValidity,
      });
      const refreshed = await agreement.refresh();
      if (agreementSuccessorStatus(refreshed.status)) {
        await agreement.cancel(`concord_agreement_${refreshed.status}`);
        continue;
      }
      return agreement;
    }
  }

  participantManager(options: {
    participant: string;
    sessionId: string;
    acceptContract: (contract: ContractHandle, record: ContractRecord) => boolean | Promise<boolean>;
    currentSessions?: (contract: ContractHandle) => Record<string, string> | Promise<Record<string, string>>;
    profile?: string;
    refreshIntervalSeconds?: number;
    reconcileIntervalSeconds?: number;
    contractSortKey?: (contract: ContractHandle) => string | number | [number, string];
    cancelTerminalStatuses?: ContractValidityStatus[];
    onError?: (error: unknown) => void;
  }): ConcordParticipantManager {
    return new ConcordParticipantManager({ concord: this, ...options });
  }

  async createContract(
    participants: string[],
    options: Parameters<ConcordCoordinator["createContract"]>[1] = {},
  ): Promise<ContractHandle> {
    return this.coordinator.createContract(participants, options);
  }

  async getContract(pointer: ContractPointer): Promise<ContractHandle | null> {
    return this.coordinator.getContract(pointer);
  }

  async contractRecord(contract: ContractHandle): Promise<ContractRecord | null> {
    return this.coordinator.contractRecord(contract);
  }

  async contracts(
    profile?: string,
    options: { contractId?: string; participant?: string; state?: ContractState } = {},
  ): Promise<ContractHandle[]> {
    return this.coordinator.contracts(profile, options);
  }

  watchContracts(): AsyncIterable<StateChange> {
    return this.coordinator.watchContracts();
  }

  async cancelContract(
    contract: ContractHandle,
    participant: string,
    options: { reason?: string } = {},
  ): Promise<boolean> {
    return this.coordinator.cancel(contract, participant, options);
  }

  async maintenanceCancelContract(
    contract: ContractHandle,
    options: { reason?: string; now?: string } = {},
  ): Promise<boolean> {
    return this.coordinator.maintenanceCancel(contract, options);
  }

  async validate(
    contract: ContractHandle,
    options: { currentSessions?: Record<string, string> } = {},
  ): Promise<ContractValidity> {
    return this.coordinator.validate(contract, options);
  }

  async attach(
    contract: ContractHandle,
    participant: string,
    sessionId: string,
  ): Promise<ParticipantHandle> {
    return this.coordinator.attach(contract, participant, sessionId);
  }

  async refreshToken(handle: ParticipantHandle): Promise<ParticipantHandle> {
    return this.coordinator.refresh(handle);
  }

  participantLease(options: {
    contract: ContractHandle;
    participant: string;
    sessionId: string;
  }): ConcordParticipantLease {
    return new ConcordParticipantLease(this, options);
  }

  async refreshAgreement(agreement: ConcordAgreement): Promise<ContractValidity> {
    if (agreement.closed) {
      throw new StateConflict("Concord agreement is closed");
    }
    const sessions = await agreementCurrentSessions(agreement.spec);
    let current = await this.validate(agreement.contract, { currentSessions: sessions });
    if (current.status === ContractValidityStatus.UNAVAILABLE) {
      return current;
    }
    if (agreementSuccessorStatus(current.status)) {
      return current;
    }
    const existing = current.tokens[agreement.spec.localParticipant];
    if (existing !== undefined && existing.sessionId !== agreement.spec.localSessionId) {
      return validity(ContractValidityStatus.SESSION_MISMATCH, {
        contract: current.contract,
        tokens: current.tokens,
        reason: agreement.spec.localParticipant,
      });
    }
    if (existing !== undefined && agreement.localToken === null) {
      agreement["lease"].adopt(existing);
    }
    if (agreement.localToken === null) {
      await agreement["lease"].attachOrRefresh();
      current = await this.validate(agreement.contract, { currentSessions: sessions });
    }
    return current;
  }

  coordinatorForTesting(): ConcordCoordinator {
    return this.coordinator;
  }

  private async selectOrCreateAgreementContract(
    spec: ConcordAgreementSpec,
  ): Promise<[ContractHandle, ContractValidity]> {
    const sessions = await agreementCurrentSessions(spec);
    const contract = await this.createContract(spec.participants, {
      generation: 1,
      profile: spec.profile,
      terms: spec.terms,
      createdBy: spec.createdBy ?? spec.localParticipant,
      supersedes: spec.supersedes,
    });
    return [contract, await this.validate(contract, { currentSessions: sessions })];
  }
}

export class ConcordParticipantManager {
  readonly participant: string;
  readonly sessionId: string;
  readonly profile?: string;

  private readonly concord: ConcordService;
  private readonly acceptContract: (contract: ContractHandle, record: ContractRecord) => boolean | Promise<boolean>;
  private readonly currentSessions?: (contract: ContractHandle) => Record<string, string> | Promise<Record<string, string>>;
  private readonly cancelTerminalStatuses: Set<ContractValidityStatus>;
  private readonly refreshIntervalSeconds: number;
  private readonly reconcileIntervalSeconds: number;
  private readonly contractSortKey?: (contract: ContractHandle) => string | number | [number, string];
  private readonly onError?: (error: unknown) => void;
  private managed = new Map<string, ConcordManagedContract>();
  private leases = new Map<string, ConcordParticipantLease>();
  private timers = new Set<ReturnType<typeof setInterval>>();
  private closed = false;
  private reconcileRunning = false;
  private refreshRunning = false;
  private watchAbort: AbortController | null = null;

  constructor(options: {
    concord: ConcordService;
    participant: string;
    sessionId: string;
    acceptContract: (contract: ContractHandle, record: ContractRecord) => boolean | Promise<boolean>;
    currentSessions?: (contract: ContractHandle) => Record<string, string> | Promise<Record<string, string>>;
    profile?: string;
    refreshIntervalSeconds?: number;
    reconcileIntervalSeconds?: number;
    contractSortKey?: (contract: ContractHandle) => string | number | [number, string];
    cancelTerminalStatuses?: ContractValidityStatus[];
    onError?: (error: unknown) => void;
  }) {
    this.concord = options.concord;
    this.participant = endpointAddress(options.participant);
    this.sessionId = requireText(options.sessionId, "Concord session id");
    this.acceptContract = options.acceptContract;
    this.currentSessions = options.currentSessions;
    this.profile = options.profile;
    this.cancelTerminalStatuses = new Set(options.cancelTerminalStatuses ?? []);
    this.refreshIntervalSeconds = positiveInterval(options.refreshIntervalSeconds ?? 5, "refreshIntervalSeconds");
    this.reconcileIntervalSeconds = positiveInterval(options.reconcileIntervalSeconds ?? 300, "reconcileIntervalSeconds");
    this.contractSortKey = options.contractSortKey;
    this.onError = options.onError;
  }

  managedContracts(): ConcordManagedContract[] {
    return [...this.managed.values()].sort((left, right) =>
      left.contract.key.localeCompare(right.contract.key),
    );
  }

  managedContract(contract: ContractHandle): ConcordManagedContract | null {
    return this.managed.get(contract.key) ?? null;
  }

  start(): void {
    if (this.closed || this.timers.size > 0 || this.watchAbort !== null) {
      return;
    }
    this.scheduleReconcile();
    this.startTimer(this.reconcileIntervalSeconds, () => this.scheduleReconcile());
    this.startTimer(this.refreshIntervalSeconds, () => this.scheduleRefresh());
    this.startWatchLoop();
  }

  async aclose(): Promise<void> {
    this.closed = true;
    for (const timer of this.timers) {
      clearInterval(timer);
    }
    this.timers.clear();
    this.watchAbort?.abort();
    this.watchAbort = null;
    for (const lease of this.leases.values()) {
      await lease.close();
    }
    this.managed.clear();
    this.leases.clear();
  }

  async release(contract: ContractHandle | string): Promise<void> {
    await this.releaseInternal(typeof contract === "string" ? contract : contract.key);
  }

  async reconcile(): Promise<ConcordManagedContract[]> {
    if (this.closed) {
      return [];
    }
    const nextManaged = new Map<string, ConcordManagedContract>();
    const nextLeases = new Map<string, ConcordParticipantLease>();
    for (const contract of this.sortedContracts(await this.concord.contracts(this.profile, {
      participant: this.participant,
      state: ContractState.OPEN,
    }))) {
      const managed = await this.reconcileContract(contract);
      if (managed === null) {
        continue;
      }
      nextManaged.set(contract.key, managed);
      const lease = this.leases.get(contract.key);
      if (lease !== undefined) {
        nextLeases.set(contract.key, lease);
      }
    }
    for (const [key, lease] of this.leases) {
      if (!nextLeases.has(key)) {
        await lease.close();
      }
    }
    this.managed = nextManaged;
    this.leases = nextLeases;
    return this.managedContracts();
  }

  async validate(
    contract: ContractHandle,
    options: { currentSessions?: Record<string, string> } = {},
  ): Promise<ContractValidity> {
    const sessions = { ...(options.currentSessions ?? {}), [this.participant]: this.sessionId };
    return this.concord.validate(contract, { currentSessions: sessions });
  }

  async cancel(contract: ContractHandle, options: { reason?: string } = {}): Promise<boolean> {
    return this.concord.cancelContract(contract, this.participant, options);
  }

  private startTimer(seconds: number, callback: () => void): void {
    const timer = setInterval(callback, seconds * 1000);
    timer.unref?.();
    this.timers.add(timer);
  }

  private scheduleReconcile(): void {
    if (this.closed || this.reconcileRunning) {
      return;
    }
    this.reconcileRunning = true;
    void this.reconcile()
      .catch((error: unknown) => this.reportError(error))
      .finally(() => {
        this.reconcileRunning = false;
      });
  }

  private scheduleRefresh(): void {
    if (this.closed || this.refreshRunning) {
      return;
    }
    this.refreshRunning = true;
    void this.refreshTokens()
      .catch((error: unknown) => this.reportError(error))
      .finally(() => {
        this.refreshRunning = false;
      });
  }

  private async refreshTokens(): Promise<void> {
    for (const [key, lease] of [...this.leases]) {
      if (this.closed) {
        return;
      }
      try {
        const token = await lease.attachOrRefresh();
        const managed = this.managed.get(key);
        if (managed !== undefined) {
          this.managed.set(key, { ...managed, token });
        }
      } catch {
        await this.releaseInternal(key);
      }
    }
  }

  private startWatchLoop(): void {
    const abort = new AbortController();
    this.watchAbort = abort;
    void this.watchLoop(abort.signal).catch((error: unknown) => {
      if (!abort.signal.aborted) {
        this.reportError(error);
      }
    });
  }

  private async watchLoop(signal: AbortSignal): Promise<void> {
    while (!this.closed && !signal.aborted) {
      try {
        for await (const _change of this.concord.watchContracts()) {
          if (this.closed || signal.aborted) {
            return;
          }
          this.scheduleReconcile();
        }
        return;
      } catch (error) {
        if (this.closed || signal.aborted) {
          return;
        }
        this.reportError(error);
        await sleep(Math.min(this.reconcileIntervalSeconds, 5), signal);
      }
    }
  }

  private sortedContracts(contracts: ContractHandle[]): ContractHandle[] {
    if (this.contractSortKey === undefined) {
      return contracts;
    }
    return [...contracts].sort((left, right) =>
      compareSortKey(this.contractSortKey!(left), this.contractSortKey!(right)),
    );
  }

  private async reconcileContract(contract: ContractHandle): Promise<ConcordManagedContract | null> {
    if (!contract.participants.includes(this.participant)) {
      await this.releaseInternal(contract.key);
      return null;
    }
    const record = await this.concord.contractRecord(contract);
    if (record === null) {
      await this.releaseInternal(contract.key);
      return null;
    }
    if (this.profile !== undefined && record.profile !== this.profile) {
      await this.releaseInternal(contract.key);
      return null;
    }
    if (record.state === ContractState.CANCELLED) {
      await this.releaseInternal(contract.key);
      return null;
    }
    if (!(await this.acceptContract(contract, record))) {
      await this.releaseInternal(contract.key);
      return null;
    }
    const sessions = {
      ...(this.currentSessions === undefined ? {} : await this.currentSessions(contract)),
      [this.participant]: this.sessionId,
    };
    let contractValidity = await this.concord.validate(contract, { currentSessions: sessions });
    const existing = contractValidity.tokens[this.participant];
    if (terminalManagedStatus(contractValidity.status)) {
      if (this.cancelTerminalStatuses.has(contractValidity.status)) {
        await this.cancel(contract, { reason: `concord_managed_${contractValidity.status}` });
      }
      await this.releaseInternal(contract.key);
      return null;
    }
    let lease = this.leases.get(contract.key);
    if (lease === undefined) {
      lease = this.concord.participantLease({
        contract,
        participant: this.participant,
        sessionId: this.sessionId,
      });
      this.leases.set(contract.key, lease);
    }
    if (existing !== undefined) {
      if (existing.sessionId !== this.sessionId) {
        await this.releaseInternal(contract.key);
        return null;
      }
      lease.adopt(existing);
    }
    let token = lease.token;
    if (token === null) {
      try {
        token = await lease.attachOrRefresh();
      } catch {
        await this.releaseInternal(contract.key);
        return null;
      }
      contractValidity = await this.concord.validate(contract, { currentSessions: sessions });
    }
    const managed: ConcordManagedContract = {
      contract,
      record: contractValidity.contract ?? record,
      validity: contractValidity,
      token,
    };
    return managed;
  }

  private async releaseInternal(key: string): Promise<void> {
    this.managed.delete(key);
    const lease = this.leases.get(key);
    if (lease !== undefined) {
      await lease.close();
      this.leases.delete(key);
    }
  }

  private reportError(error: unknown): void {
    if (this.onError !== undefined) {
      this.onError(error);
    }
  }
}

export const STALE_OPEN_CONTRACT_STATUSES = new Set<ContractValidityStatus>([
  ContractValidityStatus.NOT_YET_FULFILLED,
  ContractValidityStatus.MISSING_TOKEN,
  ContractValidityStatus.INVALID_TOKEN,
  ContractValidityStatus.SESSION_MISMATCH,
  ContractValidityStatus.TERMS_HASH_MISMATCH,
  ContractValidityStatus.GENERATION_MISMATCH,
  ContractValidityStatus.INVALID_CONTRACT,
]);

export class ConcordReaperService {
  private readonly concord: ConcordService;
  private readonly contractState: StateStore;
  private readonly tokenState: StateStore;
  private readonly maintenanceState: StateStore;
  private readonly staleGraceSeconds: number;
  private readonly cancelledRetentionSeconds: number;
  private readonly clock: () => string;

  constructor(
    concord: ConcordService,
    options: {
      contractState: StateStore;
      tokenState: StateStore;
      maintenanceState: StateStore;
      staleGraceSeconds?: number;
      cancelledRetentionSeconds?: number;
      clock?: () => string;
    },
  ) {
    this.concord = concord;
    this.contractState = options.contractState;
    this.tokenState = options.tokenState;
    this.maintenanceState = options.maintenanceState;
    this.staleGraceSeconds = options.staleGraceSeconds ?? DEFAULT_CONCORD_REAPER_STALE_GRACE_SECONDS;
    this.cancelledRetentionSeconds =
      options.cancelledRetentionSeconds ?? DEFAULT_CONCORD_REAPER_CANCELLED_RETENTION_SECONDS;
    this.clock = options.clock ?? utcIsoNow;
  }

  async scanOnce(): Promise<ConcordReaperScanResult> {
    const counts = emptyReaperCounts();
    const now = this.clock();
    for (const entry of await this.contractState.items(concordContractsPrefix())) {
      const parsed = parseConcordContractKey(entry.key);
      if (parsed === null) {
        continue;
      }
      counts.scannedContractCount += 1;
      const [contractId, generation] = parsed;
      try {
        const record = validateContractRecord(entry.value);
        if (record.contractId !== contractId || record.generation !== generation) {
          counts.staleObservationsCreated += await this.observeInvalid(entry, contractId, generation, now);
          continue;
        }
        const handle = contractHandle(entry.key, record, entry.revision);
        if (record.state === ContractState.CANCELLED) {
          counts.staleObservationsCleared += await this.clearStale(contractId, generation);
          if (record.cancelledAt !== undefined && ageSeconds(record.cancelledAt, now) >= this.cancelledRetentionSeconds) {
            const deleted = await this.deleteCancelled(handle);
            if (deleted.deleted) {
              counts.contractsDeleted += 1;
              counts.tokenKeysDeleted += deleted.deletedTokenKeyCount;
            }
          }
          continue;
        }
        const currentValidity = await this.concord.validate(handle);
        if (STALE_OPEN_CONTRACT_STATUSES.has(currentValidity.status)) {
          const observed = await this.observeStale(
            contractId,
            generation,
            currentValidity.status,
            currentValidity.reason,
            entry.revision,
            now,
          );
          if (observed.created) {
            counts.staleObservationsCreated += 1;
          }
          if (ageSeconds(observed.firstObservedStaleAt, now) >= this.staleGraceSeconds) {
            if (await this.concord.maintenanceCancelContract(handle, { now })) {
              counts.contractsCancelled += 1;
            }
            counts.staleObservationsCleared += await this.clearStale(contractId, generation);
          }
          continue;
        }
        if (currentValidity.status !== ContractValidityStatus.UNAVAILABLE) {
          counts.staleObservationsCleared += await this.clearStale(contractId, generation);
        }
      } catch {
        counts.staleObservationsCreated += await this.observeInvalid(entry, contractId, generation, now);
      }
    }
    counts.staleObservationsCleared += await this.clearOrphanedStaleObservations();
    counts.staleObservationCount = (await this.maintenanceState.items("stale.")).length;
    return counts;
  }

  private async observeInvalid(
    entry: StateEntry,
    contractId: string,
    generation: number,
    now: string,
  ): Promise<number> {
    await this.observeStale(
      contractId,
      generation,
      ContractValidityStatus.INVALID_CONTRACT,
      "invalid contract record",
      entry.revision,
      now,
    );
    return 1;
  }

  private async observeStale(
    contractId: string,
    generation: number,
    status: ContractValidityStatus,
    reason: string | undefined,
    contractRevision: number | undefined,
    now: string,
  ): Promise<{ firstObservedStaleAt: string; created: boolean }> {
    const key = concordStaleObservationKey({ contractId, generation });
    const current = await this.maintenanceState.get(key);
    if (current !== null) {
      try {
        const record = validateStaleObservationRecord(current.value);
        if (record.contractId === contractId && record.generation === generation) {
          return { firstObservedStaleAt: record.firstObservedStaleAt, created: false };
        }
      } catch {
        // replace invalid observation below
      }
    }
    const observation = validateStaleObservationRecord({
      schema: CONCORD_STALE_OBSERVATION_SCHEMA_ID,
      contractId,
      generation,
      firstObservedStaleAt: now,
      status,
      reason,
      contractRevision,
    });
    try {
      await this.maintenanceState.create(key, recordToJson(observation));
    } catch (error) {
      if (!(error instanceof StateConflict)) {
        throw error;
      }
      return { firstObservedStaleAt: now, created: false };
    }
    return { firstObservedStaleAt: now, created: true };
  }

  private async clearStale(contractId: string, generation: number): Promise<number> {
    const key = concordStaleObservationKey({ contractId, generation });
    const current = await this.maintenanceState.get(key);
    if (current === null) {
      return 0;
    }
    try {
      await this.maintenanceState.delete(key, { revision: current.revision });
    } catch (error) {
      if (error instanceof StateConflict) {
        return 0;
      }
      throw error;
    }
    return 1;
  }

  private async clearOrphanedStaleObservations(): Promise<number> {
    let cleared = 0;
    for (const entry of await this.maintenanceState.items("stale.")) {
      let observation: ConcordStaleObservationRecord;
      try {
        observation = validateStaleObservationRecord(entry.value);
      } catch {
        continue;
      }
      const contract = await this.contractState.get(
        concordContractKey({
          contractId: observation.contractId,
          generation: observation.generation,
        }),
      );
      if (contract !== null) {
        continue;
      }
      await this.maintenanceState.delete(entry.key, { revision: entry.revision });
      cleared += 1;
    }
    return cleared;
  }

  private async deleteCancelled(contract: ContractHandle): Promise<ConcordMaintenanceDeletionResult> {
    const current = await this.contractState.get(contract.key);
    if (current === null) {
      return { deleted: false, deletedTokenKeyCount: 0 };
    }
    const record = validateContractRecord(current.value);
    if (record.state !== ContractState.CANCELLED) {
      return { deleted: false, deletedTokenKeyCount: 0 };
    }
    await this.contractState.delete(contract.key, { revision: current.revision });
    let deletedTokenKeyCount = 0;
    for (const entry of await this.tokenState.items(
      concordContractPrefix({ contractId: contract.contractId, generation: contract.generation }),
    )) {
      await this.tokenState.delete(entry.key, { revision: entry.revision });
      deletedTokenKeyCount += 1;
    }
    return { deleted: true, deletedTokenKeyCount };
  }
}

function contractHandle(key: string, record: ContractRecord, revision: number): ContractHandle {
  return {
    key,
    contractId: record.contractId,
    generation: record.generation,
    participants: [...record.participants],
    attachedParticipants: [...record.attachedParticipants],
    revision,
    state: record.state,
    ...(record.profile === undefined ? {} : { profile: record.profile }),
    ...(record.termsHash === undefined ? {} : { termsHash: record.termsHash }),
  };
}

function participantHandle(
  key: string,
  record: ParticipantTokenRecord,
  revision: number,
): ParticipantHandle {
  return {
    key,
    contractId: record.contractId,
    generation: record.generation,
    participant: record.participant,
    sessionId: record.sessionId,
    tokenId: record.tokenId,
    revision,
    refreshSeq: record.refreshSeq,
    ttlSeconds: record.ttlSeconds,
    ...(record.termsHash === undefined ? {} : { termsHash: record.termsHash }),
  };
}

function validity(
  status: ContractValidityStatus,
  options: {
    contract?: ContractRecord;
    tokens?: Record<string, ParticipantHandle>;
    reason?: string;
  } = {},
): ContractValidity {
  return {
    status,
    ...(options.contract === undefined ? {} : { contract: options.contract }),
    tokens: options.tokens ?? {},
    ...(options.reason === undefined ? {} : { reason: options.reason }),
    valid: status === ContractValidityStatus.VALID,
  };
}

function validateEndpointList(value: unknown, fieldName: string): string[] {
  if (!Array.isArray(value)) {
    throw new ValidationError(`${fieldName} must be an array`);
  }
  return value.map((item) => endpointAddress(requireText(item, fieldName)));
}

function requireCanonicalUnique(values: string[], fieldName: string): void {
  if (new Set(values).size !== values.length) {
    throw new ValidationError(`${fieldName} must be unique`);
  }
  const sorted = [...values].sort();
  if (sorted.some((value, index) => value !== values[index])) {
    throw new ValidationError(`${fieldName} must be canonicalized`);
  }
}

function nonNegativeInteger(value: unknown, fieldName: string): number {
  if (!Number.isInteger(value) || Number(value) < 0) {
    throw new ValidationError(`${fieldName} must be non-negative`);
  }
  return Number(value);
}

function setOptionalText(
  target: object,
  key: string,
  value: unknown,
  fieldName: string,
): void {
  if (value !== undefined && value !== null) {
    (target as Record<string, unknown>)[key] = requireText(value, fieldName);
  }
}

function setOptionalEndpoint(
  target: object,
  key: string,
  value: unknown,
): void {
  if (value !== undefined && value !== null) {
    (target as Record<string, unknown>)[key] = endpointAddress(requireText(value, key));
  }
}

function tokenMatchesAttachRequest(
  token: ParticipantTokenRecord,
  record: ContractRecord,
  participant: string,
  sessionId: string,
  tokenId: string | undefined,
): boolean {
  return (
    token.contractId === record.contractId &&
    token.generation === record.generation &&
    token.participant === participant &&
    token.sessionId === sessionId &&
    (tokenId === undefined || token.tokenId === tokenId) &&
    token.termsHash === record.termsHash
  );
}

function tokenMatchesHandle(token: ParticipantTokenRecord, handle: ParticipantHandle): boolean {
  return (
    token.contractId === handle.contractId &&
    token.generation === handle.generation &&
    token.participant === handle.participant &&
    token.sessionId === handle.sessionId &&
    token.tokenId === handle.tokenId &&
    token.termsHash === handle.termsHash
  );
}

function tokenValidityStatus(
  token: ParticipantTokenRecord,
  contract: ContractRecord,
  participant: string,
  currentSessions: Record<string, string> | undefined,
): ContractValidityStatus | null {
  if (token.contractId !== contract.contractId) {
    return ContractValidityStatus.INVALID_TOKEN;
  }
  if (token.generation !== contract.generation) {
    return ContractValidityStatus.GENERATION_MISMATCH;
  }
  if (token.participant !== participant) {
    return ContractValidityStatus.INVALID_TOKEN;
  }
  if (contract.termsHash !== undefined && token.termsHash !== contract.termsHash) {
    return ContractValidityStatus.TERMS_HASH_MISMATCH;
  }
  const currentSession = currentSessions?.[participant];
  if (currentSession !== undefined && token.sessionId !== currentSession) {
    return ContractValidityStatus.SESSION_MISMATCH;
  }
  return null;
}

function normalizeAgreementSpec(spec: ConcordAgreementSpec): ConcordAgreementSpec {
  const participants = spec.participants.map(endpointAddress).sort();
  requireCanonicalUnique(participants, "Concord agreement participants");
  const localParticipant = endpointAddress(spec.localParticipant);
  if (!participants.includes(localParticipant)) {
    throw new ValidationError("localParticipant must be named by participants");
  }
  return {
    ...spec,
    participants,
    localParticipant,
    localSessionId: requireText(spec.localSessionId, "Concord agreement session id"),
    createdBy: spec.createdBy === undefined ? localParticipant : endpointAddress(spec.createdBy),
    terms: spec.terms === undefined ? undefined : cloneJson(spec.terms),
    supersedes: spec.supersedes === undefined ? undefined : validateContractPointer(spec.supersedes),
  };
}

async function agreementCurrentSessions(spec: ConcordAgreementSpec): Promise<Record<string, string>> {
  const sessions =
    spec.currentSessions === undefined
      ? {}
      : typeof spec.currentSessions === "function"
        ? await spec.currentSessions()
        : spec.currentSessions;
  return { ...sessions, [spec.localParticipant]: spec.localSessionId };
}

function agreementSuccessorStatus(status: ContractValidityStatus): boolean {
  const statuses: ContractValidityStatus[] = [
    ContractValidityStatus.CANCELLED,
    ContractValidityStatus.MISSING_CONTRACT,
    ContractValidityStatus.INVALID_CONTRACT,
    ContractValidityStatus.INVALID_TOKEN,
    ContractValidityStatus.MISSING_TOKEN,
    ContractValidityStatus.GENERATION_MISMATCH,
    ContractValidityStatus.SESSION_MISMATCH,
    ContractValidityStatus.TERMS_HASH_MISMATCH,
  ];
  return statuses.includes(status);
}

function terminalManagedStatus(status: ContractValidityStatus): boolean {
  return agreementSuccessorStatus(status);
}

function isTerminalParticipantConflict(error: StateConflict): boolean {
  return [
    "Concord contract is missing",
    "Concord contract is cancelled",
    "Concord participant token is missing",
    "Concord participant token changed owner",
    "Concord participant is already attached",
  ].some((text) => error.message.includes(text));
}

function positiveInterval(value: number, fieldName: string): number {
  if (!Number.isFinite(value) || value <= 0) {
    throw new ValidationError(`${fieldName} must be greater than zero`);
  }
  return value;
}

function compareSortKey(
  left: string | number | [number, string],
  right: string | number | [number, string],
): number {
  if (Array.isArray(left) && Array.isArray(right)) {
    const numeric = left[0] - right[0];
    return numeric === 0 ? left[1].localeCompare(right[1]) : numeric;
  }
  if (typeof left === "number" && typeof right === "number") {
    return left - right;
  }
  return String(left).localeCompare(String(right));
}

async function sleep(seconds: number, signal: AbortSignal): Promise<void> {
  await new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, seconds * 1000);
    timer.unref?.();
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

function emptyReaperCounts(): ConcordReaperScanResult {
  return {
    scannedContractCount: 0,
    staleObservationCount: 0,
    staleObservationsCreated: 0,
    staleObservationsCleared: 0,
    contractsCancelled: 0,
    contractsDeleted: 0,
    tokenKeysDeleted: 0,
  };
}

function ageSeconds(then: string, now: string): number {
  return (Date.parse(now) - Date.parse(then)) / 1000;
}

function recordToJson<T>(record: T): JsonObject {
  return cloneJson(record as unknown as JsonObject);
}
