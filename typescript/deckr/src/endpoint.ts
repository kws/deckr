import { ValidationError } from "./errors.ts";
import { requireText } from "./json.ts";

export interface EndpointAddress {
  family: string;
  endpointId: string;
}

export interface EndpointTarget {
  targetType: "endpoint";
  endpoint: string;
}

export interface BroadcastTarget {
  targetType: "broadcast";
  scope: string;
  endpointFamily: string;
  domain?: string;
  hopLimit?: number;
}

export type MessageTarget = EndpointTarget | BroadcastTarget;

export function parseEndpointAddress(value: string | EndpointAddress): EndpointAddress {
  if (typeof value !== "string") {
    return {
      family: requireText(value.family, "endpoint family"),
      endpointId: requireText(value.endpointId, "endpoint id"),
    };
  }
  if (value.trim() !== value) {
    throw new ValidationError("endpoint address must not contain leading or trailing whitespace");
  }
  const parts = value.split(":");
  if (parts.length !== 2 || parts[0] === "" || parts[1] === "") {
    throw new ValidationError(`invalid endpoint address: ${JSON.stringify(value)}`);
  }
  return { family: parts[0]!, endpointId: parts[1]! };
}

export function endpointAddress(value: string | EndpointAddress): string {
  const parsed = parseEndpointAddress(value);
  return `${parsed.family}:${parsed.endpointId}`;
}

export function controllerAddress(controllerId: string): string {
  return `controller:${requireText(controllerId, "controller id")}`;
}

export function hardwareManagerAddress(managerId: string): string {
  return `hardware_manager:${requireText(managerId, "hardware manager id")}`;
}

export function actionProviderAddress(providerInstanceId: string): string {
  return `action_provider:${requireText(providerInstanceId, "provider instance id")}`;
}

export function parseControllerAddress(address: string): string | null {
  try {
    const parsed = parseEndpointAddress(address);
    return parsed.family === "controller" ? parsed.endpointId : null;
  } catch {
    return null;
  }
}

export function parseActionProviderAddress(address: string): string | null {
  try {
    const parsed = parseEndpointAddress(address);
    return parsed.family === "action_provider" ? parsed.endpointId : null;
  } catch {
    return null;
  }
}

export function serviceAddress(serviceId: string): string {
  return `service:${requireText(serviceId, "service id")}`;
}

export function endpointTarget(endpoint: string | EndpointAddress): EndpointTarget {
  return { targetType: "endpoint", endpoint: endpointAddress(endpoint) };
}

export function endpointEquals(left: string | EndpointAddress, right: string | EndpointAddress): boolean {
  return endpointAddress(left) === endpointAddress(right);
}
