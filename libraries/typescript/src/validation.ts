import { Ajv2020 } from "ajv/dist/2020.js";
import * as addFormatsModule from "ajv-formats";
import type { AnySchema } from "ajv";

const addFormats = (
  "default" in addFormatsModule ? addFormatsModule.default : addFormatsModule
) as unknown as (ajv: Ajv2020) => void;

export function createContractAjv(): Ajv2020 {
  const ajv = new Ajv2020({
    allErrors: true,
    strict: false,
    validateFormats: true,
  });
  addFormats(ajv);
  ajv.addFormat("date-time", {
    type: "string",
    validate(value: string): boolean {
      if (
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
          value,
        )
      ) {
        return false;
      }
      return Number.isFinite(Date.parse(value));
    },
  });
  return ajv;
}

export function validateJsonSchema(schema: unknown, data: unknown): boolean {
  const validate = createContractAjv().compile(schema as AnySchema);
  return validate(data) as boolean;
}
