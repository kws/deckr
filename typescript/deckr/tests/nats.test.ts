import assert from "node:assert/strict";
import test from "node:test";

import { StateUnavailable } from "../src/errors.ts";
import {
  NATS_KV_NON_AUTHORITATIVE_CONFIG_FIELDS,
  NATS_KV_POLICY_VECTOR_FIELDS,
  NatsStateStore,
} from "../src/nats.ts";
import {
  PERSISTENT_STATE_STORE_POLICY,
  type StateStorePolicy,
} from "../src/state.ts";

test("NATS KV policy vector fields are explicit and complete", () => {
  assert.deepEqual(NATS_KV_POLICY_VECTOR_FIELDS, [
    "max_age",
    "max_msgs_per_subject",
    "allow_msg_ttl",
    "subject_delete_marker_ttl",
  ]);
  assert.deepEqual(NATS_KV_NON_AUTHORITATIVE_CONFIG_FIELDS, [
    "allow_direct",
    "metadata",
    "storage",
  ]);
});

test("NatsStateStore atomically creates a TTL bucket before binding", async () => {
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
  const store = await NatsStateStore.open({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await store.put("advertisements.by_feature.hardware.deck", { owner: "hw" });

  assert.deepEqual(connection.views.opens, [
    {
      bucket: "deckr_beacon_advertisement_v1",
      options: { bindOnly: true },
    },
  ]);
  assert.equal(connection.streams.adds.length, 1);
  assert.equal(connection.streams.updates.length, 0);
  const created = connection.streams.adds[0]!.config;
  assert.equal(created["name"], "KV_deckr_beacon_advertisement_v1");
  assert.deepEqual(created["subjects"], [
    "$KV.deckr_beacon_advertisement_v1.>",
  ]);
  assert.equal(created["retention"], "limits");
  assert.equal(created["storage"], "file");
  assert.equal(created["discard"], "new");
  assert.equal(created["max_consumers"], -1);
  assert.equal(created["max_msgs"], -1);
  assert.equal(created["max_age"], 30_000_000_000);
  assert.equal(created["max_bytes"], -1);
  assert.equal(created["max_msg_size"], -1);
  assert.equal(created["max_msgs_per_subject"], 1);
  assert.equal(created["duplicate_window"], 120_000_000_000);
  assert.equal(created["deny_delete"], true);
  assert.equal(created["allow_rollup_hdrs"], true);
  assert.equal(created["allow_direct"], true);
  assert.equal(created["num_replicas"], 1);
  assert.equal(created["allow_msg_ttl"], true);
  assert.equal(created["subject_delete_marker_ttl"], 30_000_000_000);
  assert.equal(
    typeof (created["metadata"] as Record<string, unknown>)[
      "deckr.kv.creation_id"
    ],
    "string",
  );
});

test("NatsStateStore atomically creates persistent policy with message TTL disabled", async () => {
  const kv = new FakeKv({
    ttl: 0,
    history: 1,
    config: {
      name: "KV_deckr_concord_contract_v1",
    },
  });
  const connection = new FakeConnection(kv, { createdByOpen: true });

  await NatsStateStore.open({
    connection,
    bucket: "deckr_concord_contract_v1",
    policy: PERSISTENT_STATE_STORE_POLICY,
  });

  assert.equal(connection.streams.adds.length, 1);
  assert.equal(connection.streams.updates.length, 0);
  const created = connection.streams.adds[0]!.config;
  assert.equal(created["max_age"], 0);
  assert.equal(created["max_msgs_per_subject"], 1);
  assert.equal(created["allow_msg_ttl"], false);
  assert.equal("subject_delete_marker_ttl" in created, false);
  assert.deepEqual(connection.views.opens[0]!.options, { bindOnly: true });
});

test("NatsStateStore opens once and reuses its validated handle", async () => {
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
  const store = await NatsStateStore.open({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await Promise.all([
    store.put("advertisements.by_feature.hardware.one", { owner: "one" }),
    store.put("advertisements.by_feature.hardware.two", { owner: "two" }),
  ]);

  assert.equal(connection.views.opens.length, 1);
  assert.equal(connection.streams.adds.length, 1);
  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore ignores non-authoritative fields on compatible existing buckets", async () => {
  const kv = new FakeKv({
    ttl: 30_000,
    history: 1,
    config: {
      name: "KV_deckr_beacon_advertisement_v1",
      max_age: 30_000_000_000,
      max_msgs_per_subject: 1,
      allow_msg_ttl: true,
      subject_delete_marker_ttl: 30_000_000_000,
      allow_direct: false,
      storage: "memory",
      metadata: { "created-by": "another-runtime" },
    },
  });
  const connection = new FakeConnection(kv);
  const store = await NatsStateStore.open({
    connection,
    bucket: "deckr_beacon_advertisement_v1",
    policy: ttlPolicy,
  });

  await store.put("advertisements.by_feature.hardware.deck", { owner: "hw" });

  assert.equal(connection.streams.adds.length, 1);
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
  await assert.rejects(
    () => NatsStateStore.open({
      connection,
      bucket: "deckr_beacon_advertisement_v1",
      policy: ttlPolicy,
    }),
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
  const connection = new FakeConnection(kv, { createRace: true });
  await assert.rejects(
    () => NatsStateStore.open({
      connection,
      bucket: "deckr_beacon_advertisement_v1",
      policy: ttlPolicy,
    }),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("will not rewrite shared bucket policy"),
  );
  assert.equal(connection.streams.adds.length, 1);
  assert.equal(connection.views.opens.length, 1);
  assert.deepEqual(connection.views.opens[0]!.options, { bindOnly: true });
  assert.equal(connection.streams.updates.length, 0);
});

test("NatsStateStore never rewrites a newly created bucket whose atomic policy was not honored", async () => {
  const kv = new FakeKv({
    ttl: 0,
    history: 1,
    config: {
      name: "KV_deckr_concord_contract_v1",
    },
  });
  const connection = new FakeConnection(kv, {
    createdByOpen: true,
    createdConfigOverrides: { allow_msg_ttl: true },
  });

  await assert.rejects(
    () => NatsStateStore.open({
      connection,
      bucket: "deckr_concord_contract_v1",
      policy: PERSISTENT_STATE_STORE_POLICY,
    }),
    (error: unknown) =>
      error instanceof StateUnavailable &&
      error.message.includes("New NATS KV bucket") &&
      error.message.includes("allow_msg_ttl (expected false"),
  );
  assert.equal(connection.streams.adds.length, 1);
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
  await assert.rejects(
    () => NatsStateStore.open({
      connection,
      bucket: "deckr_concord_contract_v1",
      policy: PERSISTENT_STATE_STORE_POLICY,
    }),
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
  await assert.rejects(
    () => NatsStateStore.open({
      connection,
      bucket: "deckr_concord_contract_v1",
      policy: PERSISTENT_STATE_STORE_POLICY,
    }),
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
    options: {
      createdByOpen?: boolean;
      createRace?: boolean;
      createdConfigOverrides?: Record<string, unknown>;
    } = {},
  ) {
    this.views = new FakeViews(kv);
    this.streams = new FakeStreams(kv, options);
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

  constructor(store: FakeKv) {
    this.store = store;
  }

  async kv(
    bucket: string,
    options: Record<string, unknown>,
  ): Promise<FakeKv> {
    this.store.bucket = bucket;
    this.opens.push({ bucket, options });
    return this.store;
  }
}

class FakeStreams {
  readonly adds: { config: Record<string, unknown> }[] = [];
  readonly updates: { name: string; config: Record<string, unknown> }[] = [];
  private readonly store: FakeKv;
  private readonly createdByOpen: boolean;
  private readonly createRace: boolean;
  private readonly createdConfigOverrides: Record<string, unknown>;

  constructor(
    store: FakeKv,
    options: {
      createdByOpen?: boolean;
      createRace?: boolean;
      createdConfigOverrides?: Record<string, unknown>;
    },
  ) {
    this.store = store;
    this.createdByOpen = options.createdByOpen ?? false;
    this.createRace = options.createRace ?? false;
    this.createdConfigOverrides = options.createdConfigOverrides ?? {};
  }

  async add(config: Record<string, unknown>): Promise<void> {
    this.adds.push({ config });
    if (!this.createdByOpen || this.createRace) {
      throw new Error("stream name already in use");
    }
    this.store.replaceConfig({
      ...config,
      ...this.createdConfigOverrides,
    });
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

  replaceConfig(config: Record<string, unknown>): void {
    this.statusValue.config = config;
  }
}
