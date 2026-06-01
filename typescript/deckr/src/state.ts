import { StateConflict, StateUnavailable } from "./errors.ts";
import { cloneJson, type JsonObject, type JsonValue } from "./json.ts";

export interface StateStorePolicy {
  brokerTtlSeconds: number | null;
  allowWriteTtl: boolean;
  description: string;
}

export const DEFAULT_STATE_TTL_SECONDS = 30;
export const DEFAULT_STATE_RENEWAL_INTERVAL_SECONDS = 5;
export const DEFAULT_STATE_RECONCILE_SECONDS = 300;
export const DEFAULT_STATE_NOTIFICATION_BATCH_SECONDS = 1;

export const TTL_STATE_STORE_POLICY: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: DEFAULT_STATE_TTL_SECONDS,
  allowWriteTtl: true,
  description: "TTL current state",
});

export const PERSISTENT_STATE_STORE_POLICY: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: null,
  allowWriteTtl: false,
  description: "persistent current state",
});

export interface StateEntry {
  key: string;
  value: JsonObject;
  revision: number;
}

export type StateOperation = "put" | "delete" | "expire";

export interface StateChange {
  operation: StateOperation;
  key: string;
  entry?: StateEntry;
}

export interface StateStore {
  get(key: string): Promise<StateEntry | null>;
  items(prefix?: string): Promise<StateEntry[]>;
  put(key: string, value: JsonObject, options?: { ttl?: number | null }): Promise<StateEntry>;
  create(key: string, value: JsonObject, options?: { ttl?: number | null }): Promise<StateEntry>;
  update(
    key: string,
    value: JsonObject,
    options: { revision: number; ttl?: number | null },
  ): Promise<StateEntry>;
  delete(key: string, options?: { revision?: number | null }): Promise<void>;
  watch?(prefix?: string): AsyncIterable<StateChange>;
}

export interface PrefixObservation {
  entries: StateEntry[];
  confirmedMissing: Set<string>;
}

export async function observePrefixCurrent(
  state: StateStore,
  prefix = "",
  knownKeys: Iterable<string> = [],
): Promise<PrefixObservation> {
  const observed = new Map<string, StateEntry>();
  for (const entry of await state.items(prefix)) {
    observed.set(entry.key, entry);
  }
  const confirmedMissing = new Set<string>();
  for (const key of [...new Set(knownKeys)].sort()) {
    if (!key.startsWith(prefix) || observed.has(key)) {
      continue;
    }
    const current = await state.get(key);
    if (current === null) {
      confirmedMissing.add(key);
    } else {
      observed.set(key, current);
    }
  }
  return {
    entries: [...observed.values()].sort((left, right) => left.key.localeCompare(right.key)),
    confirmedMissing,
  };
}

export class MemoryStateStore implements StateStore {
  readonly name: string;
  readonly policy: StateStorePolicy;

  private revision = 0;
  private entries = new Map<string, StateEntry>();
  private watchers = new Set<MemoryWatcher>();

  constructor(options: { name?: string; policy?: StateStorePolicy } = {}) {
    this.name = options.name ?? "memory";
    this.policy = options.policy ?? PERSISTENT_STATE_STORE_POLICY;
  }

  async get(key: string): Promise<StateEntry | null> {
    const entry = this.entries.get(key);
    return entry === undefined ? null : copyEntry(entry);
  }

  async items(prefix = ""): Promise<StateEntry[]> {
    return [...this.entries.values()]
      .filter((entry) => entry.key.startsWith(prefix))
      .sort((left, right) => left.key.localeCompare(right.key))
      .map(copyEntry);
  }

  async put(key: string, value: JsonObject, options: { ttl?: number | null } = {}): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const entry = this.nextEntry(key, value);
    this.entries.set(key, entry);
    this.emit({ operation: "put", key, entry: copyEntry(entry) });
    return copyEntry(entry);
  }

  async create(key: string, value: JsonObject, options: { ttl?: number | null } = {}): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    if (this.entries.has(key)) {
      throw new StateConflict(`State key ${JSON.stringify(key)} already exists`);
    }
    const entry = this.nextEntry(key, value);
    this.entries.set(key, entry);
    this.emit({ operation: "put", key, entry: copyEntry(entry) });
    return copyEntry(entry);
  }

  async update(
    key: string,
    value: JsonObject,
    options: { revision: number; ttl?: number | null },
  ): Promise<StateEntry> {
    this.validateTtl(options.ttl ?? null);
    const current = this.entries.get(key);
    if (current === undefined) {
      throw new StateConflict(`State key ${key} is missing`);
    }
    if (current.revision !== options.revision) {
      throw new StateConflict(`State key ${key} revision changed`);
    }
    const entry = this.nextEntry(key, value);
    this.entries.set(key, entry);
    this.emit({ operation: "put", key, entry: copyEntry(entry) });
    return copyEntry(entry);
  }

  async delete(key: string, options: { revision?: number | null } = {}): Promise<void> {
    const current = this.entries.get(key);
    if (current === undefined) {
      return;
    }
    if (options.revision !== undefined && options.revision !== null && current.revision !== options.revision) {
      throw new StateConflict(`State key ${key} revision changed`);
    }
    this.entries.delete(key);
    this.emit({ operation: "delete", key });
  }

  watch(prefix = ""): AsyncIterable<StateChange> {
    const watcher = new MemoryWatcher(prefix, () => this.watchers.delete(watcher));
    this.watchers.add(watcher);
    return watcher;
  }

  private nextEntry(key: string, value: JsonObject): StateEntry {
    this.revision += 1;
    return { key, value: cloneJson(value as JsonValue) as JsonObject, revision: this.revision };
  }

  private validateTtl(ttl: number | null): void {
    if (!this.policy.allowWriteTtl && ttl !== null) {
      throw new StateUnavailable(
        `${this.policy.description} does not use write TTL; per-key TTL ${ttl} is not supported.`,
      );
    }
    if (ttl !== null && this.policy.brokerTtlSeconds !== null && Math.abs(ttl - this.policy.brokerTtlSeconds) > 0.001) {
      throw new StateUnavailable(
        `current state uses broker TTL ${this.policy.brokerTtlSeconds}; per-key TTL ${ttl} is not supported.`,
      );
    }
  }

  private emit(change: StateChange): void {
    for (const watcher of this.watchers) {
      watcher.offer(change);
    }
  }
}

class MemoryWatcher implements AsyncIterable<StateChange> {
  private queue: StateChange[] = [];
  private waits: Array<(value: IteratorResult<StateChange>) => void> = [];
  private closed = false;
  private readonly prefix: string;
  private readonly onClose: () => void;

  constructor(prefix: string, onClose: () => void) {
    this.prefix = prefix;
    this.onClose = onClose;
  }

  offer(change: StateChange): void {
    if (this.closed || !change.key.startsWith(this.prefix)) {
      return;
    }
    const copied = copyChange(change);
    const wait = this.waits.shift();
    if (wait !== undefined) {
      wait({ done: false, value: copied });
      return;
    }
    this.queue.push(copied);
  }

  [Symbol.asyncIterator](): AsyncIterator<StateChange> {
    return {
      next: () => {
        if (this.queue.length > 0) {
          return Promise.resolve({ done: false, value: this.queue.shift()! });
        }
        if (this.closed) {
          return Promise.resolve({ done: true, value: undefined });
        }
        return new Promise((resolve) => this.waits.push(resolve));
      },
      return: () => {
        this.close();
        return Promise.resolve({ done: true, value: undefined });
      },
    };
  }

  private close(): void {
    if (this.closed) {
      return;
    }
    this.closed = true;
    this.onClose();
    for (const wait of this.waits.splice(0)) {
      wait({ done: true, value: undefined });
    }
  }
}

function copyEntry(entry: StateEntry): StateEntry {
  return {
    key: entry.key,
    value: cloneJson(entry.value as JsonValue) as JsonObject,
    revision: entry.revision,
  };
}

function copyChange(change: StateChange): StateChange {
  return {
    operation: change.operation,
    key: change.key,
    ...(change.entry === undefined ? {} : { entry: copyEntry(change.entry) }),
  };
}
