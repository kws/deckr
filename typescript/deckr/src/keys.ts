const SAFE_TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

export function encodeKeyToken(raw: string): string {
  if (SAFE_TOKEN_RE.test(raw) && !raw.startsWith("b64_")) {
    return raw;
  }
  return `b64_${Buffer.from(raw, "utf8").toString("base64url").replace(/=+$/, "")}`;
}

export function decodeKeyToken(token: string): string {
  if (!token.startsWith("b64_")) {
    return token;
  }
  return Buffer.from(token.slice(4), "base64url").toString("utf8");
}
