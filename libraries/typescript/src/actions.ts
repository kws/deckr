import {
  actionProviderAddress,
  actionProviderInstanceSubject,
  contextSubject,
  messageTargetsEndpoint,
  parseControllerAddress,
  type DeckrMessage,
  type EntitySubject,
} from "./identity.js";
import type {
  BindingMetadata,
  CapabilityInputEvent,
  CapabilityRef,
  JsonObject,
  JsonValue,
  MatchedCapability,
  PageSessionMetadata,
  RegisteredAction,
  SettingsSnapshot,
  SettingsTargetRef,
} from "./state.js";
import { parseSettingsTargetKey, settingsTargetKey } from "./state.js";

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

export {
  actionProviderAddress,
  actionProviderInstanceSubject,
  contextSubject,
  parseSettingsTargetKey,
  settingsTargetKey,
  type BindingMetadata,
  type CapabilityInputEvent,
  type CapabilityRef,
  type JsonObject,
  type JsonValue,
  type MatchedCapability,
  type PageSessionMetadata,
  type RegisteredAction,
  type SettingsSnapshot,
  type SettingsTargetRef,
};

export function actionBody(message: DeckrMessage): JsonObject {
  return message.body;
}

export function actionMessageForProvider(
  message: DeckrMessage,
  providerInstanceId: string,
): boolean {
  return messageTargetsEndpoint(message, actionProviderAddress(providerInstanceId));
}

export function isFromExpectedController(
  message: DeckrMessage,
  controllerId: string,
): boolean {
  return parseControllerAddress(message.sender) === controllerId;
}

export function settingsSubject(input: {
  controllerId: string;
  providerInstanceId: string;
  actionInstanceId?: string;
}): EntitySubject {
  return {
    kind: "settings",
    identifiers: {
      ...(input.actionInstanceId === undefined
        ? {}
        : { actionInstanceId: input.actionInstanceId }),
      controllerId: input.controllerId,
      providerInstanceId: input.providerInstanceId,
    },
  };
}
