import assert from "node:assert/strict";
import test from "node:test";

import { NatsStateStore } from "../src/nats.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateStorePolicy,
} from "../src/state.ts";

test("NatsStateStore updates TTL buckets with subject delete markers", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
    },
  });
  const connection = new FakeConnection(kv);
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await store.put("advertisements.by_feature.hardware.deck", { owner: "hw" });

  assert.deepEqual(connection.views.openOptions, { history: 1, ttl: 30_000 });
  assert.equal(connection.streams.updates.length, 1);
  assert.equal(
    connection.streams.updates[0]!.config["subject_delete_marker_ttl"],
    30_000_000_000,
  );
  assert.equal(connection.streams.updates[0]!.config["max_age"], 30_000_000_000);
  assert.equal(connection.streams.updates[0]!.config["max_msgs_per_subject"], 1);
});

test("NatsStateStore keeps TTL buckets with matching subject delete markers", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
      subject_delete_marker_ttl: 30_000_000_000,
    },
  });
  const connection = new FakeConnection(kv);
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await store.put("advertisements.by_feature.hardware.deck", { owner: "hw" });

  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore leaves persistent marker config untouched", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 2,
    config: {
      name: "KV_deckr_concord_contract_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 2,
      subject_delete_marker_ttl: 12_000_000_000,
    },
  });
  const connection = new FakeConnection(kv);
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_concord_contract_v1",
    policy: PERSISTENT_STATE_STORE_POLICY,
  });

  await store.put("contracts.main.1.meta", { state: "open" });

  assert.equal(connection.streams.updates.length, 1);
  assert.equal(connection.streams.updates[0]!.config["max_age"], 0);
  assert.equal(
    connection.streams.updates[0]!.config["subject_delete_marker_ttl"],
    12_000_000_000,
  );
});

const ttlPolicy: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: 30,
  allowWriteTtl: true,
  description: "test ttl state",
});

class FakeConnection {
  readonly views: FakeViews;
  readonly streams = new FakeStreams();

  constructor(kv: FakeKv) {
    this.views = new FakeViews(kv);
  }

  jetstream(): { views: FakeViews } {
    return { views: this.views };
  }

  jetstreamManager(): { streams: FakeStreams } {
    return { streams: this.streams };
  }
}

class FakeViews {
  openOptions: Record<string, unknown> | null = null;
  private readonly store: FakeKv;

  constructor(store: FakeKv) {
    this.store = store;
  }

  async kv(
    bucket: string,
    options: Record<string, unknown>,
  ): Promise<FakeKv> {
    this.store.bucket = bucket;
    this.openOptions = options;
    return this.store;
  }
}

class FakeStreams {
  readonly updates: { name: string; config: Record<string, unknown> }[] = [];

  async update(
    name: string,
    config: Record<string, unknown>,
  ): Promise<void> {
    this.updates.push({ name, config });
  }
}

class FakeKv {
  bucket = "";
  revision = 0;
  private readonly statusValue: {
    ttl: number;
    history: number;
    config: Record<string, unknown>;
  };

  constructor(statusValue: {
    ttl: number;
    history: number;
    config: Record<string, unknown>;
  }) {
    this.statusValue = statusValue;
  }

  async status(): Promise<{
    ttl: number;
    history: number;
    streamInfo: { config: Record<string, unknown> };
  }> {
    return {
      ttl: this.statusValue.ttl,
      history: this.statusValue.history,
      streamInfo: { config: this.statusValue.config },
    };
  }

  async put(_key: string, _value: string): Promise<number> {
    this.revision += 1;
    return this.revision;
  }
}
