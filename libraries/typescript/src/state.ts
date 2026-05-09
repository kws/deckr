import {
  actionProviderAddress,
  endpointAddress,
  parseEndpointAddress,
  serviceAddress,
  type DeckrMessage,
} from "./identity.js";
import { decodeKeyToken, encodeKeyToken } from "./keys.js";
import type { JsonObject, JsonValue } from "./json.js";

export type { JsonObject, JsonValue } from "./json.js";

export const DEFAULT_LEASE_STATE_BUCKET = "deckr_lease_v1";
export const DEFAULT_DISCOVERY_STATE_BUCKET = "deckr_discovery_v1";
export const STATE_TTL_SECONDS = 30;
export const STATE_RENEWAL_INTERVAL_SECONDS = 5;

export interface EndpointPresence {
  endpoint: string;
  lane: string;
  sessionId: string;
  timestamp: string;
  ttlSeconds: number;
  metadata?: Record<string, string>;
}

export interface ActionProviderCatalog {
  providerInstanceId: string;
  providerEndpoint: string;
  providerId: string;
  sessionId: string;
  timestamp: string;
  labels?: Record<string, string>;
  annotations?: JsonObject;
  actions: Record<string, ActionDescriptor>;
}

export interface HardwareInventoryDevice {
  deviceRef: DeviceRef;
  descriptor: DeviceDescriptor;
}

export interface HardwareInventory {
  managerId: string;
  managerEndpoint: string;
  sessionId: string;
  timestamp: string;
  devices: Record<string, HardwareInventoryDevice>;
}

export interface DeviceClaim {
  claimedByEndpoint: string;
  claimedBySessionId: string;
  timestamp: string;
  ttlSeconds: number;
}

export interface ServiceCatalog {
  serviceId: string;
  serviceEndpoint: string;
  serviceNamespace: string;
  sessionId: string;
  timestamp: string;
  supportedOperations: string[];
  viewPrefixes?: string[];
  labels?: Record<string, string>;
  annotations?: JsonObject;
  diagnostics?: JsonObject;
}

export interface ServiceStatus {
  serviceId: string;
  serviceEndpoint: string;
  serviceNamespace: string;
  sessionId: string;
  timestamp: string;
  status: string;
  diagnostics?: JsonObject;
}

export interface RegisteredAction {
  actionId: string;
  name?: string;
  providerId?: string;
  controllers?: string[];
  requirements?: CapabilityRequirement[];
  dynamicPageTemplates?: DynamicPageTemplateDescriptor[];
  propertyInspectorPath?: string;
  manifestDefaults?: JsonObject;
  settingsSchema?: JsonObject;
  providerSettingsSchema?: JsonObject;
}

export interface ActionDescriptor extends RegisteredAction {}

export interface CapabilityRequirementSelector {
  capabilityId?: string;
  family?: string;
  type?: string;
  direction?: "input" | "output" | "state" | "command";
  eventTypes?: string[];
  commandTypes?: string[];
}

export interface CapabilityRequirement {
  name: string;
  required?: boolean;
  preferences: CapabilityRequirementSelector[];
  eventTypes?: string[];
  commandTypes?: string[];
  views?: Array<"raw" | "native" | "projected" | "derived" | "extension">;
}

export interface DynamicPageRoleDescriptor {
  roleId: string;
  cardinality?: "single" | "collection";
  optional?: boolean;
  min?: number;
  preferred?: number;
  max?: number;
  requirements: CapabilityRequirement[];
  layout?: JsonObject;
}

export interface DynamicPageTemplateDescriptor {
  templateId: string;
  roles: DynamicPageRoleDescriptor[];
}

export interface DeviceRef {
  managerId: string;
  deviceId: string;
  fingerprint?: string;
}

export interface ControlRef {
  deviceRef: DeviceRef;
  controlId: string;
}

export interface CapabilityRef {
  deviceRef?: DeviceRef;
  controlId?: string;
  capabilityId: string;
}

export interface MatchedCapability {
  requirementName?: string;
  roleId?: string;
  capability: CapabilityRef;
  family: string;
  type: string;
  direction: "input" | "output" | "state" | "command";
  eventTypes?: string[];
  commandTypes?: string[];
  provenance?: "native" | "projection" | "derivation" | "extension";
  source?: CapabilityRef;
}

export interface BindingMetadata {
  providerInstanceId: string;
  providerId: string;
  actionId: string;
  actionInstanceId: string;
  configId: string;
  contextId: string;
  bindingId: string;
  pageSessionId?: string;
  deviceRef: DeviceRef;
  controlRef: ControlRef;
  roleId?: string;
  itemKey?: string;
  handler?: string;
  matchedCapabilities?: MatchedCapability[];
  outputGeneration?: number;
}

export interface PageSessionMetadata {
  providerInstanceId: string;
  providerId: string;
  actionInstanceId: string;
  configId: string;
  pageId: string;
  pageSessionId: string;
  contextId: string;
  templateId?: string;
  ownerBindingId?: string;
  bindings?: BindingMetadata[];
}

export interface CapabilityInputEvent {
  capability: CapabilityRef;
  eventType: string;
  value?: JsonValue;
  sequence?: number;
  occurredAt: string;
  producer?: string;
  source?: CapabilityRef;
  view?: "raw" | "native" | "projected" | "derived" | "extension";
}

export interface DeviceDescriptor {
  deviceId: string;
  fingerprint: string;
  displayName?: string;
  manufacturer?: string;
  model?: string;
  controls: JsonObject[];
  capabilities: JsonObject[];
  connections: JsonObject[];
  [key: string]: unknown;
}

export type SettingsScope = "action_provider_instance" | "action_instance";

export interface SettingsTargetRef {
  scope: SettingsScope;
  controllerId: string;
  configId: string;
  providerInstanceId: string;
  providerId: string;
  actionId?: string;
  actionInstanceId?: string;
  stableId?: string;
}

export interface SettingsSchemaMetadata {
  schemaId?: string;
  schema?: JsonObject;
  stale?: boolean;
}

export interface SettingsSnapshot {
  target: SettingsTargetRef;
  settings: JsonObject;
  provenance?: string[];
  schemaMetadata?: SettingsSchemaMetadata;
}

export function presenceEndpointKey(input: {
  lane: string;
  endpoint: string;
}): string {
  const parsed = parseEndpointAddress(input.endpoint);
  if (parsed === null) {
    throw new Error(`invalid endpoint address: ${input.endpoint}`);
  }
  return [
    "presence",
    "endpoint",
    encodeKeyToken(input.lane),
    encodeKeyToken(parsed.family),
    encodeKeyToken(parsed.endpointId),
  ].join(".");
}

export function presenceEndpointPrefix(lane: string, endpointFamily: string): string {
  return [
    "presence",
    "endpoint",
    encodeKeyToken(lane),
    encodeKeyToken(endpointFamily),
    "",
  ].join(".");
}

export function controllerPresencePrefix(): string {
  return presenceEndpointPrefix("hardware_messages", "controller");
}

export function parsePresenceEndpointKey(
  key: string,
): { lane: string; endpoint: string } | null {
  const parts = key.split(".");
  if (parts.length !== 5 || parts[0] !== "presence" || parts[1] !== "endpoint") {
    return null;
  }
  const lane = decodeKeyToken(parts[2]);
  const family = decodeKeyToken(parts[3]);
  const endpointId = decodeKeyToken(parts[4]);
  return { lane, endpoint: endpointAddress(family, endpointId) };
}

export function hardwareInventoryKey(managerId: string): string {
  return `inventory.hardware.${encodeKeyToken(managerId)}`;
}

export function parseHardwareInventoryKey(key: string): { managerId: string } | null {
  const parts = key.split(".");
  if (parts.length !== 3 || parts[0] !== "inventory" || parts[1] !== "hardware") {
    return null;
  }
  return { managerId: decodeKeyToken(parts[2]) };
}

export function deviceClaimKey(input: { managerId: string; deviceId: string }): string {
  return [
    "claim",
    "device",
    encodeKeyToken(input.managerId),
    encodeKeyToken(input.deviceId),
  ].join(".");
}

export function deviceClaimPrefix(managerId: string): string {
  return `claim.device.${encodeKeyToken(managerId)}.`;
}

export function parseDeviceClaimKey(
  key: string,
): { managerId: string; deviceId: string } | null {
  const parts = key.split(".");
  if (parts.length !== 4 || parts[0] !== "claim" || parts[1] !== "device") {
    return null;
  }
  return {
    managerId: decodeKeyToken(parts[2]),
    deviceId: decodeKeyToken(parts[3]),
  };
}

export function actionProviderCatalogKey(providerInstanceId: string): string {
  return [
    "catalog",
    "actions",
    "providers",
    encodeKeyToken(providerInstanceId),
  ].join(".");
}

export function parseActionProviderCatalogKey(
  key: string,
): { providerInstanceId: string } | null {
  const parts = key.split(".");
  if (
    parts.length !== 4 ||
    parts[0] !== "catalog" ||
    parts[1] !== "actions" ||
    parts[2] !== "providers"
  ) {
    return null;
  }
  return { providerInstanceId: decodeKeyToken(parts[3]) };
}

export function serviceCatalogKey(serviceId: string): string {
  return `catalog.services.${encodeKeyToken(serviceId)}`;
}

export function parseServiceCatalogKey(key: string): { serviceId: string } | null {
  const parts = key.split(".");
  if (parts.length !== 3 || parts[0] !== "catalog" || parts[1] !== "services") {
    return null;
  }
  return { serviceId: decodeKeyToken(parts[2]) };
}

export function serviceStatusKey(serviceId: string): string {
  return `status.services.${encodeKeyToken(serviceId)}`;
}

export function parseServiceStatusKey(key: string): { serviceId: string } | null {
  const parts = key.split(".");
  if (parts.length !== 3 || parts[0] !== "status" || parts[1] !== "services") {
    return null;
  }
  return { serviceId: decodeKeyToken(parts[2]) };
}

export function serviceViewKey(
  serviceId: string,
  serviceNamespace: string,
  ...tokens: string[]
): string {
  return [
    "view",
    "services",
    encodeKeyToken(serviceId),
    encodeKeyToken(serviceNamespace),
    ...tokens.map(encodeKeyToken),
  ].join(".");
}

export function parseServiceViewKey(
  key: string,
): { serviceId: string; serviceNamespace: string; tokens: string[] } | null {
  const parts = key.split(".");
  if (parts.length < 4 || parts[0] !== "view" || parts[1] !== "services") {
    return null;
  }
  return {
    serviceId: decodeKeyToken(parts[2]),
    serviceNamespace: decodeKeyToken(parts[3]),
    tokens: parts.slice(4).map(decodeKeyToken),
  };
}

export function settingsTargetKey(target: SettingsTargetRef): string {
  const parts = [
    "settings",
    "target",
    encodeKeyToken(target.scope),
    encodeKeyToken(target.controllerId),
    encodeKeyToken(target.configId),
    encodeKeyToken(target.providerInstanceId),
    encodeKeyToken(target.providerId),
  ];
  if (target.scope === "action_instance") {
    if (target.actionId === undefined || target.actionInstanceId === undefined) {
      throw new Error("action instance settings target requires action ids");
    }
    parts.push(
      encodeKeyToken(target.actionId),
      encodeKeyToken(target.actionInstanceId),
      target.stableId === undefined ? "0" : "1",
    );
    if (target.stableId !== undefined) {
      parts.push(encodeKeyToken(target.stableId));
    }
  }
  return parts.join(".");
}

export function parseSettingsTargetKey(key: string): SettingsTargetRef | null {
  const parts = key.split(".");
  if (parts.length < 7 || parts[0] !== "settings" || parts[1] !== "target") {
    return null;
  }
  const scope = decodeKeyToken(parts[2]);
  if (scope !== "action_provider_instance" && scope !== "action_instance") {
    return null;
  }
  const target: SettingsTargetRef = {
    scope,
    controllerId: decodeKeyToken(parts[3]),
    configId: decodeKeyToken(parts[4]),
    providerInstanceId: decodeKeyToken(parts[5]),
    providerId: decodeKeyToken(parts[6]),
  };
  if (scope === "action_provider_instance") {
    return parts.length === 7 ? target : null;
  }
  if (parts.length !== 10 && parts.length !== 11) {
    return null;
  }
  target.actionId = decodeKeyToken(parts[7]);
  target.actionInstanceId = decodeKeyToken(parts[8]);
  const hasStableId = parts[9];
  if (hasStableId === "0") {
    return parts.length === 10 ? target : null;
  }
  if (hasStableId !== "1" || parts.length !== 11) {
    return null;
  }
  target.stableId = decodeKeyToken(parts[10]);
  return target;
}

export function actionDescriptor(action: RegisteredAction): ActionDescriptor {
  return stripUndefined({
    actionId: action.actionId,
    name: action.name,
    providerId: action.providerId,
    requirements: action.requirements?.map((requirement) => ({ ...requirement })),
    dynamicPageTemplates: action.dynamicPageTemplates?.map((template) => ({
      ...template,
    })),
    controllers: action.controllers === undefined ? undefined : [...action.controllers],
    propertyInspectorPath: action.propertyInspectorPath,
    manifestDefaults: action.manifestDefaults,
    settingsSchema: action.settingsSchema,
    providerSettingsSchema: action.providerSettingsSchema,
  }) as unknown as ActionDescriptor;
}

export function actionCatalogMap(
  actions: Iterable<RegisteredAction>,
): Record<string, ActionDescriptor> {
  return Object.fromEntries(
    [...actions]
      .filter((action) => action.actionId.trim().length > 0)
      .sort((left, right) => left.actionId.localeCompare(right.actionId))
      .map((action) => [action.actionId, actionDescriptor(action)]),
  );
}

export function endpointPresence(input: {
  endpoint: string;
  lane: string;
  sessionId: string;
  timestamp: string;
  ttlSeconds: number;
  metadata?: Record<string, string>;
}): EndpointPresence {
  return stripUndefined({
    endpoint: input.endpoint,
    lane: input.lane,
    sessionId: input.sessionId,
    timestamp: input.timestamp,
    ttlSeconds: input.ttlSeconds,
    metadata: input.metadata ?? {},
  }) as unknown as EndpointPresence;
}

export function actionProviderEndpointPresence(input: {
  providerInstanceId: string;
  sessionId: string;
  timestamp: string;
  ttlSeconds: number;
  metadata?: Record<string, string>;
}): EndpointPresence {
  return endpointPresence({
    endpoint: actionProviderAddress(input.providerInstanceId),
    lane: "actions",
    sessionId: input.sessionId,
    timestamp: input.timestamp,
    ttlSeconds: input.ttlSeconds,
    metadata: input.metadata,
  });
}

export function actionProviderCatalog(input: {
  providerInstanceId: string;
  providerId: string;
  sessionId: string;
  timestamp: string;
  labels?: Record<string, string>;
  annotations?: JsonObject;
  actions: Iterable<RegisteredAction>;
}): ActionProviderCatalog {
  const providerEndpoint = actionProviderAddress(input.providerInstanceId);
  return {
    providerInstanceId: input.providerInstanceId,
    providerEndpoint,
    providerId: input.providerId,
    sessionId: input.sessionId,
    timestamp: input.timestamp,
    labels: input.labels ?? {},
    annotations: input.annotations ?? {},
    actions: actionCatalogMap(input.actions),
  };
}

export function serviceCatalog(input: {
  serviceId: string;
  serviceNamespace: string;
  sessionId: string;
  timestamp: string;
  supportedOperations: string[];
  viewPrefixes?: string[];
  labels?: Record<string, string>;
  annotations?: JsonObject;
  diagnostics?: JsonObject;
}): ServiceCatalog {
  return stripUndefined({
    serviceId: input.serviceId,
    serviceEndpoint: serviceAddress(input.serviceId),
    serviceNamespace: input.serviceNamespace,
    sessionId: input.sessionId,
    timestamp: input.timestamp,
    supportedOperations: [...input.supportedOperations],
    viewPrefixes: input.viewPrefixes,
    labels: input.labels ?? {},
    annotations: input.annotations ?? {},
    diagnostics: input.diagnostics ?? {},
  }) as unknown as ServiceCatalog;
}

export function statePayloadFromMessage(message: DeckrMessage): JsonObject {
  return message.body;
}

function stripUndefined(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(value).filter((entry) => entry[1] !== undefined),
  );
}
