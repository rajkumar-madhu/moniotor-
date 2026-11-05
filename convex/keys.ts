const TENANT_KEY = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
const OPERATOR_ID = /^[A-Za-z0-9@._:-]{1,120}$/;

export const LIMITS = {
  alerts: 25,
  assets: 200,
  threads: 20,
  assetMatches: 5,
  runbookChars: 50_000,
  promptChars: 8_000,
  titleChars: 120,
  nameChars: 100,
  detailChars: 2_000,
  messageChars: 4_000,
  excerptChars: 4_000,
};

export function randomKey(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function safeEqual(left: string, right: string): boolean {
  const encodedLeft = new TextEncoder().encode(left);
  const encodedRight = new TextEncoder().encode(right);
  const length = Math.max(encodedLeft.byteLength, encodedRight.byteLength);
  let diff = encodedLeft.byteLength === encodedRight.byteLength ? 0 : 1;
  for (let index = 0; index < length; index += 1) {
    diff |= (encodedLeft[index] ?? 0) ^ (encodedRight[index] ?? 0);
  }
  return diff === 0;
}

// LINKEDEYE_AGENT_TOKEN is a Convex deployment env var (`npx convex env set`).
// Ingest and thread creation fail until it is set.
export function assertAgentToken(token: string): void {
  const expected = process.env.LINKEDEYE_AGENT_TOKEN;
  if (!expected || !safeEqual(token, expected)) {
    throw new Error("Unauthorized");
  }
}

export function tenantKey(value: string): string {
  const key = value.trim();
  if (!TENANT_KEY.test(key)) {
    throw new Error(
      "Tenant key must be 1-100 letters, numbers, hyphens, or underscores",
    );
  }
  return key;
}

export function operatorId(value: string): string {
  const id = value.trim();
  if (!OPERATOR_ID.test(id)) {
    throw new Error("Operator id must be 1-120 letters, numbers, or . @ _ : -");
  }
  return id;
}

export function boundedText(value: string, field: string, max: number): string {
  const text = value.trim();
  if (!text || text.length > max) {
    throw new Error(`${field} must be 1-${max} characters`);
  }
  return text;
}

export function slugCode(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return slug || "site";
}
