import type { DeckrMessage, EntitySubject } from "./identity.js";
import {
  hardwareManagerAddress,
  hardwareSubjectForCapability,
  hardwareSubjectForDevice,
} from "./identity.js";
import type {
  CapabilityRef,
  ControlRef,
  DeviceDescriptor,
  DeviceRef,
  JsonObject,
  JsonValue,
} from "./state.js";

export const HARDWARE_MESSAGES_SCHEMA_ID = "dev.deckr.message.hardware_messages.v1";
export const DECKR_PROTOCOL_VERSION = "1";

export const DEVICE_AVAILABLE = "deviceAvailable";
export const DEVICE_DESCRIPTOR_CHANGED = "deviceDescriptorChanged";
export const DEVICE_UNAVAILABLE = "deviceUnavailable";
export const CONTROL_INPUT = "controlInput";
export const CONTROL_COMMAND = "controlCommand";
export const CAPABILITY_STATE_CHANGED = "capabilityStateChanged";
export const CAPABILITY_STATE_REQUEST = "capabilityStateRequest";
export const CAPABILITY_STATE_REPLY = "capabilityStateReply";
export const COMMAND_ACCEPTED = "commandAccepted";
export const COMMAND_REJECTED = "commandRejected";
export const COMMAND_REPLY = "commandReply";

export interface ControlGeometry {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  unit?: string;
}

export interface CapabilitySchema {
  schemaId?: string;
  schema?: JsonObject;
}

export interface CapabilityDescriptor {
  capabilityId: string;
  family: string;
  type: string;
  direction: "input" | "output" | "state" | "command";
  access?: string[];
  eventTypes?: string[];
  commandTypes?: string[];
  valueSchema?: CapabilitySchema;
  commandSchema?: CapabilitySchema;
  constraints?: JsonObject[];
  units?: JsonObject[];
  projections?: JsonObject[];
  sources?: JsonObject[];
}

export interface ControlDescriptor {
  controlId: string;
  kind: string;
  geometry?: ControlGeometry;
  inputCapabilities?: CapabilityDescriptor[];
  outputCapabilities?: CapabilityDescriptor[];
  stateCapabilities?: CapabilityDescriptor[];
  configCapabilities?: CapabilityDescriptor[];
  diagnosticCapabilities?: CapabilityDescriptor[];
  relatedControlIds?: string[];
  sources?: JsonObject[];
}

export type HardwareMessageBody = JsonObject;

export {
  hardwareManagerAddress,
  hardwareSubjectForCapability,
  hardwareSubjectForDevice,
  type CapabilityRef,
  type ControlRef,
  type DeviceDescriptor,
  type DeviceRef,
  type JsonObject,
  type JsonValue,
};

export function hardwareBodyFromMessage(message: DeckrMessage): HardwareMessageBody {
  if (!HARDWARE_MESSAGE_TYPES.has(message.messageType)) {
    throw new Error(`unsupported hardware message type ${message.messageType}`);
  }
  return message.body;
}

export function hardwareSubjectForRef(ref: CapabilityRef): EntitySubject {
  const deviceRef = ref.deviceRef;
  if (deviceRef === undefined || ref.controlId === undefined) {
    throw new Error("hardware capability subject requires deviceRef and controlId");
  }
  return hardwareSubjectForCapability({
    capabilityId: ref.capabilityId,
    controlId: ref.controlId,
    deviceId: deviceRef.deviceId,
    managerId: deviceRef.managerId,
  });
}

const HARDWARE_MESSAGE_TYPES = new Set([
  DEVICE_AVAILABLE,
  DEVICE_DESCRIPTOR_CHANGED,
  DEVICE_UNAVAILABLE,
  CONTROL_INPUT,
  CONTROL_COMMAND,
  CAPABILITY_STATE_CHANGED,
  CAPABILITY_STATE_REQUEST,
  CAPABILITY_STATE_REPLY,
  COMMAND_ACCEPTED,
  COMMAND_REJECTED,
  COMMAND_REPLY,
]);
