import assert from "node:assert/strict";
import test from "node:test";

import { StateUnavailable } from "../src/errors.ts";
import { NatsStateStore } from "../src/nats.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateStorePolicy,
} from "../src/state.ts";

test("NatsStateStore completes policy for a bucket created by this open", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
    },
  });
  const connection = new FakeConnection(kv, { createdByOpen: true });
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await store.put("advertisements.by_feature.hardware.deck", { owner: "hw" });

  assert.equal(connection.views.opens.length, 1);
  assert.equal(connection.views.opens[0]!.options["history"], 1);
  assert.equal(connection.views.opens[0]!.options["ttl"], 30_000);
  assert.equal(
    typeof (connection.views.opens[0]!.options["metadata"] as Record<string, unknown>)[
      "deckr.kv.creation_id"
    ],
    "string",
  );
  assert.equal(connection.streams.updates.length, 1);
  assert.equal(
    connection.streams.updates[0]!.config["subject_delete_marker_ttl"],
    30_000_000_000,
  );
  assert.equal(connection.streams.updates[0]!.config["max_age"], 30_000_000_000);
  assert.equal(connection.streams.updates[0]!.config["max_msgs_per_subject"], 1);
  assert.equal(connection.streams.updates[0]!.config["allow_msg_ttl"], true);
});

test("NatsStateStore serializes concurrent first opens", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
    },
  });
  const connection = new FakeConnection(kv, { createdByOpen: true });
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await Promise.all([
    store.put("advertisements.by_feature.hardware.one", { owner: "one" }),
    store.put("advertisements.by_feature.hardware.two", { owner: "two" }),
  ]);

  assert.equal(connection.views.opens.length, 1);
  assert.equal(connection.streams.updates.length, 1);
});

test("NatsStateStore keeps TTL buckets with matching subject delete markers", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
      allow_msg_ttl: true,
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

test("NatsStateStore rejects incompatible existing TTL policy without updating", async () => {
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

  await assert.rejects(
    () => store.put("advertisements.by_feature.hardware.deck", { owner: "hw" }),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("will not rewrite shared bucket policy") &&
      error.message.includes("allow_msg_ttl") &&
      error.message.includes("subject_delete_marker_ttl"),
  );
  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore treats a create race winner as an existing bucket", async () => {
  const kv = new FakeKv({
    ttl: 12_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 12_000_000_000,
      max_msgs_per_subject: 1,
      allow_msg_ttl: true,
      subject_delete_marker_ttl: 12_000_000_000,
    },
  });
  const connection = new FakeConnection(kv, { failFirstOpen: true });
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await assert.rejects(
    () => store.put("advertisements.by_feature.hardware.deck", { owner: "hw" }),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("will not rewrite shared bucket policy"),
  );
  assert.equal(connection.views.opens.length, 2);
  assert.deepEqual(connection.views.opens[1]!.options, { bindOnly: true });
  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore rejects incompatible existing persistent policy", async () => {
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

  await assert.rejects(
    () => store.put("contracts.main.1.meta", { state: "open" }),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("will not rewrite shared bucket policy") &&
      error.message.includes("max_age") &&
      error.message.includes("max_msgs_per_subject"),
  );
  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore rejects unexpected per-message TTL capability", async () => {
  const kv = new FakeKv({
    ttl: 0,
    history: 1,
    config: {
      name: "KV_deckr_concord_contract_v1",
      max_age: 0,
      max_msgs_per_subject: 1,
      allow_msg_ttl: true,
    },
  });
  const connection = new FakeConnection(kv);
  const store = new NatsStateStore({
    connection,
    bucket: "deckr_concord_contract_v1",
    policy: PERSISTENT_STATE_STORE_POLICY,
  });

  await assert.rejects(
    () => store.get("contracts.main.1.meta"),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("allow_msg_ttl (expected false"),
  );
  assert.equal(connection.streams.updates.length, 0);
});

const ttlPolicy: StateStorePolicy = Object.freeze({
  brokerTtlSeconds: 30,
  allowWriteTtl: true,
  description: "test ttl state",
});

class FakeConnection {
  readonly views: FakeViews;
  readonly streams: FakeStreams;

  constructor(
    kv: FakeKv,
    options: { createdByOpen?: boolean; failFirstOpen?: boolean } = {},
  ) {
    this.views = new FakeViews(kv, options);
    this.streams = new FakeStreams(kv);
  }

  jetstream(): { views: FakeViews } {
    return { views: this.views };
  }

  jetstreamManager(): { streams: FakeStreams } {
    return { streams: this.streams };
  }
}

class FakeViews {
  readonly opens: { bucket: string; options: Record<string, unknown> }[] = [];
  private readonly store: FakeKv;
  private readonly createdByOpen: boolean;
  private failFirstOpen: boolean;

  constructor(
    store: FakeKv,
    options: { createdByOpen?: boolean; failFirstOpen?: boolean },
  ) {
    this.store = store;
    this.createdByOpen = options.createdByOpen ?? false;
    this.failFirstOpen = options.failFirstOpen ?? false;
  }

  async kv(
    bucket: string,
    options: Record<string, unknown>,
  ): Promise<FakeKv> {
    this.store.bucket = bucket;
    this.opens.push({ bucket, options });
    if (this.failFirstOpen) {
      this.failFirstOpen = false;
      throw new Error("stream name already in use");
    }
    if (this.createdByOpen && options["bindOnly"] !== true) {
      this.store.markCreated(options);
    }
    return this.store;
  }
}

class FakeStreams {
  readonly updates: { name: string; config: Record<string, unknown> }[] = [];
  private readonly store: FakeKv;

  constructor(store: FakeKv) {
    this.store = store;
  }

  async update(
    name: string,
    config: Record<string, unknown>,
  ): Promise<void> {
    this.updates.push({ name, config });
    this.store.replaceConfig(config);
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

  markCreated(options: Record<string, unknown>): void {
    this.statusValue.config["metadata"] = options["metadata"];
  }

  replaceConfig(config: Record<string, unknown>): void {
    this.statusValue.config = config;
  }
}
