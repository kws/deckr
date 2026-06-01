import { randomUUID } from "node:crypto";

import {
  endpointAddress,
  endpointTarget,
  parseEndpointAddress,
  type BroadcastTarget,
  type EndpointTarget,
  type MessageTarget,
} from "./endpoint.ts";
import { ValidationError } from "./errors.ts";
import { cloneJson, requireJsonObject, requireText, type JsonObject } from "./json.ts";
import { encodeKeyToken } from "./keys.ts";

const LANE_PREFIX = "deckr.lane";

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

export interface BuildMessageInput {
  lane?: string;
  messageType: string;
  sender: string;
  senderSessionId: string;
  recipient: string | MessageTarget;
  recipientSessionId?: string;
  subject: EntitySubject;
  body: JsonObject;
  messageId?: string;
  schemaVersion?: string;
  createdAt?: string;
  ttlMs?: number;
  expiresAt?: string;
  inReplyTo?: string;
  causationId?: string;
  trace?: JsonObject;
}

export function entitySubject(kind: string, identifiers: Record<string, string> = {}): EntitySubject {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(identifiers)) {
    if (value !== undefined && value !== "") {
      out[key] = String(value);
    }
  }
  return { kind: requireText(kind, "subject kind"), identifiers: out };
}

export function buildMessage(input: BuildMessageInput): DeckrMessage {
  const createdAt = input.createdAt ?? new Date().toISOString();
  const expiresAt =
    input.expiresAt ??
    (input.ttlMs === undefined
      ? undefined
      : new Date(Date.parse(createdAt) + input.ttlMs).toISOString());
  return validateDeckrMessage({
    messageId: input.messageId ?? randomUUID(),
    protocolVersion: "1",
    schemaVersion: input.schemaVersion ?? "1",
    lane: input.lane ?? "actions",
    messageType: requireText(input.messageType, "message type"),
    sender: endpointAddress(input.sender),
    senderSessionId: requireText(input.senderSessionId, "sender session id"),
    recipient:
      typeof input.recipient === "string" ? endpointTarget(input.recipient) : input.recipient,
    ...(input.recipientSessionId === undefined
      ? {}
      : { recipientSessionId: requireText(input.recipientSessionId, "recipient session id") }),
    subject: input.subject,
    createdAt,
    ...(expiresAt === undefined ? {} : { expiresAt }),
    ...(input.ttlMs === undefined ? {} : { ttlMs: input.ttlMs }),
    ...(input.inReplyTo === undefined ? {} : { inReplyTo: input.inReplyTo }),
    ...(input.causationId === undefined ? {} : { causationId: input.causationId }),
    ...(input.trace === undefined ? {} : { trace: cloneJson(input.trace) }),
    body: cloneJson(input.body),
  });
}

export function validateDeckrMessage(value: unknown): DeckrMessage {
  const raw = requireJsonObject(value, "Deckr message");
  const recipient = validateRecipient(raw.recipient);
  const subject = validateSubject(raw.subject);
  const body = requireJsonObject(raw.body, "Deckr message body");
  const message: DeckrMessage = {
    messageId: requireText(raw.messageId, "messageId"),
    protocolVersion: raw.protocolVersion === "1" ? "1" : fail("protocolVersion must be 1"),
    schemaVersion: requireText(raw.schemaVersion, "schemaVersion"),
    lane: requireText(raw.lane, "lane"),
    messageType: requireText(raw.messageType, "messageType"),
    sender: endpointAddress(requireText(raw.sender, "sender")),
    senderSessionId: requireText(raw.senderSessionId, "senderSessionId"),
    recipient,
    subject,
    createdAt: requireText(raw.createdAt, "createdAt"),
    body,
  };
  if (raw.recipientSessionId !== undefined) {
    message.recipientSessionId = requireText(raw.recipientSessionId, "recipientSessionId");
  }
  if (raw.expiresAt !== undefined) {
    message.expiresAt = requireText(raw.expiresAt, "expiresAt");
  }
  if (raw.ttlMs !== undefined) {
    if (typeof raw.ttlMs !== "number" || !Number.isFinite(raw.ttlMs) || raw.ttlMs < 0) {
      throw new ValidationError("ttlMs must be a non-negative number");
    }
    message.ttlMs = raw.ttlMs;
  }
  if (raw.inReplyTo !== undefined) {
    message.inReplyTo = requireText(raw.inReplyTo, "inReplyTo");
  }
  if (raw.causationId !== undefined) {
    message.causationId = requireText(raw.causationId, "causationId");
  }
  if (raw.trace !== undefined) {
    message.trace = requireJsonObject(raw.trace, "trace");
  }
  return message;
}

export function subjectFor(message: DeckrMessage): string {
  const sender = parseEndpointAddress(message.sender);
  return [
    LANE_PREFIX,
    encodeKeyToken(message.lane),
    encodeKeyToken(sender.family),
    encodeKeyToken(sender.endpointId),
  ].join(".");
}

export function recipientHeader(message: DeckrMessage): string {
  if (message.recipient.targetType === "endpoint") {
    return message.recipient.endpoint;
  }
  return ["broadcast", message.recipient.scope, message.recipient.endpointFamily].join(":");
}

export function headersFor(message: DeckrMessage): Record<string, string> {
  return {
    "Deckr-Message-Id": message.messageId,
    "Deckr-Message-Type": message.messageType,
    "Deckr-Sender": message.sender,
    "Deckr-Sender-Session": message.senderSessionId,
    "Deckr-Recipient": recipientHeader(message),
    ...(message.recipientSessionId === undefined
      ? {}
      : { "Deckr-Recipient-Session": message.recipientSessionId }),
    ...(message.inReplyTo === undefined ? {} : { "Deckr-In-Reply-To": message.inReplyTo }),
  };
}

export interface HeaderReader {
  get(name: string): string | string[] | undefined | null;
}

export function validateHeaderHints(
  headers: HeaderReader | Record<string, string> | undefined | null,
  message: DeckrMessage,
): void {
  if (headers === undefined || headers === null) {
    return;
  }
  for (const [name, expected] of Object.entries(headersFor(message))) {
    let actual: string | string[] | undefined | null;
    if (typeof (headers as HeaderReader).get === "function") {
      actual = (headers as HeaderReader).get(name);
    } else {
      actual = (headers as Record<string, string>)[name];
    }
    const actualText = Array.isArray(actual) ? actual[0] : actual;
    if (actualText !== undefined && actualText !== null && actualText !== expected) {
      throw new ValidationError(`NATS header ${name} disagrees with Deckr envelope`);
    }
  }
}

export function validateSubjectHint(subject: string, message: DeckrMessage): void {
  if (!subject.startsWith(`${LANE_PREFIX}.`)) {
    return;
  }
  const expected = subjectFor(message).split(".");
  if (subject.split(".").slice(0, expected.length).join(".") !== expected.join(".")) {
    throw new ValidationError("NATS subject disagrees with Deckr envelope sender");
  }
}

export function messageIsExpired(message: DeckrMessage, now: Date = new Date()): boolean {
  if (message.expiresAt === undefined) {
    return false;
  }
  return Date.parse(message.expiresAt) <= now.getTime();
}

export function messageTargetsEndpoint(message: DeckrMessage, endpoint: string): boolean {
  const parsed = parseEndpointAddress(endpoint);
  if (message.recipient.targetType === "endpoint") {
    return message.recipient.endpoint === endpointAddress(parsed);
  }
  return message.recipient.endpointFamily === parsed.family;
}

export function messageIsDeliverableTo(
  message: DeckrMessage,
  endpoint: string,
  options: { endpointSessionId?: string; now?: Date } = {},
): boolean {
  if (messageIsExpired(message, options.now)) {
    return false;
  }
  if (!messageTargetsEndpoint(message, endpoint)) {
    return false;
  }
  if (
    message.recipientSessionId !== undefined &&
    options.endpointSessionId !== undefined &&
    message.recipientSessionId !== options.endpointSessionId
  ) {
    return false;
  }
  return true;
}

function validateRecipient(value: unknown): MessageTarget {
  const raw = requireJsonObject(value, "recipient");
  if (raw.targetType === "endpoint") {
    return { targetType: "endpoint", endpoint: endpointAddress(requireText(raw.endpoint, "recipient endpoint")) };
  }
  if (raw.targetType === "broadcast") {
    const target: BroadcastTarget = {
      targetType: "broadcast",
      scope: requireText(raw.scope, "broadcast scope"),
      endpointFamily: requireText(raw.endpointFamily, "broadcast endpointFamily"),
    };
    if (raw.domain !== undefined) {
      target.domain = requireText(raw.domain, "broadcast domain");
    }
    if (raw.hopLimit !== undefined) {
      if (!Number.isInteger(raw.hopLimit) || Number(raw.hopLimit) < 0) {
        throw new ValidationError("broadcast hopLimit must be a non-negative integer");
      }
      target.hopLimit = Number(raw.hopLimit);
    }
    return target;
  }
  throw new ValidationError("recipient targetType must be endpoint or broadcast");
}

function validateSubject(value: unknown): EntitySubject {
  const raw = requireJsonObject(value, "subject");
  const identifiers = requireJsonObject(raw.identifiers ?? {}, "subject identifiers");
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(identifiers)) {
    out[requireText(key, "subject identifier key")] = requireText(item, "subject identifier value");
  }
  return { kind: requireText(raw.kind, "subject kind"), identifiers: out };
}

function fail(message: string): never {
  throw new ValidationError(message);
}

export type { BroadcastTarget, EndpointTarget, MessageTarget };
