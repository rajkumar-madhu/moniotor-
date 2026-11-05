import assert from "node:assert/strict";
import test from "node:test";
import { rollupStatuses } from "../convex/health.ts";
import { assertAgentToken, safeEqual, slugCode, tenantKey } from "../convex/keys.ts";

test("counts known asset statuses and ignores anything else", () => {
  assert.deepEqual(
    rollupStatuses(["up", "up", "down", "degraded", "unknown", "missing"]),
    { up: 2, down: 1, degraded: 1, unknown: 1 },
  );
});

test("compares secrets without treating a shared prefix as equal", () => {
  assert.equal(safeEqual("abc", "abc"), true);
  assert.equal(safeEqual("abc", "abd"), false);
  assert.equal(safeEqual("abc", "abcd"), false);
  assert.equal(safeEqual("", ""), true);
});

test("accepts a tenant key and rejects blank or spaced keys", () => {
  assert.equal(tenantKey("acme-west"), "acme-west");
  assert.throws(() => tenantKey("acme west"), /Tenant key/);
  assert.throws(() => tenantKey(""), /Tenant key/);
});

test("builds a site code from a display name", () => {
  assert.equal(slugCode("Mumbai (NSE)"), "mumbai-nse");
});

test("rejects calls until the agent token is set, then checks it", () => {
  const previous = process.env.LINKEDEYE_AGENT_TOKEN;
  delete process.env.LINKEDEYE_AGENT_TOKEN;
  assert.throws(() => assertAgentToken("anything"), /Unauthorized/);
  process.env.LINKEDEYE_AGENT_TOKEN = "local-test-token";
  assert.doesNotThrow(() => assertAgentToken("local-test-token"));
  assert.throws(() => assertAgentToken("other"), /Unauthorized/);
  if (previous === undefined) delete process.env.LINKEDEYE_AGENT_TOKEN;
  else process.env.LINKEDEYE_AGENT_TOKEN = previous;
});
