#!/usr/bin/env node

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIONS_LANE,
  DECKR_NATS_HEADERS,
  DEFAULT_DISCOVERY_STATE_BUCKET,
  DEFAULT_LEASE_STATE_BUCKET,
  HARDWARE_MESSAGES_LANE,
  LANE_SUBJECT_PREFIX,
  LANE_SUBJECT_TEMPLATE,
  LANE_SUBSCRIBE_TEMPLATE,
  NATS_BINDING_PATH,
  NATS_BINDING_SCHEMA_ID,
  REQUIRED_DECKR_NATS_HEADERS,
  SERVICES_LANE,
  STATE_RENEWAL_INTERVAL_SECONDS,
  STATE_TTL_SECONDS,
  actionProviderCatalogKey,
  contextSubject,
  decodeKeyToken,
  defaultContractRoot,
  deviceClaimKey,
  encodeKeyToken,
  hardwareInventoryKey,
  hardwareSubjectForCapability,
  headersFor,
  messageIsExpiredAt,
  messageIsDeliverableAt,
  messageTargetsEndpoint,
  parseActionProviderCatalogKey,
  parseDeviceClaimKey,
  parseEndpointAddress,
  parseHardwareInventoryKey,
  parsePresenceEndpointKey,
  parseServiceCatalogKey,
  parseServiceStatusKey,
  parseServiceViewKey,
  parseSettingsTargetKey,
  payloadJsonString,
  presenceEndpointKey,
  readJsonFile,
  serviceCatalogKey,
  serviceStatusKey,
  serviceViewKey,
  settingsTargetKey,
  subjectFor,
  subscribeSubjectForLane,
  validateLaneMessage,
  createContractAjv,
  type ContractManifest,
  type DeckrMessage,
  type EntitySubject,
  type JsonObject,
} from "../index.js";
import type { AnySchema } from "ajv";

const REQUIRED_GROUP_IDS = [
  "artifacts.manifest",
  "schemas.fixtures",
  "vectors.keys",
  "vectors.identity",
  "messages.actions",
  "messages.hardware",
  "messages.services",
  "runtime.lane",
  "substrate.nats",
] as const;

interface CliArgs {
  contractRoot?: string;
  output?: string;
}

interface Report {
  schema: "dev.deckr.interop.report.v1";
  contractVersion: string;
  specVersion: string;
  implementation: {
    language: "typescript";
    package: "@deckr/core";
    version: string;
  };
  roles: ["validator"];
  groups: Array<ReturnType<GroupResult["toJSON"]>>;
  summary: {
    status: "passed" | "failed";
    passed: number;
    failed: number;
    skipped: number;
  };
}

class GroupResult {
  passed = 0;
  failed = 0;
  skipped = 0;
  diagnostics: string[] = [];

  constructor(readonly id: string) {}

  pass(): void {
    this.passed += 1;
  }

  fail(diagnostic: string): void {
    this.failed += 1;
    this.diagnostics.push(diagnostic);
  }

  toJSON(): {
    id: string;
    status: "passed" | "failed" | "skipped";
    passed: number;
    failed: number;
    skipped: number;
    diagnostics: string[];
  } {
    return {
      id: this.id,
      status:
        this.failed > 0
          ? "failed"
          : this.passed === 0 && this.skipped > 0
            ? "skipped"
            : "passed",
      passed: this.passed,
      failed: this.failed,
      skipped: this.skipped,
      diagnostics: this.diagnostics,
    };
  }
}

async function main(): Promise<number> {
  const args = parseArgs(process.argv.slice(2));
  const contractRoot = args.contractRoot ?? defaultContractRoot();
  const report = await runConformance(contractRoot);
  const output = `${JSON.stringify(report, null, 2)}\n`;
  if (args.output === undefined) {
    process.stdout.write(output);
  } else {
    await mkdir(dirname(args.output), { recursive: true });
    await writeFile(args.output, output, "utf8");
  }
  return report.summary.failed === 0 ? 0 : 1;
}

async function runConformance(contractRoot: string): Promise<Report> {
  const manifest = await readJsonFile<ContractManifest>(
    join(contractRoot, "manifest.json"),
  );
  const groups = [
    await checkManifest(contractRoot, manifest),
    await checkFixtures(contractRoot, manifest),
    await checkKeyVectors(contractRoot),
    await checkIdentityVectors(contractRoot),
    await checkLaneMessages(
      contractRoot,
      manifest,
      "messages.actions",
      "schemas/actions/actions.v1.schema.json",
      ACTIONS_LANE,
    ),
    await checkLaneMessages(
      contractRoot,
      manifest,
      "messages.hardware",
      "schemas/hardware/hardware-messages.v1.schema.json",
      HARDWARE_MESSAGES_LANE,
    ),
    await checkLaneMessages(
      contractRoot,
      manifest,
      "messages.services",
      "schemas/services/services.v1.schema.json",
      SERVICES_LANE,
    ),
    await checkLaneRuntimeVectors(contractRoot),
    await checkNatsLaneVectors(contractRoot),
  ];
  const serializedGroups = groups.map((group) => group.toJSON());
  const summary = {
    passed: groups.reduce((total, group) => total + group.passed, 0),
    failed: groups.reduce((total, group) => total + group.failed, 0),
    skipped: groups.reduce((total, group) => total + group.skipped, 0),
    status: "passed" as "passed" | "failed",
  };
  summary.status = summary.failed > 0 ? "failed" : "passed";
  return {
    schema: "dev.deckr.interop.report.v1",
    contractVersion: String(manifest.contractVersion),
    specVersion: String(manifest.specVersion),
    implementation: {
      language: "typescript",
      package: "@deckr/core",
      version: await packageVersion(),
    },
    roles: ["validator"],
    groups: serializedGroups,
    summary,
  };
}

async function checkManifest(
  contractRoot: string,
  manifest: ContractManifest,
): Promise<GroupResult> {
  const group = new GroupResult("artifacts.manifest");
  try {
    await readJsonFile(join(contractRoot, "manifest.json"));
    group.pass();
  } catch (error) {
    group.fail(`Missing manifest.json: ${error}`);
  }
  try {
    await readJsonFile(join(contractRoot, "asyncapi.json"));
    group.pass();
  } catch (error) {
    group.fail(`Missing asyncapi.json: ${error}`);
  }
  if (manifest.bundle === "deckr-contract-v1") {
    group.pass();
  } else {
    group.fail(`Unexpected bundle id: ${manifest.bundle}`);
  }
  if (JSON.stringify([...REQUIRED_GROUP_IDS]) === JSON.stringify(plannedGroups())) {
    group.pass();
  } else {
    group.fail("Static runner group ids drifted from required ids");
  }
  return group;
}

async function checkFixtures(
  contractRoot: string,
  manifest: ContractManifest,
): Promise<GroupResult> {
  const group = new GroupResult("schemas.fixtures");
  for (const artifact of manifest.artifacts) {
    if (artifact.kind !== "fixture") {
      continue;
    }
    try {
      if (artifact.schemaPath === undefined) {
        throw new Error("fixture artifact lacks schemaPath");
      }
      const schema = await readJsonFile(join(contractRoot, artifact.schemaPath));
      const fixture = await readJsonFile(join(contractRoot, artifact.path));
      const validate = createContractAjv().compile(schema as AnySchema);
      const valid = validate(fixture);
      if (artifact.valid === true && !valid) {
        group.fail(`${artifact.path}: expected valid, got errors`);
      } else if (artifact.valid === false && valid) {
        group.fail(`${artifact.path}: expected invalid, got no errors`);
      } else {
        group.pass();
      }
    } catch (error) {
      group.fail(`${artifact.path}: validation failed: ${error}`);
    }
  }
  return group;
}

async function checkKeyVectors(contractRoot: string): Promise<GroupResult> {
  const group = new GroupResult("vectors.keys");
  const keyVectors = await readJsonFile<{ cases: JsonObject[] }>(
    join(contractRoot, "vectors/key-tokens.v1.json"),
  );
  for (const testCase of keyVectors.cases) {
    if (testCase.valid === false) {
      checkInvalidKeyTokenCase(group, testCase);
      continue;
    }
    const encoded = encodeKeyToken(String(testCase.raw));
    const decoded = decodeKeyToken(String(testCase.encoded));
    if (encoded !== testCase.encoded) {
      group.fail(`${String(testCase.raw)}: encoded as ${encoded}`);
    } else if (decoded !== testCase.decoded) {
      group.fail(`${String(testCase.encoded)}: decoded as ${decoded}`);
    } else {
      group.pass();
    }
  }

  const stateVectors = await readJsonFile<{ cases: JsonObject[] }>(
    join(contractRoot, "vectors/state-keys.v1.json"),
  );
  for (const testCase of stateVectors.cases) {
    if (testCase.valid === false) {
      checkInvalidStateKeyCase(group, testCase);
      continue;
    }
    try {
      const result = stateKeyCase(testCase);
      if (result.key !== testCase.key) {
        group.fail(`${String(testCase.id)}: key ${result.key} != ${String(testCase.key)}`);
      } else if (!jsonEquivalent(result.parsed, testCase.parsed)) {
        group.fail(`${String(testCase.id)}: parsed ${JSON.stringify(result.parsed)}`);
      } else {
        group.pass();
      }
    } catch (error) {
      group.fail(`${String(testCase.id)}: helper failed: ${error}`);
    }
  }
  return group;
}

function checkInvalidKeyTokenCase(group: GroupResult, testCase: JsonObject): void {
  try {
    if (testCase.operation === "decode_key_token") {
      decodeKeyToken(String(testCase.encoded));
    } else {
      throw new Error(`unknown invalid key-token operation ${String(testCase.operation)}`);
    }
  } catch {
    group.pass();
    return;
  }
  group.fail(`${String(testCase.id)}: invalid key-token case accepted`);
}

function checkInvalidStateKeyCase(group: GroupResult, testCase: JsonObject): void {
  try {
    const parsed = invalidStateKeyCase(testCase);
    if (parsed === null) {
      group.pass();
    } else {
      group.fail(`${String(testCase.id)}: invalid state-key case parsed`);
    }
  } catch {
    group.pass();
  }
}

function invalidStateKeyCase(testCase: JsonObject): unknown {
  const key = String(testCase.key);
  switch (testCase.helper) {
    case "parse_presence_endpoint_key":
      return parsePresenceEndpointKey(key);
    case "parse_hardware_inventory_key":
      return parseHardwareInventoryKey(key);
    case "parse_device_claim_key":
      return parseDeviceClaimKey(key);
    case "parse_action_provider_catalog_key":
      return parseActionProviderCatalogKey(key);
    case "parse_service_catalog_key":
      return parseServiceCatalogKey(key);
    case "parse_service_status_key":
      return parseServiceStatusKey(key);
    case "parse_service_view_key":
      return parseServiceViewKey(key);
    case "parse_settings_target_key":
      return parseSettingsTargetKey(key);
    default:
      throw new Error(`unknown invalid state-key helper ${String(testCase.helper)}`);
  }
}

function stateKeyCase(testCase: JsonObject): { key: string; parsed: JsonObject } {
  const input = testCase.input as JsonObject;
  switch (testCase.helper) {
    case "presence_endpoint_key": {
      const key = presenceEndpointKey({
        lane: String(input.lane),
        endpoint: String(input.endpoint),
      });
      const parsed = parsePresenceEndpointKey(key);
      if (parsed === null) {
        throw new Error("presence endpoint key did not parse");
      }
      return { key, parsed };
    }
    case "hardware_inventory_key": {
      const key = hardwareInventoryKey(String(input.managerId));
      const parsed = parseHardwareInventoryKey(key);
      if (parsed === null) {
        throw new Error("hardware inventory key did not parse");
      }
      return { key, parsed };
    }
    case "device_claim_key": {
      const key = deviceClaimKey({
        managerId: String(input.managerId),
        deviceId: String(input.deviceId),
      });
      const parsed = parseDeviceClaimKey(key);
      if (parsed === null) {
        throw new Error("device claim key did not parse");
      }
      return { key, parsed };
    }
    case "action_provider_catalog_key": {
      const key = actionProviderCatalogKey(String(input.providerInstanceId));
      const parsed = parseActionProviderCatalogKey(key);
      if (parsed === null) {
        throw new Error("action provider catalog key did not parse");
      }
      return { key, parsed };
    }
    case "service_catalog_key": {
      const key = serviceCatalogKey(String(input.serviceId));
      const parsed = parseServiceCatalogKey(key);
      if (parsed === null) {
        throw new Error("service catalog key did not parse");
      }
      return { key, parsed };
    }
    case "service_status_key": {
      const key = serviceStatusKey(String(input.serviceId));
      const parsed = parseServiceStatusKey(key);
      if (parsed === null) {
        throw new Error("service status key did not parse");
      }
      return { key, parsed };
    }
    case "service_view_key": {
      const tokens = input.tokens as string[];
      const key = serviceViewKey(
        String(input.serviceId),
        String(input.serviceNamespace),
        ...tokens,
      );
      const parsed = parseServiceViewKey(key);
      if (parsed === null) {
        throw new Error("service view key did not parse");
      }
      return { key, parsed };
    }
    case "settings_target_key": {
      const target = parseSettingsTargetKey(String(testCase.key));
      if (target === null) {
        throw new Error("settings target key did not parse");
      }
      const key = settingsTargetKey(target);
      return { key, parsed: { target: target as unknown as JsonObject } };
    }
    default:
      throw new Error(`unknown state-key helper ${String(testCase.helper)}`);
  }
}

async function checkIdentityVectors(contractRoot: string): Promise<GroupResult> {
  const group = new GroupResult("vectors.identity");
  const vector = await readJsonFile<{
    endpointCases: JsonObject[];
    subjectCases: JsonObject[];
  }>(join(contractRoot, "vectors/identity.v1.json"));
  for (const testCase of vector.endpointCases) {
    const parsed = parseEndpointAddress(String(testCase.input));
    if (testCase.valid === false) {
      if (parsed === null) {
        group.pass();
      } else {
        group.fail(`${String(testCase.id)}: invalid endpoint accepted`);
      }
    } else if (
      parsed?.family === testCase.family &&
      parsed.endpointId === testCase.endpointId
    ) {
      group.pass();
    } else {
      group.fail(`${String(testCase.id)}: parsed incorrectly`);
    }
  }
  for (const testCase of vector.subjectCases) {
    try {
      const subject = subjectCase(testCase);
      if (jsonEquivalent(subject, testCase.subject)) {
        group.pass();
      } else {
        group.fail(`${String(testCase.id)}: subject ${JSON.stringify(subject)}`);
      }
    } catch (error) {
      group.fail(`${String(testCase.id)}: helper failed: ${error}`);
    }
  }
  return group;
}

function subjectCase(testCase: JsonObject): EntitySubject {
  const input = testCase.input as JsonObject;
  switch (testCase.helper) {
    case "context_subject":
      return contextSubject(String(input.contextId), {
        providerInstanceId: stringOrUndefined(input.providerInstanceId),
        providerId: stringOrUndefined(input.providerId),
        configId: stringOrUndefined(input.configId),
        actionInstanceId: stringOrUndefined(input.actionInstanceId),
        bindingId: stringOrUndefined(input.bindingId),
      });
    case "hardware_subject_for_capability": {
      const deviceRef = input.deviceRef as JsonObject;
      return hardwareSubjectForCapability({
        capabilityId: String(input.capabilityId),
        controlId: String(input.controlId),
        deviceId: String(deviceRef.deviceId),
        managerId: String(deviceRef.managerId),
      });
    }
    default:
      throw new Error(`unknown subject helper ${String(testCase.helper)}`);
  }
}

async function checkLaneMessages(
  contractRoot: string,
  manifest: ContractManifest,
  groupId: string,
  schemaPath: string,
  lane: string,
): Promise<GroupResult> {
  const group = new GroupResult(groupId);
  for (const artifact of manifest.artifacts) {
    if (
      artifact.kind !== "fixture" ||
      artifact.schemaPath !== schemaPath ||
      artifact.valid !== true
    ) {
      continue;
    }
    try {
      const message = await readJsonFile<DeckrMessage>(join(contractRoot, artifact.path));
      const validation = validateLaneMessage(message);
      if (!validation.ok) {
        throw new Error(validation.reason);
      }
      if (message.lane !== lane) {
        throw new Error(`expected lane ${lane}, got ${message.lane}`);
      }
      group.pass();
    } catch (error) {
      group.fail(`${artifact.path}: message helper rejected: ${error}`);
    }
  }
  return group;
}

async function checkLaneRuntimeVectors(contractRoot: string): Promise<GroupResult> {
  const group = new GroupResult("runtime.lane");
  const vector = await readJsonFile<{ cases: JsonObject[] }>(
    join(contractRoot, "vectors/lane-runtime.v1.json"),
  );
  for (const testCase of vector.cases) {
    try {
      const message = testCase.message === undefined
        ? await readJsonFile<DeckrMessage>(join(contractRoot, String(testCase.fixture)))
        : testCase.message as unknown as DeckrMessage;
      const now = new Date(String(testCase.now));
      const expired = messageIsExpiredAt(message, now);
      const targetsEndpoint = messageTargetsEndpoint(message, String(testCase.endpoint));
      const deliverable = messageIsDeliverableAt(
        message,
        String(testCase.endpoint),
        String(testCase.endpointSessionId),
        now,
      );
      if (expired !== testCase.expired) {
        group.fail(`${String(testCase.id)}: expired ${expired}`);
      } else if (targetsEndpoint !== testCase.targetsEndpoint) {
        group.fail(`${String(testCase.id)}: targetsEndpoint ${targetsEndpoint}`);
      } else if (deliverable !== testCase.deliverable) {
        group.fail(`${String(testCase.id)}: deliverable ${deliverable}`);
      } else {
        group.pass();
      }
    } catch (error) {
      group.fail(`${String(testCase.id)}: helper failed: ${error}`);
    }
  }
  return group;
}

async function checkNatsLaneVectors(contractRoot: string): Promise<GroupResult> {
  const group = new GroupResult("substrate.nats");
  try {
    const binding = await readJsonFile<JsonObject>(join(contractRoot, NATS_BINDING_PATH));
    const laneMessages = binding.laneMessages as JsonObject;
    const lanes = laneMessages.lanes as Record<string, JsonObject>;
    const currentState = binding.currentState as JsonObject;
    const buckets = currentState.buckets as Record<string, JsonObject>;
    const headers = laneMessages.headers as JsonObject[];
    const checks = [
      binding.schema === NATS_BINDING_SCHEMA_ID,
      laneMessages.subjectRoot === LANE_SUBJECT_PREFIX,
      laneMessages.publishSubjectTemplate === LANE_SUBJECT_TEMPLATE,
      laneMessages.subscribeSubjectTemplate === LANE_SUBSCRIBE_TEMPLATE,
      JSON.stringify(headers.map((header) => header.name)) ===
        JSON.stringify([...DECKR_NATS_HEADERS]),
      JSON.stringify(
        headers.filter((header) => header.required).map((header) => header.name),
      ) === JSON.stringify([...REQUIRED_DECKR_NATS_HEADERS]),
      lanes[HARDWARE_MESSAGES_LANE]?.subscribeSubject ===
        subscribeSubjectForLane(HARDWARE_MESSAGES_LANE),
      buckets.lease?.name === DEFAULT_LEASE_STATE_BUCKET,
      buckets.lease?.brokerTtlSeconds === STATE_TTL_SECONDS,
      buckets.lease?.renewalIntervalSeconds === STATE_RENEWAL_INTERVAL_SECONDS,
      buckets.discovery?.name === DEFAULT_DISCOVERY_STATE_BUCKET,
      buckets.discovery?.brokerTtlSeconds === null,
    ];
    if (checks.every(Boolean)) {
      group.pass();
    } else {
      group.fail("NATS binding artifact disagrees with TypeScript helpers");
    }
  } catch (error) {
    group.fail(`NATS binding check failed: ${error}`);
  }

  const vector = await readJsonFile<{ cases: JsonObject[] }>(
    join(contractRoot, "vectors/nats-lane.v1.json"),
  );
  for (const testCase of vector.cases) {
    try {
      const message = await readJsonFile<DeckrMessage>(
        join(contractRoot, String(testCase.fixture)),
      );
      const subject = subjectFor(message);
      const headers = headersFor(message);
      const payloadUtf8 = payloadJsonString(message);
      if (subject !== testCase.subject) {
        group.fail(`${String(testCase.id)}: subject ${subject}`);
      } else if (!jsonEquivalent(headers, testCase.headers)) {
        group.fail(`${String(testCase.id)}: headers ${JSON.stringify(headers)}`);
      } else if (payloadUtf8 !== testCase.payloadUtf8) {
        group.fail(`${String(testCase.id)}: payloadUtf8 differed`);
      } else {
        group.pass();
      }
    } catch (error) {
      group.fail(`${String(testCase.id)}: helper failed: ${error}`);
    }
  }
  return group;
}

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--contract-root") {
      args.contractRoot = argv[++index];
    } else if (arg === "--output") {
      args.output = argv[++index];
    } else {
      throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

function plannedGroups(): string[] {
  return [...REQUIRED_GROUP_IDS];
}

async function packageVersion(): Promise<string> {
  const packageJson = await readJsonFile<{ version: string }>(
    fileURLToPath(new URL("../../package.json", import.meta.url)),
  );
  return packageJson.version;
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function jsonEquivalent(left: unknown, right: unknown): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((error: unknown) => {
    console.error(error);
    process.exitCode = 1;
  });
