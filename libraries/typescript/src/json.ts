export type JsonPrimitive = boolean | null | number | string;
export type JsonValue = JsonObject | JsonPrimitive | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

export function asJsonObject(value: unknown, label: string): JsonObject {
  return requireRecord(value, label) as JsonObject;
}

export function compactJsonBytes(value: unknown): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(value));
}

export function compactJsonString(value: unknown): string {
  return JSON.stringify(value);
}

export function contractJsonString(value: unknown): string {
  return stringifyContractValue(value);
}

function stringifyContractValue(
  value: unknown,
  key: string | undefined = undefined,
  parent: Record<string, unknown> | undefined = undefined,
): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (
      Number.isInteger(value) &&
      parent !== undefined &&
      parent.unit === "grid" &&
      key !== undefined &&
      ["x", "y", "width", "height"].includes(key)
    ) {
      return `${value}.0`;
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stringifyContractValue(item)).join(",")}]`;
  }
  if (isRecord(value)) {
    return `{${Object.entries(value)
      .filter((entry) => entry[1] !== undefined)
      .map(
        ([entryKey, entryValue]) =>
          `${JSON.stringify(entryKey)}:${stringifyContractValue(
            entryValue,
            entryKey,
            value,
          )}`,
      )
      .join(",")}}`;
  }
  return JSON.stringify(value);
}
