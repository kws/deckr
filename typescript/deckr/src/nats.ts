import { randomUUID } from "node:crypto";

import { StateConflict, StateUnavailable } from "./errors.ts";
import { requireJsonObject, type JsonObject } from "./json.ts";
import { validateDeckrMessage, validateHeaderHints, validateSubjectHint, type DeckrMessage } from "./lanes.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateChange,
  type StateEntry,
  type StateStore,
  type StateStorePolicy,
} from "./state.ts";

export interface NatsStateStoreOptions {
  connection: unknown;
  bucket: string;
  policy?: StateStorePolicy;
}

const NATS_KV_CREATION_MARKER_METADATA_KEY = "deckr.kv.creation_id";
const NATS_KV_STREAM_PREFIX = "KV_";
const NATS_KV_SUBJECT_PREFIX = "$KV";

// This is the complete cross-runtime Deckr policy vector. Other JetStream
// fields are broker/client tuning or creator diagnostics and are never grounds
// for mutating or rejecting an otherwise compatible existing shared bucket.
export const NATS_KV_POLICY_VECTOR_FIELDS = Object.freeze([
  "max_age",
  "max_msgs_per_subject",
  "allow_msg_ttl",
  "subject_delete_marker_ttl",
] as const);
export const NATS_KV_NON_AUTHORITATIVE_CONFIG_FIELDS = Object.freeze([
  "allow_direct",
  "metadata",
  "storage",
] as const);

export async function connectNats(options: Record<string, unknown>): Promise<unknown> {
  const mod = await import("nats").catch((error) => {
    throw new StateUnavailable("Install the optional nats peer dependency to use @deckr/core/nats", {
      cause: error,
    });
  });
  return mod.connect(options);
}

export function deckrMessageFromNatsMessage(message: {
  subject: string;
  data: Uint8Array;
  headers?: unknown;
}): DeckrMessage {
  const raw = JSON.parse(new TextDecoder().decode(message.data));
  const deckrMessage = validateDeckrMessage(raw);
  validateSubjectHint(message.subject, deckrMessage);
  validateHeaderHints(message.headers as never, deckrMessage);
  return deckrMessage;
}

export class NatsStateStore implements StateStore {
  readonly name: string;
  readonly policy: StateStorePolicy;

  private readonly kv: unknown;

  private constructor(
    options: NatsStateStoreOptions,
    policy: StateStorePolicy,
    kv: unknown,
  ) {
    this.name = options.bucket;
    this.policy = policy;
    this.kv = kv;
  }

  static async open(options: NatsStateStoreOptions): Promise<NatsStateStore> {
    const policy = options.policy ?? PERSISTENT_STATE_STORE_POLICY;
    const kv = await this.openAndValidateKv(
      options.connection,
      options.bucket,
      policy,
    );
    return new NatsStateStore(options, policy, kv);
  }

  async get(key: string): Promise<StateEntry | null> {
    const kv = this.kv;
    try {
      const entry = await call(kv, "get", key);
      if (entry === null || entry === undefined) {
        return null;
      }
      return stateEntryFromKv(key, entry);
    } catch (error) {
      if (isMissingKey(error)) {
        return null;
      }
      throw new StateUnavailable(`Could not get state key ${JSON.stringify(key)}`, {
        cause: error,
      });
    }
  }

  async items(prefix = ""): Promise<StateEntry[]> {
    const kv = this.kv;
    let keys: string[];
    try {
      const rawKeys = await call(kv, "keys", prefix === "" ? undefined : `${prefix}>`);
      keys = (await collectAsyncStrings(rawKeys))
        .filter((key) => key.startsWith(prefix))
        .sort();
    } catch (error) {
      throw new StateUnavailable(`Could not list state keys with prefix ${JSON.stringify(prefix)}`, {
        cause: error,
      });
    }
    const entries: StateEntry[] = [];
    for (const key of keys) {
      const entry = await this.get(key);
      if (entry !== null) {
        entries.push(entry);
      }
    }
    return entries;
  }

  async put(key: string, value: JsonObject, options: { ttl?: number | null } = {}): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const kv = this.kv;
    const revision = await call(kv, "put", key, JSON.stringify(value));
    return { key, value, revision: Number(revision) };
  }

  async create(key: string, value: JsonObject, options: { ttl?: number | null } = {}): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const kv = this.kv;
    try {
      const revision = await call(kv, "create", key, JSON.stringify(value));
      return { key, value, revision: Number(revision) };
    } catch (error) {
      if (isConflict(error)) {
        throw new StateConflict(`State key ${JSON.stringify(key)} already exists`);
      }
      throw error;
    }
  }

  async update(
    key: string,
    value: JsonObject,
    options: { revision: number; ttl?: number | null },
  ): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const kv = this.kv;
    try {
      const revision = await call(kv, "update", key, JSON.stringify(value), {
        previousSeq: options.revision,
      });
      return { key, value, revision: Number(revision) };
    } catch (error) {
      if (isConflict(error)) {
        throw new StateConflict(`State key ${JSON.stringify(key)} revision changed`);
      }
      throw error;
    }
  }

  async delete(key: string, options: { revision?: number | null } = {}): Promise<void> {
    const kv = this.kv;
    const deleteOptions =
      options.revision === undefined || options.revision === null
        ? undefined
        : { previousSeq: options.revision };
    try {
      await call(kv, "delete", key, deleteOptions);
    } catch (error) {
      if (isConflict(error)) {
        throw new StateConflict(`State key ${JSON.stringify(key)} revision changed`);
      }
      if (!isMissingKey(error)) {
        throw error;
      }
    }
  }

  watch(prefix = ""): AsyncIterable<StateChange> {
    return this.watchPrefix(prefix);
  }

  private static async openAndValidateKv(
    connection: unknown,
    name: string,
    policy: StateStorePolicy,
  ): Promise<unknown> {
    validateNatsKvBucketName(name);
    const jetstream = callSync(connection, "jetstream");
    const views = getProperty(jetstream, "views");
    const manager = await callSync(connection, "jetstreamManager");
    const streams = getProperty(manager, "streams");
    const creationMarker = randomUUID();
    const streamName = `${NATS_KV_STREAM_PREFIX}${name}`;
    const createConfig = natsKvStreamConfig(
      name,
      streamName,
      policy,
      creationMarker,
    );
    let createError: unknown | null = null;
    let newlyCreated = false;
    try {
      await call(streams, "add", createConfig);
      newlyCreated = true;
    } catch (error) {
      // Stream creation is the atomic existence check. A failed create may mean
      // another process won the race; bind to that winner and validate its
      // complete policy read-only below.
      createError = error;
    }
    let kv: unknown;
    try {
      kv = await call(views, "kv", name, { bindOnly: true });
    } catch (bindError) {
      throw new StateUnavailable(`NATS KV bucket ${JSON.stringify(name)} is unavailable`, {
        cause:
          createError === null
            ? bindError
            : new AggregateError([createError, bindError]),
      });
    }
    let status: unknown;
    try {
      status = await call(kv, "status");
    } catch (error) {
      throw new StateUnavailable(
        `Could not inspect NATS KV bucket ${JSON.stringify(name)}`,
        {
          cause:
            createError === null
              ? error
              : new AggregateError([createError, error]),
        },
      );
    }
    await this.ensureBucketPolicy(name, policy, status, newlyCreated);
    return kv;
  }

  private static async ensureBucketPolicy(
    name: string,
    policy: StateStorePolicy,
    status: unknown,
    newlyCreated: boolean,
  ): Promise<void> {
    const expectedTtlMs =
      policy.brokerTtlSeconds === null ? 0 : policy.brokerTtlSeconds * 1000;
    const expectedMaxAgeNs = expectedTtlMs * 1_000_000;
    const expectedDeleteMarkerNs =
      policy.brokerTtlSeconds === null ? null : expectedMaxAgeNs;
    const config = streamConfigFromKvStatus(status);
    const maxAgeMatches = Number(config["max_age"] ?? 0) === expectedMaxAgeNs;
    const maxMessagesMatches = Number(config["max_msgs_per_subject"]) === 1;
    const observedAllowWriteTtl = config["allow_msg_ttl"] === true;
    const allowWriteTtlMatches =
      observedAllowWriteTtl === policy.allowWriteTtl;
    const deleteMarkerMatches =
      expectedDeleteMarkerNs === null ||
      Number(config["subject_delete_marker_ttl"] ?? 0) === expectedDeleteMarkerNs;
    const mismatches: string[] = [];
    if (!maxAgeMatches) {
      mismatches.push(
        `max_age (expected ${expectedMaxAgeNs}ns, found ${String(config["max_age"])})`,
      );
    }
    if (!maxMessagesMatches) {
      mismatches.push(
        `max_msgs_per_subject (expected 1, found ${String(config["max_msgs_per_subject"])})`,
      );
    }
    if (!allowWriteTtlMatches) {
      mismatches.push(
        `allow_msg_ttl (expected ${String(policy.allowWriteTtl)}, found ${String(config["allow_msg_ttl"])})`,
      );
    }
    if (!deleteMarkerMatches) {
      mismatches.push(
        `subject_delete_marker_ttl (expected ${expectedDeleteMarkerNs}ns, found ${String(config["subject_delete_marker_ttl"])})`,
      );
    }
    if (mismatches.length === 0) {
      return;
    }

    const streamName = String(getProperty(config, "name"));
    if (!newlyCreated) {
      throw new StateUnavailable(
        `Existing NATS KV bucket ${JSON.stringify(name)} has an incompatible Deckr KV policy: ${mismatches.join(", ")}. Deckr will not rewrite shared bucket policy; delete and recreate the development bucket/stream ${streamName} and restart.`,
      );
    }

    throw new StateUnavailable(
      `New NATS KV bucket ${JSON.stringify(name)} was created with an incompatible Deckr KV policy: ${mismatches.join(", ")}. Delete the development bucket/stream ${streamName} and restart.`,
    );
  }

  private validateTtl(ttl: number | null): void {
    if (!this.policy.allowWriteTtl && ttl !== null) {
      throw new StateUnavailable(
        `${this.policy.description} does not use write TTL; per-key TTL ${ttl} is not supported.`,
      );
    }
    if (
      ttl !== null &&
      this.policy.brokerTtlSeconds !== null &&
      Math.abs(ttl - this.policy.brokerTtlSeconds) > 0.001
    ) {
      throw new StateUnavailable(
        `current state uses broker TTL ${this.policy.brokerTtlSeconds}; per-key TTL ${ttl} is not supported.`,
      );
    }
  }

  private async *watchPrefix(prefix: string): AsyncIterable<StateChange> {
    const kv = this.kv;
    const iterator = await call(kv, "watch", {
      key: prefix === "" ? ">" : `${prefix}>`,
      include: "updates",
    });
    for await (const item of iterator as AsyncIterable<unknown>) {
      const entry = item as Record<string, unknown>;
      const key = String(getProperty(entry, "key"));
      const operation = String(getProperty(entry, "operation"));
      if (!key.startsWith(prefix)) {
        continue;
      }
      if (operation === "PUT") {
        yield { operation: "put", key, entry: stateEntryFromKv(key, entry) };
      } else if (operation === "DEL" || operation === "PURGE") {
        yield { operation: "delete", key };
      }
    }
  }
}

function validateNatsKvBucketName(name: string): void {
  if (!/^[-\w]+$/.test(name)) {
    throw new StateUnavailable(`Invalid NATS KV bucket name ${JSON.stringify(name)}`);
  }
}

function natsKvStreamConfig(
  bucket: string,
  streamName: string,
  policy: StateStorePolicy,
  creationMarker: string,
): Record<string, unknown> {
  const maxAgeNs =
    policy.brokerTtlSeconds === null
      ? 0
      : policy.brokerTtlSeconds * 1_000_000_000;
  return {
    name: streamName,
    subjects: [`${NATS_KV_SUBJECT_PREFIX}.${bucket}.>`],
    retention: "limits",
    max_consumers: -1,
    max_msgs: -1,
    max_msgs_per_subject: 1,
    max_age: maxAgeNs,
    max_bytes: -1,
    max_msg_size: -1,
    storage: "file",
    discard: "new",
    duplicate_window: 120 * 1_000_000_000,
    deny_delete: true,
    allow_direct: true,
    num_replicas: 1,
    allow_rollup_hdrs: true,
    allow_msg_ttl: policy.allowWriteTtl,
    metadata: { [NATS_KV_CREATION_MARKER_METADATA_KEY]: creationMarker },
    ...(policy.brokerTtlSeconds === null
      ? {}
      : { subject_delete_marker_ttl: maxAgeNs }),
  };
}

function streamConfigFromKvStatus(status: unknown): Record<string, unknown> {
  const streamInfo = getProperty(status, "streamInfo");
  return getProperty(streamInfo, "config") as Record<string, unknown>;
}

function stateEntryFromKv(key: string, entry: unknown): StateEntry {
  const value = getProperty(entry, "value");
  const revision = Number(getProperty(entry, "revision"));
  const bytes =
    value instanceof Uint8Array
      ? value
      : typeof value === "string"
        ? new TextEncoder().encode(value)
        : new Uint8Array(value as ArrayBufferLike);
  return {
    key: String(getProperty(entry, "key") ?? key),
    value: requireJsonObject(JSON.parse(new TextDecoder().decode(bytes)), "state value"),
    revision,
  };
}

function getProperty(target: unknown, key: string): unknown {
  if (typeof target !== "object" || target === null || !(key in target)) {
    throw new StateUnavailable(`NATS object does not expose ${key}`);
  }
  return (target as Record<string, unknown>)[key];
}

function callSync(target: unknown, method: string, ...args: unknown[]): unknown {
  const fn = getProperty(target, method);
  if (typeof fn !== "function") {
    throw new StateUnavailable(`NATS object does not expose ${method}()`);
  }
  return Reflect.apply(fn, target, args);
}

async function call(target: unknown, method: string, ...args: unknown[]): Promise<unknown> {
  return await callSync(target, method, ...args);
}

async function collectAsyncStrings(value: unknown): Promise<string[]> {
  if (value === undefined || value === null) {
    return [];
  }
  if (typeof (value as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
    const out: string[] = [];
    for await (const item of value as AsyncIterable<unknown>) {
      out.push(String(item));
    }
    return out;
  }
  if (typeof (value as Iterable<unknown>)[Symbol.iterator] === "function") {
    return [...(value as Iterable<unknown>)].map(String);
  }
  return [];
}

function isMissingKey(error: unknown): boolean {
  return /not found|missing|no keys/i.test(String(error instanceof Error ? error.message : error));
}

function isConflict(error: unknown): boolean {
  return /wrong.*last|revision|exists|conflict/i.test(String(error instanceof Error ? error.message : error));
}
