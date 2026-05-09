import { compactJsonBytes, compactJsonString, contractJsonString } from "./json.js";
import {
  parseEndpointAddress,
  type DeckrMessage,
  type MessageTarget,
} from "./identity.js";
import { encodeKeyToken } from "./keys.js";

export const LANE_SUBJECT_PREFIX = "deckr.lane";
export const LANE_SUBJECT_TEMPLATE =
  "deckr.lane.{lane}.{senderFamily}.{senderEndpointToken}";
export const LANE_SUBSCRIBE_TEMPLATE = "deckr.lane.{lane}.>";
export const NATS_BINDING_SCHEMA_ID = "dev.deckr.binding.nats.v1";
export const NATS_BINDING_PATH = "bindings/nats.v1.json";
export const DECKR_NATS_HEADERS = [
  "Deckr-Message-Id",
  "Deckr-Message-Type",
  "Deckr-Sender",
  "Deckr-Sender-Session",
  "Deckr-Recipient",
  "Deckr-Recipient-Session",
  "Deckr-In-Reply-To",
] as const;
export const REQUIRED_DECKR_NATS_HEADERS = [
  "Deckr-Message-Id",
  "Deckr-Message-Type",
  "Deckr-Sender",
  "Deckr-Sender-Session",
  "Deckr-Recipient",
] as const;

export interface HeaderReader {
  get(name: string): string | null | undefined;
}

export function subjectFor(message: DeckrMessage): string {
  const sender = parseEndpointAddress(message.sender);
  if (sender === null) {
    throw new Error(`invalid sender endpoint: ${message.sender}`);
  }
  return [
    LANE_SUBJECT_PREFIX,
    encodeKeyToken(message.lane),
    encodeKeyToken(sender.family),
    encodeKeyToken(sender.endpointId),
  ].join(".");
}

export function subscribeSubjectForLane(lane: string): string {
  return `${LANE_SUBJECT_PREFIX}.${encodeKeyToken(lane)}.>`;
}

export function recipientHeader(message: DeckrMessage): string {
  return recipientHeaderValue(message.recipient);
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
    ...(message.inReplyTo === undefined
      ? {}
      : { "Deckr-In-Reply-To": message.inReplyTo }),
  };
}

export const deckrHeaderRecord = headersFor;
export const natsSubjectFor = subjectFor;

export function payloadJsonBytes(message: DeckrMessage): Uint8Array {
  return new TextEncoder().encode(payloadJsonString(message));
}

export function payloadJsonString(message: DeckrMessage): string {
  return contractJsonString(orderedMessage(message));
}

export function statePayloadJsonBytes(value: Record<string, unknown>): Uint8Array {
  return compactJsonBytes(value);
}

export function statePayloadJsonString(value: Record<string, unknown>): string {
  return compactJsonString(value);
}

export function validateHeaders(
  headers: HeaderReader | undefined,
  message: DeckrMessage,
): void {
  if (headers === undefined) {
    return;
  }
  for (const [name, expected] of Object.entries(headersFor(message))) {
    const actual = headers.get(name);
    if (actual !== undefined && actual !== null && actual !== expected) {
      throw new Error(`NATS header ${name} disagrees with Deckr envelope`);
    }
  }
}

export const validateHeaderHints = validateHeaders;

export function validateSubjectHint(subject: string, message: DeckrMessage): void {
  if (!subject.startsWith(`${LANE_SUBJECT_PREFIX}.`)) {
    return;
  }
  const actual = subject.split(".");
  const expected = subjectFor(message).split(".");
  if (actual.slice(0, expected.length).join(".") !== expected.join(".")) {
    throw new Error("NATS subject disagrees with Deckr envelope sender");
  }
}

function recipientHeaderValue(recipient: MessageTarget): string {
  if (recipient.targetType === "endpoint") {
    return recipient.endpoint;
  }
  return `broadcast:${recipient.scope}:${recipient.endpointFamily}`;
}

function orderedMessage(message: DeckrMessage): DeckrMessage {
  return {
    messageId: message.messageId,
    protocolVersion: message.protocolVersion,
    schemaVersion: message.schemaVersion,
    lane: message.lane,
    messageType: message.messageType,
    sender: message.sender,
    senderSessionId: message.senderSessionId,
    recipient: orderedRecipient(message.recipient),
    ...(message.recipientSessionId === undefined
      ? {}
      : { recipientSessionId: message.recipientSessionId }),
    subject: {
      kind: message.subject.kind,
      identifiers: message.subject.identifiers,
    },
    createdAt: message.createdAt,
    ...(message.expiresAt === undefined ? {} : { expiresAt: message.expiresAt }),
    ...(message.ttlMs === undefined ? {} : { ttlMs: message.ttlMs }),
    ...(message.inReplyTo === undefined ? {} : { inReplyTo: message.inReplyTo }),
    ...(message.causationId === undefined ? {} : { causationId: message.causationId }),
    ...(message.trace === undefined ? {} : { trace: message.trace }),
    body: message.body,
  };
}

function orderedRecipient(recipient: MessageTarget): MessageTarget {
  if (recipient.targetType === "endpoint") {
    return {
      targetType: "endpoint",
      endpoint: recipient.endpoint,
    };
  }
  return {
    targetType: "broadcast",
    scope: recipient.scope,
    endpointFamily: recipient.endpointFamily,
    ...(recipient.domain === undefined ? {} : { domain: recipient.domain }),
    ...(recipient.hopLimit === undefined ? {} : { hopLimit: recipient.hopLimit }),
  };
}
