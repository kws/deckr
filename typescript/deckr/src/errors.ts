import type { JsonObject } from "./json.ts";

export class DeckrError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DeckrError";
  }
}

export class StateConflict extends DeckrError {
  constructor(message: string) {
    super(message);
    this.name = "StateConflict";
  }
}

export class StateUnavailable extends DeckrError {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = "StateUnavailable";
    if (options !== undefined && "cause" in options) {
      this.cause = options.cause;
    }
  }
}

export class ValidationError extends DeckrError {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export class ServiceUnavailable extends DeckrError {
  readonly code: string;
  readonly diagnostics: JsonObject;

  constructor(code: string, message: string, diagnostics: JsonObject = {}) {
    super(message);
    this.name = "ServiceUnavailable";
    this.code = code;
    this.diagnostics = diagnostics;
  }
}
