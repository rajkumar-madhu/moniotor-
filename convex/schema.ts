import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const vSeverity = v.union(
  v.literal("critical"),
  v.literal("warning"),
  v.literal("info"),
);

export const vAlertStatus = v.union(
  v.literal("open"),
  v.literal("acknowledged"),
  v.literal("resolved"),
);

export const vAssetStatus = v.union(
  v.literal("up"),
  v.literal("down"),
  v.literal("degraded"),
  v.literal("unknown"),
);

export const vAssetKind = v.union(
  v.literal("host"),
  v.literal("service"),
  v.literal("check"),
  v.literal("device"),
);

export const vCriticality = v.union(
  v.literal("critical"),
  v.literal("high"),
  v.literal("standard"),
  v.literal("low"),
);

export default defineSchema({
  tenants: defineTable({
    key: v.string(),
    name: v.string(),
  }).index("by_key", ["key"]),

  sites: defineTable({
    tenantId: v.id("tenants"),
    name: v.string(),
    code: v.string(),
    region: v.string(),
    criticality: vCriticality,
    example: v.boolean(),
  })
    .index("by_tenant", ["tenantId"])
    .index("by_tenant_and_name", ["tenantId", "name"]),

  assets: defineTable({
    tenantId: v.id("tenants"),
    siteId: v.id("sites"),
    siteName: v.string(),
    name: v.string(),
    kind: vAssetKind,
    status: vAssetStatus,
    detail: v.string(),
    example: v.boolean(),
  })
    .index("by_tenant_and_site", ["tenantId", "siteId"])
    .index("by_tenant_and_name", ["tenantId", "name"]),

  alerts: defineTable({
    tenantId: v.id("tenants"),
    siteId: v.id("sites"),
    siteName: v.string(),
    assetName: v.string(),
    fingerprint: v.string(),
    severity: vSeverity,
    status: vAlertStatus,
    title: v.string(),
    message: v.string(),
    openedAt: v.number(),
    example: v.boolean(),
  })
    .index("by_tenant_and_fingerprint", ["tenantId", "fingerprint"])
    .index("by_tenant_and_status", ["tenantId", "status"])
    .index("by_tenant_status_and_severity", ["tenantId", "status", "severity"])
    .index("by_tenant_site_and_status", ["tenantId", "siteId", "status"])
    .index("by_tenant_site_status_and_severity", [
      "tenantId",
      "siteId",
      "status",
      "severity",
    ]),

  threads: defineTable({
    threadId: v.string(),
    tenantId: v.id("tenants"),
    operatorId: v.string(),
    accessKey: v.string(),
    title: v.string(),
    lastActivityAt: v.number(),
  })
    .index("by_threadId", ["threadId"])
    .index("by_tenant_operator_activity", [
      "tenantId",
      "operatorId",
      "lastActivityAt",
    ]),
});
