import { randomUUID } from "node:crypto";

import type { JsonObject } from "./json.js";

export const ACTIONS_LANE = "actions";
export const HARDWARE_MESSAGES_LANE = "hardware_messages";
export const SERVICES_LANE = "services";

export const CORE_LANES = [
  ACTIONS_LANE,
  HARDWARE_MESSAGES_LANE,
  SERVICES_LANE,
] as const;

export type CoreLane = (typeof CORE_LANES)[number];
export type EndpointFamily =
  | "action_provider"
  | "controller"
  | "hardware_manager"
  | "service";

const ENDPOINT_FAMILIES = new Set<string>([
  "action_provider",
  "controller",
  "hardware_manager",
  "service",
]);
const PROVIDER_INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const RESERVED_ACTION_PROVIDER_INSTANCE_IDS = new Set([
  "dev.deckr.controller.builtin",
]);

export interface ParsedEndpointAddress {
  family: EndpointFamily;
  endpointId: string;
}

export interface EndpointTarget {
  targetType: "endpoint";
  endpoint: string;
}

export interface BroadcastTarget {
  targetType: "broadcast";
  scope: string;
  endpointFamily: EndpointFamily | string;
  domain?: string;
  hopLimit?: number;
}

export type MessageTarget = EndpointTarget | BroadcastTarget;

export interface EntitySubject {
  kind: string;
  identifiers: Record<string, string>;
}

export interface DeckrMessage {
  messageId: string;
  protocolVersion: "1";
  schemaVersion: string;
  lane: string;
  messageType: string;
  sender: string;
  senderSessionId: string;
  recipient: MessageTarget;
  recipientSessionId?: string;
  subject: EntitySubject;
  createdAt: string;
  expiresAt?: string;
  ttlMs?: number;
  inReplyTo?: string;
  causationId?: string;
  trace?: JsonObject;
  body: JsonObject;
}

export function endpointAddress(family: string, endpointId: string): string {
  assertEndpointFamily(family);
  if (endpointId.length === 0) {
    throw new Error("endpoint id must not be empty");
  }
  if (endpointId.trim() !== endpointId || endpointId.includes(":")) {
    throw new Error("endpoint id must not contain ':' or surrounding whitespace");
  }
  if (family === "action_provider") {
    if (RESERVED_ACTION_PROVIDER_INSTANCE_IDS.has(endpointId)) {
      throw new Error(`action provider endpoint id ${endpointId} is reserved`);
    }
    if (!PROVIDER_INSTANCE_ID_RE.test(endpointId)) {
      throw new Error("action provider endpoint id is not valid");
    }
  }
  return `${family}:${endpointId}`;
}

export function parseEndpointAddress(address: string): ParsedEndpointAddress | null {
  const parts = address.split(":");
  if (parts.length !== 2 || parts[0] === "" || parts[1] === "") {
    return null;
  }
  if (!ENDPOINT_FAMILIES.has(parts[0])) {
    return null;
  }
  try {
    endpointAddress(parts[0], parts[1]);
  } catch {
    return null;
  }
  return { family: parts[0] as EndpointFamily, endpointId: parts[1] };
}

export function controllerAddress(controllerId: string): string {
  return endpointAddress("controller", controllerId);
}

export function actionProviderAddress(providerInstanceId: string): string {
  return endpointAddress("action_provider", providerInstanceId);
}

export function hardwareManagerAddress(managerId: string): string {
  return endpointAddress("hardware_manager", managerId);
}

export function serviceAddress(serviceId: string): string {
  return endpointAddress("service", serviceId);
}

export function parseControllerAddress(address: string): string | null {
  const parsed = parseEndpointAddress(address);
  return parsed?.family === "controller" ? parsed.endpointId : null;
}

export function parseActionProviderAddress(address: string): string | null {
  const parsed = parseEndpointAddress(address);
  return parsed?.family === "action_provider" ? parsed.endpointId : null;
}

export function endpointTarget(endpoint: string): EndpointTarget {
  if (parseEndpointAddress(endpoint) === null) {
    throw new Error(`invalid endpoint address: ${endpoint}`);
  }
  return { targetType: "endpoint", endpoint };
}

export function controllersBroadcast(): BroadcastTarget {
  return {
    targetType: "broadcast",
    scope: "controllers",
    endpointFamily: "controller",
  };
}

export function actionProvidersBroadcast(): BroadcastTarget {
  return {
    targetType: "broadcast",
    scope: "action_providers",
    endpointFamily: "action_provider",
  };
}

export function contextSubject(
  contextId: string,
  identifiers: Partial<Record<string, string>> = {},
): EntitySubject {
  return {
    kind: "context",
    identifiers: omitUndefined({ contextId, ...identifiers }),
  };
}

export function actionProviderInstanceSubject(input: {
  providerInstanceId: string;
  providerId: string;
}): EntitySubject {
  return {
    kind: "action_provider_instance",
    identifiers: {
      providerInstanceId: input.providerInstanceId,
      providerId: input.providerId,
    },
  };
}

export function hardwareSubjectForCapability(input: {
  deviceId: string;
  managerId: string;
  controlId: string;
  capabilityId: string;
}): EntitySubject {
  return {
    kind: "hardware_capability",
    identifiers: {
      capabilityId: input.capabilityId,
      controlId: input.controlId,
      deviceId: input.deviceId,
      managerId: input.managerId,
    },
  };
}

export function hardwareSubjectForDevice(input: {
  deviceId: string;
  managerId: string;
}): EntitySubject {
  return {
    kind: "hardware_device",
    identifiers: {
      deviceId: input.deviceId,
      managerId: input.managerId,
    },
  };
}

export function subjectDeviceId(subject: EntitySubject): string | undefined {
  return subject.identifiers.deviceId;
}

export function messageTargetsEndpoint(
  message: DeckrMessage,
  endpoint: string,
): boolean {
  const parsed = parseEndpointAddress(endpoint);
  if (parsed === null) {
    return false;
  }
  if (message.recipient.targetType === "endpoint") {
    return message.recipient.endpoint === endpoint;
  }
  return message.recipient.endpointFamily === parsed.family;
}

export function messageExpiresAt(message: DeckrMessage): Date | null {
  const expiries: number[] = [];
  if (message.expiresAt !== undefined) {
    const expiresAt = Date.parse(message.expiresAt);
    if (Number.isFinite(expiresAt)) {
      expiries.push(expiresAt);
    }
  }
  if (message.ttlMs !== undefined) {
    const createdAt = Date.parse(message.createdAt);
    if (Number.isFinite(createdAt)) {
      expiries.push(createdAt + message.ttlMs);
    }
  }
  if (expiries.length === 0) {
    return null;
  }
  return new Date(Math.min(...expiries));
}

export function messageIsExpiredAt(message: DeckrMessage, now: Date): boolean {
  const expiresAt = messageExpiresAt(message);
  return expiresAt !== null && expiresAt.getTime() <= now.getTime();
}

export function messageIsExpired(
  message: DeckrMessage,
  now: Date = new Date(),
): boolean {
  return messageIsExpiredAt(message, now);
}

export function messageIsDeliverableAt(
  message: DeckrMessage,
  endpoint: string,
  endpointSessionId: string,
  now: Date,
): boolean {
  if (messageIsExpiredAt(message, now)) {
    return false;
  }
  if (!validateLaneMessage(message).ok) {
    return false;
  }
  if (
    message.recipientSessionId !== undefined &&
    message.recipientSessionId !== endpointSessionId
  ) {
    return false;
  }
  return messageTargetsEndpoint(message, endpoint);
}

export function isDeliverableToEndpoint(
  message: DeckrMessage,
  endpoint: string,
  now: Date = new Date(),
): boolean {
  return (
    !messageIsExpiredAt(message, now) &&
    validateLaneMessage(message).ok &&
    messageTargetsEndpoint(message, endpoint)
  );
}

export interface LaneValidationResult {
  ok: boolean;
  reason?: string;
}

export function validateLaneMessage(message: DeckrMessage): LaneValidationResult {
  const contract = LANE_CONTRACTS[message.lane];
  if (contract === undefined) {
    return { ok: false, reason: `message lane ${message.lane} is not a core Deckr lane` };
  }
  if (!contract.messageTypes.has(message.messageType)) {
    return {
      ok: false,
      reason: `message type ${message.messageType} is not supported on lane ${message.lane}`,
    };
  }
  const sender = parseEndpointAddress(message.sender);
  if (sender === null) {
    return { ok: false, reason: `invalid sender endpoint ${message.sender}` };
  }
  if (!contract.allowedSenders.has(sender.family)) {
    return {
      ok: false,
      reason: `sender family ${sender.family} is not allowed on lane ${message.lane}`,
    };
  }
  if (message.recipient.targetType === "endpoint") {
    const recipient = parseEndpointAddress(message.recipient.endpoint);
    if (recipient === null) {
      return {
        ok: false,
        reason: `invalid recipient endpoint ${message.recipient.endpoint}`,
      };
    }
    if (!contract.allowedRecipients.has(recipient.family)) {
      return {
        ok: false,
        reason: `recipient family ${recipient.family} is not allowed on lane ${message.lane}`,
      };
    }
    return { ok: true };
  }
  const key = `${message.recipient.scope}:${message.recipient.endpointFamily}`;
  if (!contract.allowedBroadcasts.has(key)) {
    return {
      ok: false,
      reason: `broadcast target ${key} is not allowed on lane ${message.lane}`,
    };
  }
  return { ok: true };
}

export function buildMessage(input: {
  sender: string;
  senderSessionId: string;
  recipient: string | MessageTarget;
  recipientSessionId?: string;
  messageType: string;
  body: JsonObject;
  subject: EntitySubject;
  messageId?: string;
  lane?: CoreLane;
  createdAt?: string;
  inReplyTo?: string;
  causationId?: string;
  trace?: JsonObject;
  expiresAt?: string;
  ttlMs?: number;
}): DeckrMessage {
  return {
    messageId: input.messageId ?? randomUUID(),
    protocolVersion: "1",
    schemaVersion: "1",
    lane: input.lane ?? ACTIONS_LANE,
    messageType: input.messageType,
    sender: input.sender,
    senderSessionId: input.senderSessionId,
    recipient:
      typeof input.recipient === "string"
        ? endpointTarget(input.recipient)
        : input.recipient,
    ...(input.recipientSessionId !== undefined
      ? { recipientSessionId: input.recipientSessionId }
      : {}),
    subject: input.subject,
    createdAt: input.createdAt ?? new Date().toISOString(),
    ...(input.expiresAt !== undefined ? { expiresAt: input.expiresAt } : {}),
    ...(input.ttlMs !== undefined ? { ttlMs: input.ttlMs } : {}),
    ...(input.inReplyTo !== undefined ? { inReplyTo: input.inReplyTo } : {}),
    ...(input.causationId !== undefined ? { causationId: input.causationId } : {}),
    ...(input.trace !== undefined ? { trace: input.trace } : {}),
    body: input.body,
  };
}

interface LaneContract {
  allowedSenders: Set<string>;
  allowedRecipients: Set<string>;
  allowedBroadcasts: Set<string>;
  messageTypes: Set<string>;
}

const LANE_CONTRACTS: Record<string, LaneContract> = {
  [ACTIONS_LANE]: {
    allowedSenders: new Set(["action_provider", "controller"]),
    allowedRecipients: new Set(["action_provider", "controller"]),
    allowedBroadcasts: new Set(["action_providers:action_provider"]),
    messageTypes: new Set([
      "actionInstanceCreated",
      "actionInstanceDestroyed",
      "bindingAttached",
      "bindingDetached",
      "pageSessionOpened",
      "pageSessionClosed",
      "capabilityInput",
      "bindingOutput",
      "bindingOverlay",
      "bindingOverlayClear",
      "settingsRequest",
      "settingsPatch",
      "settingsReplace",
      "settingsSnapshot",
      "openPage",
      "replacePage",
      "closePage",
      "actionExtension",
    ]),
  },
  [HARDWARE_MESSAGES_LANE]: {
    allowedSenders: new Set(["controller", "hardware_manager"]),
    allowedRecipients: new Set(["controller", "hardware_manager"]),
    allowedBroadcasts: new Set(["controllers:controller"]),
    messageTypes: new Set([
      "deviceAvailable",
      "deviceDescriptorChanged",
      "deviceUnavailable",
      "controlInput",
      "controlCommand",
      "capabilityStateChanged",
      "capabilityStateRequest",
      "capabilityStateReply",
      "commandAccepted",
      "commandRejected",
      "commandReply",
    ]),
  },
  [SERVICES_LANE]: {
    allowedSenders: new Set(["action_provider", "controller", "service"]),
    allowedRecipients: new Set(["action_provider", "controller", "service"]),
    allowedBroadcasts: new Set([]),
    messageTypes: new Set(["serviceCommand", "serviceCommandReply"]),
  },
};

function assertEndpointFamily(family: string): asserts family is EndpointFamily {
  if (!ENDPOINT_FAMILIES.has(family)) {
    throw new Error(`unknown endpoint family ${family}`);
  }
}

function omitUndefined(values: Record<string, string | undefined>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).filter((entry): entry is [string, string] => entry[1] !== undefined),
  );
}
