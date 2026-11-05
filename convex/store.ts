import { v } from "convex/values";
import { internalMutation } from "./_generated/server";
import {
  vAlertStatus,
  vAssetKind,
  vAssetStatus,
  vCriticality,
  vSeverity,
} from "./schema";

export const upsertTenant = internalMutation({
  args: { key: v.string(), name: v.string() },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("tenants")
      .withIndex("by_key", (q) => q.eq("key", args.key))
      .unique();
    if (existing) {
      await ctx.db.patch(existing._id, { name: args.name });
      return { tenantId: existing._id, created: false };
    }
    const tenantId = await ctx.db.insert("tenants", {
      key: args.key,
      name: args.name,
    });
    return { tenantId, created: true };
  },
});

export const upsertSite = internalMutation({
  args: {
    tenantId: v.id("tenants"),
    name: v.string(),
    code: v.string(),
    region: v.string(),
    criticality: vCriticality,
    example: v.boolean(),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("sites")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.name),
      )
      .unique();
    const fields = {
      code: args.code,
      region: args.region,
      criticality: args.criticality,
      example: args.example,
    };
    if (existing) {
      await ctx.db.patch(existing._id, fields);
      return existing._id;
    }
    return await ctx.db.insert("sites", {
      tenantId: args.tenantId,
      name: args.name,
      ...fields,
    });
  },
});

export const upsertAsset = internalMutation({
  args: {
    tenantId: v.id("tenants"),
    siteName: v.string(),
    name: v.string(),
    kind: vAssetKind,
    status: vAssetStatus,
    detail: v.string(),
    example: v.boolean(),
  },
  handler: async (ctx, args) => {
    const site = await ctx.db
      .query("sites")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.siteName),
      )
      .unique();
    if (!site) throw new Error("Site not found");

    const matches = await ctx.db
      .query("assets")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.name),
      )
      .take(8);
    const existing = matches.find((row) => row.siteId === site._id);
    const fields = {
      siteId: site._id,
      siteName: site.name,
      kind: args.kind,
      status: args.status,
      detail: args.detail,
      example: args.example,
    };
    if (existing) {
      await ctx.db.patch(existing._id, fields);
      return existing._id;
    }
    return await ctx.db.insert("assets", {
      tenantId: args.tenantId,
      name: args.name,
      ...fields,
    });
  },
});

export const upsertAlert = internalMutation({
  args: {
    tenantId: v.id("tenants"),
    siteName: v.string(),
    assetName: v.string(),
    fingerprint: v.string(),
    severity: vSeverity,
    status: vAlertStatus,
    title: v.string(),
    message: v.string(),
    openedAt: v.number(),
    example: v.boolean(),
  },
  handler: async (ctx, args) => {
    const site = await ctx.db
      .query("sites")
      .withIndex("by_tenant_and_name", (q) =>
        q.eq("tenantId", args.tenantId).eq("name", args.siteName),
      )
      .unique();
    if (!site) throw new Error("Site not found");
    const existing = await ctx.db
      .query("alerts")
      .withIndex("by_tenant_and_fingerprint", (q) =>
        q.eq("tenantId", args.tenantId).eq("fingerprint", args.fingerprint),
      )
      .unique();
    const fields = {
      siteId: site._id,
      siteName: site.name,
      assetName: args.assetName,
      severity: args.severity,
      status: args.status,
      title: args.title,
      message: args.message,
      openedAt: args.openedAt,
      example: args.example,
    };
    if (existing) {
      await ctx.db.patch(existing._id, fields);
      return existing._id;
    }
    return await ctx.db.insert("alerts", {
      tenantId: args.tenantId,
      fingerprint: args.fingerprint,
      ...fields,
    });
  },
});

export const recordThread = internalMutation({
  args: {
    threadId: v.string(),
    tenantId: v.id("tenants"),
    operatorId: v.string(),
    accessKey: v.string(),
    title: v.string(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("threads", {
      ...args,
      lastActivityAt: Date.now(),
    });
  },
});

export const touchThread = internalMutation({
  args: { threadId: v.string(), title: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("threads")
      .withIndex("by_threadId", (q) => q.eq("threadId", args.threadId))
      .unique();
    if (!row) return;
    await ctx.db.patch(row._id, {
      lastActivityAt: Date.now(),
      ...(args.title ? { title: args.title } : {}),
    });
  },
});
