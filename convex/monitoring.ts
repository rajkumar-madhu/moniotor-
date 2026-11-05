import { v } from "convex/values";
import { internalQuery } from "./_generated/server";
import { rollupStatuses } from "./health";
import { LIMITS, safeEqual } from "./keys";
import { vSeverity } from "./schema";

const activeStatuses = ["open", "acknowledged"] as const;

export const getTenantByKey = internalQuery({
  args: { key: v.string() },
  handler: async (ctx, args) => {
    const tenant = await ctx.db
      .query("tenants")
      .withIndex("by_key", (q) => q.eq("key", args.key))
      .unique();
    if (!tenant) return null;
    return { _id: tenant._id, key: tenant.key, name: tenant.name };
  },
});

export const getThread = internalQuery({
  args: { threadId: v.string(), accessKey: v.string() },
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("threads")
      .withIndex("by_threadId", (q) => q.eq("threadId", args.threadId))
      .unique();
    if (!row || !safeEqual(row.accessKey, args.accessKey)) return null;
    const tenant = await ctx.db.get(row.tenantId);
    if (!tenant) return null;
    return {
      tenantId: tenant._id,
      tenantKey: tenant.key,
      operatorId: row.operatorId,
    };
  },
});

export const getThreadById = internalQuery({
  args: { threadId: v.string() },
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("threads")
      .withIndex("by_threadId", (q) => q.eq("threadId", args.threadId))
      .unique();
    if (!row) return null;
    const tenant = await ctx.db.get(row.tenantId);
    if (!tenant) return null;
    return {
      tenantId: tenant._id,
      tenantKey: tenant.key,
      operatorId: row.operatorId,
    };
  },
});

export const listOpenAlerts = internalQuery({
  args: {
    tenantId: v.id("tenants"),
    siteName: v.optional(v.string()),
    severity: v.optional(vSeverity),
  },
  handler: async (ctx, args) => {
    let siteId = undefined;
    if (args.siteName) {
      const site = await ctx.db
        .query("sites")
        .withIndex("by_tenant_and_name", (q) =>
          q.eq("tenantId", args.tenantId).eq("name", args.siteName!),
        )
        .unique();
      if (!site) return [];
      siteId = site._id;
    }

    const rows = [];
    for (const status of activeStatuses) {
      const page = siteId
        ? args.severity
          ? await ctx.db
              .query("alerts")
              .withIndex("by_tenant_site_status_and_severity", (q) =>
                q
                  .eq("tenantId", args.tenantId)
                  .eq("siteId", siteId)
                  .eq("status", status)
                  .eq("severity", args.severity!),
              )
              .take(LIMITS.alerts)
          : await ctx.db
              .query("alerts")
              .withIndex("by_tenant_site_and_status", (q) =>
                q
                  .eq("tenantId", args.tenantId)
                  .eq("siteId", siteId)
                  .eq("status", status),
              )
              .take(LIMITS.alerts)
        : args.severity
          ? await ctx.db
              .query("alerts")
              .withIndex("by_tenant_status_and_severity", (q) =>
                q
                  .eq("tenantId", args.tenantId)
                  .eq("status", status)
                  .eq("severity", args.severity!),
              )
              .take(LIMITS.alerts)
          : await ctx.db
              .query("alerts")
              .withIndex("by_tenant_and_status", (q) =>
                q.eq("tenantId", args.tenantId).eq("status", status),
              )
              .take(LIMITS.alerts);
      rows.push(...page);
    }

    rows.sort((left, right) => right.openedAt - left.openedAt);
    return rows.slice(0, LIMITS.alerts).map((row) => ({
      title: row.title,
      severity: row.severity,
      status: row.status,
      site: row.siteName,
      assetName: row.assetName,
      message: row.message,
      openedAt: row.openedAt,
      example: row.example,
    }));
  },
});

export const findAssets = internalQuery({
  args: { tenantId: v.id("tenants"), name: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("assets")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.name),
      )
      .take(LIMITS.assetMatches);
    return rows.map((row) => ({
      name: row.name,
      site: row.siteName,
      kind: row.kind,
      status: row.status,
      detail: row.detail,
      example: row.example,
    }));
  },
});

export const siteHealth = internalQuery({
  args: { tenantId: v.id("tenants"), siteName: v.string() },
  handler: async (ctx, args) => {
    const site = await ctx.db
      .query("sites")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.siteName),
      )
      .unique();
    if (!site) return null;

    const assets = await ctx.db
      .query("assets")
      .withIndex("by_tenant_and_site", (q) =>
        q.eq("tenantId", args.tenantId).eq("siteId", site._id),
      )
      .take(LIMITS.assets + 1);
    const assetPage = assets.slice(0, LIMITS.assets);
    const alerts = await ctx.db
      .query("alerts")
      .withIndex("by_tenant_site_and_status", (q) =>
        q.eq("tenantId", args.tenantId).eq("siteId", site._id).eq("status", "open"),
      )
      .take(LIMITS.alerts + 1);

    return {
      site: site.name,
      region: site.region,
      criticality: site.criticality,
      example: site.example,
      assets: {
        ...rollupStatuses(assetPage.map((row) => row.status)),
        scanned: assetPage.length,
        truncated: assets.length > LIMITS.assets,
      },
      openAlerts: Math.min(alerts.length, LIMITS.alerts),
      openAlertsTruncated: alerts.length > LIMITS.alerts,
    };
  },
});

export const listThreads = internalQuery({
  args: { tenantId: v.id("tenants"), operatorId: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("threads")
      .withIndex("by_tenant_operator_activity", (q) =>
        q.eq("tenantId", args.tenantId).eq("operatorId", args.operatorId),
      )
      .order("desc")
      .take(LIMITS.threads);
    return rows.map((row) => ({
      threadId: row.threadId,
      title: row.title,
      lastActivityAt: row.lastActivityAt,
    }));
  },
});
