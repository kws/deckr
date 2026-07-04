import { requireJsonObject, requirePositiveInteger, requireText } from "./json.ts";

export interface ContractPointer {
  contractId: string;
  generation: number;
}

export function validateContractPointer(value: unknown): ContractPointer {
  const raw = requireJsonObject(value, "Concord contract pointer");
  return {
    contractId: requireText(raw.contractId, "contractId"),
    generation: requirePositiveInteger(raw.generation, "generation"),
  };
}

export function contractPointersEqual(
  left: ContractPointer | undefined,
  right: ContractPointer | undefined,
): boolean {
  return left?.contractId === right?.contractId && left?.generation === right?.generation;
}
