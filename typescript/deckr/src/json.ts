import { createHash } from "node:crypto";

import { ValidationError } from "./errors.ts";

export type JsonPrimitive = boolean | null | number | string;
export type JsonValue = JsonObject | JsonPrimitive | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function requireJsonObject(value: unknown, fieldName: string): JsonObject {
  if (!isJsonObject(value)) {
    throw new ValidationError(`${fieldName} must be an object`);
  }
  return freezeJson(value) as JsonObject;
}

export function cloneJson<T extends JsonValue | undefined>(value: T): T {
  if (value === undefined) {
    return value;
  }
  return JSON.parse(JSON.stringify(value)) as T;
}

export function freezeJson<T extends JsonValue>(value: T): T {
  if (Array.isArray(value)) {
    return Object.freeze(value.map((item) => freezeJson(item))) as T;
  }
  if (isJsonObject(value)) {
    const out: JsonObject = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = freezeJson(item);
    }
    return Object.freeze(out) as T;
  }
  return value;
}

export function thawJson<T extends JsonValue | undefined>(value: T): T {
  return cloneJson(value);
}

export function canonicalJson(value: JsonValue): string {
  return canonicalJsonValue(value);
}

export function canonicalJsonBytes(value: JsonValue): Uint8Array {
  return new TextEncoder().encode(canonicalJson(value));
}

export function canonicalJsonHash(value: JsonValue): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value), "utf8").digest("hex")}`;
}

function canonicalJsonValue(value: JsonValue): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ValidationError("JSON numbers must be finite");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJsonValue(item)).join(",")}]`;
  }
  const entries = Object.entries(value).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  return `{${entries
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJsonValue(item)}`)
    .join(",")}}`;
}

export function requireText(value: unknown, fieldName: string): string {
  if (typeof value !== "string") {
    throw new ValidationError(`${fieldName} must be a string`);
  }
  if (value.trim() !== value) {
    throw new ValidationError(`${fieldName} must not contain leading or trailing whitespace`);
  }
  if (value.length === 0) {
    throw new ValidationError(`${fieldName} must not be empty`);
  }
  return value;
}

export function requirePositiveInteger(value: unknown, fieldName: string): number {
  if (!Number.isInteger(value) || Number(value) <= 0) {
    throw new ValidationError(`${fieldName} must be a positive integer`);
  }
  return Number(value);
}

export function optionalText(value: unknown, fieldName: string): string | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  return requireText(value, fieldName);
}

export function utcIsoNow(): string {
  return new Date().toISOString();
}

export function parseUtcMillis(value: string | undefined): number {
  if (value === undefined) {
    return Number.NEGATIVE_INFINITY;
  }
  const millis = Date.parse(value);
  return Number.isFinite(millis) ? millis : Number.NEGATIVE_INFINITY;
}
