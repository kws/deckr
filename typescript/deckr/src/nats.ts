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

  private readonly connection: unknown;
  private kv: unknown | null = null;
  private openingKv: Promise<unknown> | null = null;

  constructor(options: NatsStateStoreOptions) {
    this.connection = options.connection;
    this.name = options.bucket;
    this.policy = options.policy ?? PERSISTENT_STATE_STORE_POLICY;
  }

  async get(key: string): Promise<StateEntry | null> {
    const kv = await this.availableKv();
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
    const kv = await this.availableKv();
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
    const kv = await this.availableKv();
    const revision = await call(kv, "put", key, JSON.stringify(value));
    return { key, value, revision: Number(revision) };
  }

  async create(key: string, value: JsonObject, options: { ttl?: number | null } = {}): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const kv = await this.availableKv();
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
    const kv = await this.availableKv();
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
    const kv = await this.availableKv();
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

  private async availableKv(): Promise<unknown> {
    if (this.kv !== null) {
      return this.kv;
    }
    if (this.openingKv !== null) {
      return this.openingKv;
    }
    const opening = this.openAndValidateKv();
    this.openingKv = opening;
    try {
      return await opening;
    } finally {
      if (this.openingKv === opening) {
        this.openingKv = null;
      }
    }
  }

  private async openAndValidateKv(): Promise<unknown> {
    const jetstream = callSync(this.connection, "jetstream");
    const views = getProperty(jetstream, "views");
    const creationMarker = randomUUID();
    const openOptions: Record<string, unknown> = {
      history: 1,
      metadata: { [NATS_KV_CREATION_MARKER_METADATA_KEY]: creationMarker },
      ...(this.policy.brokerTtlSeconds === null
        ? {}
        : { ttl: this.policy.brokerTtlSeconds * 1000 }),
    };
    let kv: unknown;
    try {
      kv = await call(views, "kv", this.name, openOptions);
    } catch (createError) {
      // The KV helper races an info lookup against stream creation internally.
      // If another process wins that race, bind to the winner and validate its
      // policy as an existing shared bucket. Never infer creation ownership from
      // a preflight lookup.
      try {
        kv = await call(views, "kv", this.name, { bindOnly: true });
      } catch (bindError) {
        throw new StateUnavailable(`NATS KV bucket ${JSON.stringify(this.name)} is unavailable`, {
          cause: new AggregateError([createError, bindError]),
        });
      }
    }
    let status: unknown;
    try {
      status = await call(kv, "status");
    } catch (error) {
      throw new StateUnavailable(
        `Could not inspect NATS KV bucket ${JSON.stringify(this.name)}`,
        { cause: error },
      );
    }
    const config = streamConfigFromKvStatus(status);
    const metadata = recordOrNull(config["metadata"]);
    const newlyCreated =
      metadata?.[NATS_KV_CREATION_MARKER_METADATA_KEY] === creationMarker;
    await this.ensureBucketPolicy(kv, status, newlyCreated);
    // Do not cache a handle until its policy has passed validation. A failed
    // first operation must not bypass the check on the next operation.
    this.kv = kv;
    return kv;
  }

  private async ensureBucketPolicy(
    kv: unknown,
    status: unknown,
    newlyCreated: boolean,
  ): Promise<void> {
    const expectedTtlMs =
      this.policy.brokerTtlSeconds === null ? 0 : this.policy.brokerTtlSeconds * 1000;
    const expectedMaxAgeNs = expectedTtlMs * 1_000_000;
    const expectedDeleteMarkerNs =
      this.policy.brokerTtlSeconds === null ? null : expectedMaxAgeNs;
    const config = streamConfigFromKvStatus(status);
    const maxAgeMatches = Number(config["max_age"] ?? 0) === expectedMaxAgeNs;
    const maxMessagesMatches = Number(config["max_msgs_per_subject"]) === 1;
    const observedAllowWriteTtl = config["allow_msg_ttl"] === true;
    const allowWriteTtlMatches =
      observedAllowWriteTtl === this.policy.allowWriteTtl;
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
        `allow_msg_ttl (expected ${String(this.policy.allowWriteTtl)}, found ${String(config["allow_msg_ttl"])})`,
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
        `Existing NATS KV bucket ${JSON.stringify(this.name)} has an incompatible Deckr KV policy: ${mismatches.join(", ")}. Deckr will not rewrite shared bucket policy; delete and recreate the development bucket/stream ${streamName} and restart.`,
      );
    }

    // The public KV creation options express max_age and history. If the
    // broker did not honor either field, a post-create rewrite would disguise
    // an incompatible server or configuration.
    if (!maxAgeMatches || !maxMessagesMatches) {
      throw new StateUnavailable(
        `New NATS KV bucket ${JSON.stringify(this.name)} was created with an incompatible max_age or max_msgs_per_subject policy.`,
      );
    }

    const manager = await callSync(this.connection, "jetstreamManager");
    const streams = getProperty(manager, "streams");
    const updatedConfig: Record<string, unknown> = {
      ...config,
    };
    updatedConfig["allow_msg_ttl"] = this.policy.allowWriteTtl;
    if (expectedDeleteMarkerNs !== null) {
      updatedConfig["subject_delete_marker_ttl"] = expectedDeleteMarkerNs;
    }
    try {
      await call(streams, "update", streamName, updatedConfig);
    } catch (error) {
      throw new StateUnavailable(
        `New NATS KV bucket ${JSON.stringify(this.name)} could not be configured for Deckr's current KV policy. Delete the development bucket/stream ${streamName} and restart.`,
        { cause: error },
      );
    }
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
    const kv = await this.availableKv();
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

function streamConfigFromKvStatus(status: unknown): Record<string, unknown> {
  const streamInfo = getProperty(status, "streamInfo");
  return getProperty(streamInfo, "config") as Record<string, unknown>;
}

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
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
