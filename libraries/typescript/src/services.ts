import type { DeckrMessage, EntitySubject } from "./identity.js";
import { serviceAddress } from "./identity.js";
import {
  parseServiceCatalogKey,
  parseServiceStatusKey,
  parseServiceViewKey,
  serviceCatalogKey,
  serviceStatusKey,
  serviceViewKey,
  type JsonObject,
  type ServiceCatalog,
  type ServiceStatus,
} from "./state.js";

export const SERVICE_COMMAND = "serviceCommand";
export const SERVICE_COMMAND_REPLY = "serviceCommandReply";

export {
  parseServiceCatalogKey,
  parseServiceStatusKey,
  parseServiceViewKey,
  serviceAddress,
  serviceCatalogKey,
  serviceStatusKey,
  serviceViewKey,
  type JsonObject,
  type ServiceCatalog,
  type ServiceStatus,
};

export function serviceBody(message: DeckrMessage): JsonObject {
  if (message.messageType !== SERVICE_COMMAND && message.messageType !== SERVICE_COMMAND_REPLY) {
    throw new Error(`unsupported service message type ${message.messageType}`);
  }
  return message.body;
}

export function serviceSubject(input: {
  serviceId: string;
  namespace: string;
  operation: string;
}): EntitySubject {
  return {
    kind: "service",
    identifiers: {
      namespace: input.namespace,
      operation: input.operation,
      serviceId: input.serviceId,
    },
  };
}
