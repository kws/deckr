import { StateConflict, StateUnavailable } from "./errors.ts";
import { requireJsonObject, type JsonObject } from "./json.ts";
import { validateDeckrMessage, validateHeaderHints, validateSubjectHint, type DeckrMessage } from "./lanes.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateEntry,
  type StateStore,
  type StateStorePolicy,
} from "./state.ts";

export interface NatsStateStoreOptions {
  connection: unknown;
  bucket: string;
  policy?: StateStorePolicy;
}

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

  constructor(options: NatsStateStoreOptions) {
    this.connection = options.connection;
    this.name = options.bucket;
    this.policy = options.policy ?? PERSISTENT_STATE_STORE_POLICY;
  }

  async get(key: string): Promise<StateEntry | null> {
    const kv = await this.availableKv();
    try {
      const entry = await call(kv, "get", key);
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
      const rawKeys = (await call(kv, "keys")) as Iterable<unknown>;
      keys = [...rawKeys].map(String).filter((key) => key.startsWith(prefix)).sort();
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

  private async availableKv(): Promise<unknown> {
    if (this.kv !== null) {
      return this.kv;
    }
    const jetstream = callSync(this.connection, "jetstream");
    const views = getProperty(jetstream, "views");
    this.kv = await call(views, "kv", this.name, {
      history: 1,
      ...(this.policy.brokerTtlSeconds === null ? {} : { ttl: this.policy.brokerTtlSeconds * 1000 }),
    });
    return this.kv;
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

function isMissingKey(error: unknown): boolean {
  return /not found|missing|no keys/i.test(String(error instanceof Error ? error.message : error));
}

function isConflict(error: unknown): boolean {
  return /wrong.*last|revision|exists|conflict/i.test(String(error instanceof Error ? error.message : error));
}
