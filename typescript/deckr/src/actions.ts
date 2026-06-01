import type { AdvertisementRecord } from "./beacon.ts";
import {
  actionProviderAddress,
  controllerAddress,
  endpointAddress,
  endpointTarget,
  parseEndpointAddress,
  type BroadcastTarget,
  type MessageTarget,
} from "./endpoint.ts";
import { ValidationError } from "./errors.ts";
import {
  cloneJson,
  requireJsonObject,
  requireText,
  type JsonObject,
  type JsonValue,
} from "./json.ts";
import { decodeKeyToken, encodeKeyToken } from "./keys.ts";
import {
  entitySubject,
  messageTargetsEndpoint,
  type DeckrMessage,
  type EntitySubject,
} from "./lanes.ts";

export const ACTIONS_PROFILE_ID = "dev.deckr.profile.actions.v1";
export const ACTION_PROVIDER_SESSION_PROFILE_ID =
  "dev.deckr.profile.action_provider_session.v1";
export const ACTIONS_FEATURE_ID = "dev.deckr.actions";

export const ACTION_INSTANCE_CREATED = "actionInstanceCreated";
export const ACTION_INSTANCE_DESTROYED = "actionInstanceDestroyed";
export const BINDING_ATTACHED = "bindingAttached";
export const BINDING_DETACHED = "bindingDetached";
export const PAGE_SESSION_OPENED = "pageSessionOpened";
export const PAGE_SESSION_CLOSED = "pageSessionClosed";
export const CAPABILITY_INPUT = "capabilityInput";
export const BINDING_OUTPUT = "bindingOutput";
export const BINDING_OVERLAY = "bindingOverlay";
export const BINDING_OVERLAY_CLEAR = "bindingOverlayClear";
export const SETTINGS_REQUEST = "settingsRequest";
export const SETTINGS_PATCH = "settingsPatch";
export const SETTINGS_REPLACE = "settingsReplace";
export const SETTINGS_SNAPSHOT = "settingsSnapshot";
export const OPEN_PAGE = "openPage";
export const UPDATE_PAGE = "updatePage";
export const REPLACE_PAGE = "replacePage";
export const CLOSE_PAGE = "closePage";
export const ACTION_EXTENSION = "actionExtension";

export interface CapabilityRequirementSelector {
  capabilityId?: string | null;
  family?: string | null;
  type?: string | null;
  direction?: "input" | "output" | "state" | "command" | null;
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

export interface ProfileCapacity {
  totalInstances?: number | null;
  claimedInstances?: number;
  availableInstances?: number | null;
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

export interface ActionBeaconDescriptor {
  actionId: string;
  name?: string | null;
  providerId?: string | null;
  controllers?: string[] | null;
  requirements?: CapabilityRequirement[] | null;
  dynamicPageTemplates?: DynamicPageTemplateDescriptor[] | null;
  propertyInspectorPath?: string | null;
  manifestDefaults?: JsonObject | null;
  settingsSchema?: JsonObject | null;
  providerSettingsSchema?: JsonObject | null;
  capacity?: ProfileCapacity | null;
  hints: JsonObject;
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
  capacity?: ProfileCapacity;
  hints?: JsonObject;
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

export interface ActionInstanceMetadata {
  providerInstanceId: string;
  providerId: string;
  actionId: string;
  actionInstanceId: string;
  configId: string;
  contextId: string;
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

export type SettingsProvenance = string;

export interface SettingsSnapshot {
  target: SettingsTargetRef;
  settings: JsonObject;
  provenance?: SettingsProvenance[];
  schemaMetadata?: SettingsSchemaMetadata;
}

export interface SettingsTargetDescription {
  target: SettingsTargetRef;
  providerInstanceId: string;
  providerId: string;
  actionId?: string;
  label?: string;
  placement?: JsonObject;
  schemaMetadata?: SettingsSchemaMetadata;
  provenance?: SettingsProvenance[];
}

export interface ActionsBeaconPayload {
  profile: typeof ACTIONS_PROFILE_ID;
  providerInstanceId: string;
  providerEndpoint: string;
  providerId: string;
  sessionId: string;
  labels: Record<string, string>;
  annotations: JsonObject;
  actions: Record<string, ActionBeaconDescriptor>;
}

export interface ActionProviderSessionTerms {
  profile: typeof ACTION_PROVIDER_SESSION_PROFILE_ID;
  sessionId: string;
  controllerEndpoint: string;
  providerEndpoint: string;
  providerInstanceId: string;
  providerId: string;
}

export function actionBeaconDescriptor(action: RegisteredAction): ActionBeaconDescriptor {
  return validateActionBeaconDescriptor({
    actionId: action.actionId,
    ...(action.name === undefined ? {} : { name: action.name }),
    ...(action.providerId === undefined ? {} : { providerId: action.providerId }),
    ...(action.controllers === undefined ? {} : { controllers: [...action.controllers] }),
    ...(action.requirements === undefined
      ? {}
      : { requirements: cloneJson(action.requirements as unknown as JsonValue) }),
    ...(action.dynamicPageTemplates === undefined
      ? {}
      : { dynamicPageTemplates: cloneJson(action.dynamicPageTemplates as unknown as JsonValue) }),
    ...(action.propertyInspectorPath === undefined
      ? {}
      : { propertyInspectorPath: action.propertyInspectorPath }),
    ...(action.manifestDefaults === undefined
      ? {}
      : { manifestDefaults: cloneJson(action.manifestDefaults) }),
    ...(action.settingsSchema === undefined
      ? {}
      : { settingsSchema: cloneJson(action.settingsSchema) }),
    ...(action.providerSettingsSchema === undefined
      ? {}
      : { providerSettingsSchema: cloneJson(action.providerSettingsSchema) }),
    ...(action.capacity === undefined
      ? {}
      : { capacity: cloneJson(action.capacity as unknown as JsonValue) }),
    hints: action.hints ?? {},
  });
}

export function actionCatalogMap(
  actions: Iterable<RegisteredAction>,
): Record<string, ActionBeaconDescriptor> {
  return Object.fromEntries(
    [...actions]
      .filter((action) => action.actionId.trim().length > 0)
      .sort((left, right) => left.actionId.localeCompare(right.actionId))
      .map((action) => [action.actionId, actionBeaconDescriptor(action)]),
  );
}

export function actionsBeaconPayload(input: {
  providerInstanceId: string;
  providerId: string;
  sessionId: string;
  labels?: Record<string, string>;
  annotations?: JsonObject;
  actions: Iterable<RegisteredAction> | Record<string, ActionBeaconDescriptor>;
}): ActionsBeaconPayload {
  const providerInstanceId = requireText(input.providerInstanceId, "providerInstanceId");
  const actions =
    typeof (input.actions as Iterable<RegisteredAction>)[Symbol.iterator] === "function"
      ? actionCatalogMap(input.actions as Iterable<RegisteredAction>)
      : input.actions;
  return validateActionsBeaconPayload({
    profile: ACTIONS_PROFILE_ID,
    providerInstanceId,
    providerEndpoint: actionProviderAddress(providerInstanceId),
    providerId: input.providerId,
    sessionId: input.sessionId,
    labels: input.labels ?? {},
    annotations: input.annotations ?? {},
    actions,
  });
}

export function validateActionBeaconDescriptor(value: unknown): ActionBeaconDescriptor {
  const raw = requireJsonObject(value, "action Beacon descriptor");
  const descriptor: ActionBeaconDescriptor = {
    actionId: requireText(raw.actionId, "actionId"),
    hints: raw.hints === undefined ? {} : requireJsonObject(raw.hints, "hints"),
  };
  setOptionalText(descriptor, "name", raw.name, "name", true);
  setOptionalText(descriptor, "providerId", raw.providerId, "providerId", true);
  setOptionalText(descriptor, "propertyInspectorPath", raw.propertyInspectorPath, "propertyInspectorPath", true);
  setOptionalTextList(descriptor, "controllers", raw.controllers, "controllers", true);
  setOptionalObject(descriptor, "manifestDefaults", raw.manifestDefaults, "manifestDefaults", true);
  setOptionalObject(descriptor, "settingsSchema", raw.settingsSchema, "settingsSchema", true);
  setOptionalObject(descriptor, "providerSettingsSchema", raw.providerSettingsSchema, "providerSettingsSchema", true);
  if (raw.requirements !== undefined && raw.requirements !== null) {
    if (!Array.isArray(raw.requirements)) {
      throw new ValidationError("requirements must be an array");
    }
    descriptor.requirements = raw.requirements.map(validateCapabilityRequirement);
  } else if (raw.requirements === null) {
    descriptor.requirements = null;
  }
  if (raw.dynamicPageTemplates !== undefined && raw.dynamicPageTemplates !== null) {
    if (!Array.isArray(raw.dynamicPageTemplates)) {
      throw new ValidationError("dynamicPageTemplates must be an array");
    }
    descriptor.dynamicPageTemplates = raw.dynamicPageTemplates.map(validateDynamicPageTemplate);
  } else if (raw.dynamicPageTemplates === null) {
    descriptor.dynamicPageTemplates = null;
  }
  if (raw.capacity !== undefined && raw.capacity !== null) {
    descriptor.capacity = validateProfileCapacity(raw.capacity);
  } else if (raw.capacity === null) {
    descriptor.capacity = null;
  }
  return descriptor;
}

export function validateActionsBeaconPayload(value: unknown): ActionsBeaconPayload {
  const raw = requireJsonObject(value, "actions Beacon payload");
  if (raw.profile !== undefined && raw.profile !== ACTIONS_PROFILE_ID) {
    throw new ValidationError("actions Beacon payload profile is invalid");
  }
  const providerInstanceId = requireText(raw.providerInstanceId, "providerInstanceId");
  const providerEndpoint = endpointAddress(requireText(raw.providerEndpoint, "providerEndpoint"));
  if (providerEndpoint !== actionProviderAddress(providerInstanceId)) {
    throw new ValidationError("providerEndpoint must equal action_provider:<providerInstanceId>");
  }
  const providerId = requireText(raw.providerId, "providerId");
  const sessionId = requireText(raw.sessionId, "sessionId");
  const labels = validateStringRecord(raw.labels ?? {}, "labels");
  const annotations = requireJsonObject(raw.annotations ?? {}, "annotations");
  const rawActions = requireJsonObject(raw.actions ?? {}, "actions");
  const actions: Record<string, ActionBeaconDescriptor> = {};
  for (const [key, item] of Object.entries(rawActions)) {
    const actionId = requireText(key, "action id");
    const descriptor = validateActionBeaconDescriptor(item);
    if (descriptor.actionId !== actionId) {
      throw new ValidationError("action map keys must match descriptor actionId");
    }
    if (descriptor.providerId !== undefined && descriptor.providerId !== null && descriptor.providerId !== providerId) {
      throw new ValidationError("action descriptor providerId must match providerId");
    }
    actions[actionId] = descriptor;
  }
  return {
    profile: ACTIONS_PROFILE_ID,
    providerInstanceId,
    providerEndpoint,
    providerId,
    sessionId,
    labels,
    annotations,
    actions,
  };
}

export function actionsPayloadFromAdvertisement(
  advertisement: AdvertisementRecord,
): ActionsBeaconPayload {
  if (advertisement.featureId !== ACTIONS_FEATURE_ID) {
    throw new ValidationError("advertisement featureId is not dev.deckr.actions");
  }
  if (advertisement.payload === undefined) {
    throw new ValidationError("actions advertisement requires payload");
  }
  const payload = validateActionsBeaconPayload(advertisement.payload);
  if (payload.sessionId !== advertisement.sessionId) {
    throw new ValidationError("actions payload sessionId must match advertisement sessionId");
  }
  if (payload.providerEndpoint !== advertisement.endpoint) {
    throw new ValidationError("actions payload providerEndpoint must match advertisement endpoint");
  }
  return payload;
}

export function actionProviderSessionTerms(input: {
  sessionId: string;
  controllerEndpoint: string;
  providerEndpoint: string;
  providerInstanceId: string;
  providerId: string;
}): ActionProviderSessionTerms {
  return validateActionProviderSessionTerms({
    profile: ACTION_PROVIDER_SESSION_PROFILE_ID,
    ...input,
  });
}

export function validateActionProviderSessionTerms(value: unknown): ActionProviderSessionTerms {
  const raw = requireJsonObject(value, "action provider session terms");
  if (raw.profile !== undefined && raw.profile !== ACTION_PROVIDER_SESSION_PROFILE_ID) {
    throw new ValidationError("action provider session profile is invalid");
  }
  const terms: ActionProviderSessionTerms = {
    profile: ACTION_PROVIDER_SESSION_PROFILE_ID,
    sessionId: requireText(raw.sessionId, "sessionId"),
    controllerEndpoint: endpointAddress(requireText(raw.controllerEndpoint, "controllerEndpoint")),
    providerEndpoint: endpointAddress(requireText(raw.providerEndpoint, "providerEndpoint")),
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
  };
  const controller = parseEndpointAddress(terms.controllerEndpoint);
  const provider = parseEndpointAddress(terms.providerEndpoint);
  if (controller.family !== "controller") {
    throw new ValidationError("controllerEndpoint must use controller:<id>");
  }
  if (provider.family !== "action_provider") {
    throw new ValidationError("providerEndpoint must use action_provider:<id>");
  }
  if (provider.endpointId !== terms.providerInstanceId) {
    throw new ValidationError("providerEndpoint must equal action_provider:<providerInstanceId>");
  }
  return terms;
}

export function actionProviderSessionContractId(
  controllerEndpoint: string,
  providerEndpoint: string,
): string {
  const controller = parseEndpointAddress(controllerEndpoint);
  const provider = parseEndpointAddress(providerEndpoint);
  if (controller.family !== "controller") {
    throw new ValidationError("controllerEndpoint must use controller:<id>");
  }
  if (provider.family !== "action_provider") {
    throw new ValidationError("providerEndpoint must use action_provider:<id>");
  }
  return `action-provider-session:${endpointAddress(controller)}:${endpointAddress(provider)}`;
}

export function actionProvidersBroadcast(): BroadcastTarget {
  return {
    targetType: "broadcast",
    scope: "action_providers",
    endpointFamily: "action_provider",
  };
}

export function controllersBroadcast(): BroadcastTarget {
  return {
    targetType: "broadcast",
    scope: "controllers",
    endpointFamily: "controller",
  };
}

export interface ContextSubjectOptions {
  providerInstanceId?: string;
  providerId?: string;
  configId?: string;
  actionInstanceId?: string;
  bindingId?: string;
  pageSessionId?: string;
}

export function contextSubject(
  contextId: string,
  options: ContextSubjectOptions = {},
): EntitySubject {
  return entitySubject("context", {
    contextId: requireText(contextId, "contextId"),
    ...(options.providerInstanceId === undefined
      ? {}
      : { providerInstanceId: requireText(options.providerInstanceId, "providerInstanceId") }),
    ...(options.providerId === undefined
      ? {}
      : { providerId: requireText(options.providerId, "providerId") }),
    ...(options.configId === undefined
      ? {}
      : { configId: requireText(options.configId, "configId") }),
    ...(options.actionInstanceId === undefined
      ? {}
      : { actionInstanceId: requireText(options.actionInstanceId, "actionInstanceId") }),
    ...(options.bindingId === undefined
      ? {}
      : { bindingId: requireText(options.bindingId, "bindingId") }),
    ...(options.pageSessionId === undefined
      ? {}
      : { pageSessionId: requireText(options.pageSessionId, "pageSessionId") }),
  });
}

export function actionProviderInstanceSubject(input: {
  providerInstanceId: string;
  providerId: string;
}): EntitySubject {
  return entitySubject("action_provider_instance", {
    providerInstanceId: requireText(input.providerInstanceId, "providerInstanceId"),
    providerId: requireText(input.providerId, "providerId"),
  });
}

export function subjectContextId(subject: EntitySubject): string | undefined {
  return optionalTextValue(subject.identifiers.contextId, "contextId");
}

export function subjectDeviceId(subject: EntitySubject): string | undefined {
  return optionalTextValue(subject.identifiers.deviceId, "deviceId");
}

export function actionBody(message: DeckrMessage): JsonObject {
  return message.body;
}

export function actionMessageForProvider(
  message: DeckrMessage,
  providerInstanceId: string,
): boolean {
  return messageTargetsEndpoint(message, actionProviderAddress(providerInstanceId));
}

export function actionMessageForController(
  message: DeckrMessage,
  controllerId?: string,
): boolean {
  if (controllerId !== undefined) {
    return messageTargetsEndpoint(message, controllerAddress(controllerId));
  }
  if (message.recipient.targetType === "endpoint") {
    return parseEndpointAddress(message.recipient.endpoint).family === "controller";
  }
  return message.recipient.endpointFamily === "controller";
}

export function settingsTargetKey(target: SettingsTargetRef): string {
  const validated = validateSettingsTargetRef(target);
  const parts = [
    "settings",
    "target",
    encodeKeyToken(validated.scope),
    encodeKeyToken(validated.controllerId),
    encodeKeyToken(validated.configId),
    encodeKeyToken(validated.providerInstanceId),
    encodeKeyToken(validated.providerId),
  ];
  if (validated.scope === "action_instance") {
    parts.push(
      encodeKeyToken(validated.actionId ?? ""),
      encodeKeyToken(validated.actionInstanceId ?? ""),
      validated.stableId === undefined ? "0" : "1",
    );
    if (validated.stableId !== undefined) {
      parts.push(encodeKeyToken(validated.stableId));
    }
  }
  return parts.join(".");
}

export function parseSettingsTargetKey(key: string): SettingsTargetRef | null {
  const parts = key.split(".");
  if (parts.length < 7 || parts[0] !== "settings" || parts[1] !== "target") {
    return null;
  }
  try {
    const scope = decodeKeyToken(parts[2] ?? "");
    const controllerId = decodeKeyToken(parts[3] ?? "");
    const configId = decodeKeyToken(parts[4] ?? "");
    const providerInstanceId = decodeKeyToken(parts[5] ?? "");
    const providerId = decodeKeyToken(parts[6] ?? "");
    if (scope === "action_provider_instance" && parts.length === 7) {
      return validateSettingsTargetRef({
        scope,
        controllerId,
        configId,
        providerInstanceId,
        providerId,
      });
    }
    if (scope !== "action_instance" || ![10, 11].includes(parts.length)) {
      return null;
    }
    const stableFlag = parts[9];
    if (stableFlag === "0" && parts.length !== 10) {
      return null;
    }
    if (stableFlag === "1" && parts.length !== 11) {
      return null;
    }
    if (stableFlag !== "0" && stableFlag !== "1") {
      return null;
    }
    return validateSettingsTargetRef({
      scope,
      controllerId,
      configId,
      providerInstanceId,
      providerId,
      actionId: decodeKeyToken(parts[7] ?? ""),
      actionInstanceId: decodeKeyToken(parts[8] ?? ""),
      ...(stableFlag === "1" ? { stableId: decodeKeyToken(parts[10] ?? "") } : {}),
    });
  } catch {
    return null;
  }
}

export function settingsTargetPayload(target: SettingsTargetRef): JsonObject {
  return validateSettingsTargetRef(target) as unknown as JsonObject;
}

export function validateDeviceRef(value: unknown): DeviceRef {
  const raw = requireJsonObject(value, "deviceRef");
  const ref: DeviceRef = {
    managerId: requireText(raw.managerId, "managerId"),
    deviceId: requireText(raw.deviceId, "deviceId"),
  };
  setOptionalTextValue(ref, "fingerprint", raw.fingerprint, "fingerprint");
  return ref;
}

export function validateControlRef(value: unknown): ControlRef {
  const raw = requireJsonObject(value, "controlRef");
  return {
    deviceRef: validateDeviceRef(raw.deviceRef),
    controlId: requireText(raw.controlId, "controlId"),
  };
}

export function validateCapabilityRef(value: unknown): CapabilityRef {
  const raw = requireJsonObject(value, "capabilityRef");
  const ref: CapabilityRef = {
    capabilityId: requireText(raw.capabilityId, "capabilityId"),
  };
  if (raw.deviceRef !== undefined && raw.deviceRef !== null) {
    ref.deviceRef = validateDeviceRef(raw.deviceRef);
  }
  setOptionalTextValue(ref, "controlId", raw.controlId, "controlId");
  return ref;
}

export function validateMatchedCapability(value: unknown): MatchedCapability {
  const raw = requireJsonObject(value, "matchedCapability");
  const direction = requireText(raw.direction, "matched capability direction");
  if (!isCapabilityDirection(direction)) {
    throw new ValidationError("matched capability direction is invalid");
  }
  const match: MatchedCapability = {
    capability: validateCapabilityRef(raw.capability),
    family: requireText(raw.family, "matched capability family"),
    type: requireText(raw.type, "matched capability type"),
    direction,
  };
  setOptionalTextValue(match, "requirementName", raw.requirementName, "requirementName");
  setOptionalTextValue(match, "roleId", raw.roleId, "roleId");
  setOptionalTextListValue(match, "eventTypes", raw.eventTypes, "eventTypes");
  setOptionalTextListValue(match, "commandTypes", raw.commandTypes, "commandTypes");
  if (raw.provenance !== undefined && raw.provenance !== null) {
    const provenance = requireText(raw.provenance, "provenance");
    if (!["native", "projection", "derivation", "extension"].includes(provenance)) {
      throw new ValidationError("matched capability provenance is invalid");
    }
    match.provenance = provenance as MatchedCapability["provenance"];
  }
  if (raw.source !== undefined && raw.source !== null) {
    match.source = validateCapabilityRef(raw.source);
  }
  return match;
}

export function validateBindingMetadata(value: unknown): BindingMetadata {
  const raw = requireJsonObject(value, "binding metadata");
  const binding: BindingMetadata = {
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
    actionId: requireText(raw.actionId, "actionId"),
    actionInstanceId: requireText(raw.actionInstanceId, "actionInstanceId"),
    configId: requireText(raw.configId, "configId"),
    contextId: requireText(raw.contextId, "contextId"),
    bindingId: requireText(raw.bindingId, "bindingId"),
    deviceRef: validateDeviceRef(raw.deviceRef),
    controlRef: validateControlRef(raw.controlRef),
  };
  setOptionalTextValue(binding, "pageSessionId", raw.pageSessionId, "pageSessionId");
  setOptionalTextValue(binding, "roleId", raw.roleId, "roleId");
  setOptionalTextValue(binding, "itemKey", raw.itemKey, "itemKey");
  setOptionalTextValue(binding, "handler", raw.handler, "handler");
  if (raw.matchedCapabilities !== undefined && raw.matchedCapabilities !== null) {
    if (!Array.isArray(raw.matchedCapabilities)) {
      throw new ValidationError("matchedCapabilities must be an array");
    }
    binding.matchedCapabilities = raw.matchedCapabilities.map(validateMatchedCapability);
  } else {
    binding.matchedCapabilities = [];
  }
  binding.outputGeneration =
    raw.outputGeneration === undefined || raw.outputGeneration === null
      ? 0
      : requireNonNegativeInteger(raw.outputGeneration, "outputGeneration");
  return binding;
}

export function validateActionInstanceMetadata(value: unknown): ActionInstanceMetadata {
  const raw = requireJsonObject(value, "action instance metadata");
  return {
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
    actionId: requireText(raw.actionId, "actionId"),
    actionInstanceId: requireText(raw.actionInstanceId, "actionInstanceId"),
    configId: requireText(raw.configId, "configId"),
    contextId: requireText(raw.contextId, "contextId"),
  };
}

export function validatePageSessionMetadata(value: unknown): PageSessionMetadata {
  const raw = requireJsonObject(value, "page session metadata");
  const session: PageSessionMetadata = {
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
    actionInstanceId: requireText(raw.actionInstanceId, "actionInstanceId"),
    configId: requireText(raw.configId, "configId"),
    pageId: requireText(raw.pageId, "pageId"),
    pageSessionId: requireText(raw.pageSessionId, "pageSessionId"),
    contextId: requireText(raw.contextId, "contextId"),
  };
  setOptionalTextValue(session, "templateId", raw.templateId, "templateId");
  setOptionalTextValue(session, "ownerBindingId", raw.ownerBindingId, "ownerBindingId");
  if (raw.bindings !== undefined && raw.bindings !== null) {
    if (!Array.isArray(raw.bindings)) {
      throw new ValidationError("page session bindings must be an array");
    }
    session.bindings = raw.bindings.map(validateBindingMetadata);
  } else {
    session.bindings = [];
  }
  return session;
}

export function validateCapabilityInputEvent(value: unknown): CapabilityInputEvent {
  const raw = requireJsonObject(value, "capability input event");
  const event: CapabilityInputEvent = {
    capability: validateCapabilityRef(raw.capability),
    eventType: requireText(raw.eventType, "eventType"),
    occurredAt: requireText(raw.occurredAt, "occurredAt"),
  };
  if (raw.value !== undefined) {
    event.value = cloneJson(raw.value as JsonValue);
  }
  if (raw.sequence !== undefined && raw.sequence !== null) {
    event.sequence = requireNonNegativeInteger(raw.sequence, "sequence");
  }
  setOptionalTextValue(event, "producer", raw.producer, "producer");
  if (raw.source !== undefined && raw.source !== null) {
    event.source = validateCapabilityRef(raw.source);
  }
  if (raw.view !== undefined && raw.view !== null) {
    const view = requireText(raw.view, "view");
    if (!["raw", "native", "projected", "derived", "extension"].includes(view)) {
      throw new ValidationError("capability input view is invalid");
    }
    event.view = view as CapabilityInputEvent["view"];
  }
  return event;
}

export function validateSettingsTargetRef(value: unknown): SettingsTargetRef {
  const raw = requireJsonObject(value, "settings target");
  const scope = requireText(raw.scope, "settings target scope");
  if (scope !== "action_provider_instance" && scope !== "action_instance") {
    throw new ValidationError("settings target scope is invalid");
  }
  const target: SettingsTargetRef = {
    scope,
    controllerId: requireText(raw.controllerId, "controllerId"),
    configId: requireText(raw.configId, "configId"),
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
  };
  const actionId = optionalTextValue(raw.actionId, "actionId");
  const actionInstanceId = optionalTextValue(raw.actionInstanceId, "actionInstanceId");
  const stableId = optionalTextValue(raw.stableId, "stableId");
  if (scope === "action_provider_instance") {
    if (actionId !== undefined || actionInstanceId !== undefined || stableId !== undefined) {
      throw new ValidationError(
        "action provider instance settings target must not include action ids",
      );
    }
    return target;
  }
  if (actionId === undefined || actionInstanceId === undefined) {
    throw new ValidationError("action instance settings target requires actionId and actionInstanceId");
  }
  target.actionId = actionId;
  target.actionInstanceId = actionInstanceId;
  if (stableId !== undefined) {
    target.stableId = stableId;
  }
  return target;
}

export function validateSettingsSchemaMetadata(value: unknown): SettingsSchemaMetadata {
  const raw = requireJsonObject(value, "settings schema metadata");
  const metadata: SettingsSchemaMetadata = {};
  setOptionalTextValue(metadata, "schemaId", raw.schemaId, "schemaId");
  if (raw.schema !== undefined && raw.schema !== null) {
    metadata.schema = requireJsonObject(raw.schema, "schema");
  }
  if (raw.stale !== undefined) {
    if (typeof raw.stale !== "boolean") {
      throw new ValidationError("settings schema metadata stale must be boolean");
    }
    metadata.stale = raw.stale;
  }
  return metadata;
}

export function validateSettingsSnapshot(value: unknown): SettingsSnapshot {
  const raw = requireJsonObject(value, "settings snapshot");
  const snapshot: SettingsSnapshot = {
    target: validateSettingsTargetRef(raw.target),
    settings:
      raw.settings === undefined || raw.settings === null
        ? {}
        : requireJsonObject(raw.settings, "settings"),
  };
  setOptionalTextListValue(snapshot, "provenance", raw.provenance, "provenance");
  if (raw.schemaMetadata !== undefined && raw.schemaMetadata !== null) {
    snapshot.schemaMetadata = validateSettingsSchemaMetadata(raw.schemaMetadata);
  }
  return snapshot;
}

export function validateSettingsTargetDescription(value: unknown): SettingsTargetDescription {
  const raw = requireJsonObject(value, "settings target description");
  const target = validateSettingsTargetRef(raw.target);
  const description: SettingsTargetDescription = {
    target,
    providerInstanceId: requireText(raw.providerInstanceId, "providerInstanceId"),
    providerId: requireText(raw.providerId, "providerId"),
  };
  setOptionalTextValue(description, "actionId", raw.actionId, "actionId");
  setOptionalTextValue(description, "label", raw.label, "label");
  if (raw.placement !== undefined && raw.placement !== null) {
    description.placement = requireJsonObject(raw.placement, "placement");
  }
  if (raw.schemaMetadata !== undefined && raw.schemaMetadata !== null) {
    description.schemaMetadata = validateSettingsSchemaMetadata(raw.schemaMetadata);
  }
  setOptionalTextListValue(description, "provenance", raw.provenance, "provenance");
  if (description.providerInstanceId !== target.providerInstanceId) {
    throw new ValidationError("settings target description providerInstanceId must match target");
  }
  if (description.providerId !== target.providerId) {
    throw new ValidationError("settings target description providerId must match target");
  }
  if (description.actionId !== target.actionId) {
    throw new ValidationError("settings target description actionId must match target");
  }
  return description;
}

function validateCapabilityRequirement(value: unknown): CapabilityRequirement {
  const raw = requireJsonObject(value, "capability requirement");
  if (!Array.isArray(raw.preferences)) {
    throw new ValidationError("capability requirement preferences must be an array");
  }
  const requirement: CapabilityRequirement = {
    name: requireText(raw.name, "capability requirement name"),
    preferences: raw.preferences.map(validateCapabilityRequirementSelector),
  };
  if (raw.required !== undefined) {
    if (typeof raw.required !== "boolean") {
      throw new ValidationError("capability requirement required must be boolean");
    }
    requirement.required = raw.required;
  }
  setOptionalTextList(requirement, "eventTypes", raw.eventTypes, "eventTypes", false);
  setOptionalTextList(requirement, "commandTypes", raw.commandTypes, "commandTypes", false);
  setOptionalTextList(requirement, "views", raw.views, "views", false);
  return requirement;
}

function validateCapabilityRequirementSelector(value: unknown): CapabilityRequirementSelector {
  const raw = requireJsonObject(value, "capability requirement selector");
  const selector: CapabilityRequirementSelector = {};
  setOptionalText(selector, "capabilityId", raw.capabilityId, "capabilityId", true);
  setOptionalText(selector, "family", raw.family, "family", true);
  setOptionalText(selector, "type", raw.type, "type", true);
  setOptionalText(selector, "direction", raw.direction, "direction", true);
  setOptionalTextList(selector, "eventTypes", raw.eventTypes, "eventTypes", false);
  setOptionalTextList(selector, "commandTypes", raw.commandTypes, "commandTypes", false);
  return selector;
}

function validateDynamicPageTemplate(value: unknown): DynamicPageTemplateDescriptor {
  const raw = requireJsonObject(value, "dynamic page template");
  if (!Array.isArray(raw.roles)) {
    throw new ValidationError("dynamic page template roles must be an array");
  }
  return {
    templateId: requireText(raw.templateId, "templateId"),
    roles: raw.roles.map(validateDynamicPageRole),
  };
}

function validateDynamicPageRole(value: unknown): DynamicPageRoleDescriptor {
  const raw = requireJsonObject(value, "dynamic page role");
  if (!Array.isArray(raw.requirements)) {
    throw new ValidationError("dynamic page role requirements must be an array");
  }
  const role: DynamicPageRoleDescriptor = {
    roleId: requireText(raw.roleId, "roleId"),
    requirements: raw.requirements.map(validateCapabilityRequirement),
  };
  setOptionalText(role, "cardinality", raw.cardinality, "cardinality", false);
  if (raw.optional !== undefined) {
    if (typeof raw.optional !== "boolean") {
      throw new ValidationError("dynamic page role optional must be boolean");
    }
    role.optional = raw.optional;
  }
  setOptionalInteger(role, "min", raw.min, "min");
  setOptionalInteger(role, "preferred", raw.preferred, "preferred");
  setOptionalInteger(role, "max", raw.max, "max");
  setOptionalObject(role, "layout", raw.layout, "layout", false);
  return role;
}

function validateProfileCapacity(value: unknown): ProfileCapacity {
  const raw = requireJsonObject(value, "profile capacity");
  const capacity: ProfileCapacity = {};
  setOptionalInteger(capacity, "totalInstances", raw.totalInstances, "totalInstances", true);
  setOptionalInteger(capacity, "claimedInstances", raw.claimedInstances, "claimedInstances");
  setOptionalInteger(capacity, "availableInstances", raw.availableInstances, "availableInstances", true);
  return capacity;
}

function validateStringRecord(value: unknown, fieldName: string): Record<string, string> {
  const raw = requireJsonObject(value, fieldName);
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(raw)) {
    out[requireText(key, `${fieldName} key`)] = requireText(item, `${fieldName} value`);
  }
  return out;
}

function setOptionalText<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
  nullable: boolean,
): void {
  if (value === undefined) {
    return;
  }
  if (value === null && nullable) {
    (target as Record<string, unknown>)[key] = null;
    return;
  }
  (target as Record<string, unknown>)[key] = requireText(value, fieldName);
}

function setOptionalTextList<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
  nullable: boolean,
): void {
  if (value === undefined) {
    return;
  }
  if (value === null && nullable) {
    (target as Record<string, unknown>)[key] = null;
    return;
  }
  if (!Array.isArray(value)) {
    throw new ValidationError(`${fieldName} must be an array`);
  }
  (target as Record<string, unknown>)[key] = value.map((item) => requireText(item, fieldName));
}

function setOptionalObject<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
  nullable: boolean,
): void {
  if (value === undefined) {
    return;
  }
  if (value === null && nullable) {
    (target as Record<string, unknown>)[key] = null;
    return;
  }
  (target as Record<string, unknown>)[key] = requireJsonObject(value, fieldName);
}

function setOptionalInteger<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
  nullable = false,
): void {
  if (value === undefined) {
    return;
  }
  if (value === null && nullable) {
    (target as Record<string, unknown>)[key] = null;
    return;
  }
  if (!Number.isInteger(value)) {
    throw new ValidationError(`${fieldName} must be an integer`);
  }
  (target as Record<string, unknown>)[key] = value;
}

function optionalTextValue(value: unknown, fieldName: string): string | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  return requireText(value, fieldName);
}

function setOptionalTextValue<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
): void {
  const text = optionalTextValue(value, fieldName);
  if (text !== undefined) {
    (target as Record<string, unknown>)[key] = text;
  }
}

function setOptionalTextListValue<T extends object, K extends keyof T & string>(
  target: T,
  key: K,
  value: unknown,
  fieldName: string,
): void {
  if (value === undefined || value === null) {
    return;
  }
  if (!Array.isArray(value)) {
    throw new ValidationError(`${fieldName} must be an array`);
  }
  (target as Record<string, unknown>)[key] = value.map((item) =>
    requireText(item, fieldName),
  );
}

function requireNonNegativeInteger(value: unknown, fieldName: string): number {
  if (!Number.isInteger(value) || Number(value) < 0) {
    throw new ValidationError(`${fieldName} must be a non-negative integer`);
  }
  return Number(value);
}

function isCapabilityDirection(
  value: string,
): value is MatchedCapability["direction"] {
  return ["input", "output", "state", "command"].includes(value);
}
