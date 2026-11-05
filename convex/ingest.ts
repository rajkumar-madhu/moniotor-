import { v } from "convex/values";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { action, type ActionCtx } from "./_generated/server";
import { assertAgentToken, boundedText, LIMITS, slugCode, tenantKey } from "./keys";
import { monitoringRag } from "./rag";
import {
  vAlertStatus,
  vAssetKind,
  vAssetStatus,
  vCriticality,
  vSeverity,
} from "./schema";

type TenantRow = { _id: Id<"tenants">; key: string; name: string };

async function requireTenant(ctx: ActionCtx, key: string): Promise<TenantRow> {
  const tenant: TenantRow | null = await ctx.runQuery(internal.monitoring.getTenantByKey, {
    key: tenantKey(key),
  });
  if (!tenant) throw new Error("Tenant not found");
  return tenant;
}

export const upsertTenant = action({
  args: { token: v.string(), key: v.string(), name: v.string() },
  handler: async (ctx, args): Promise<{ tenantId: Id<"tenants">; created: boolean }> => {
    assertAgentToken(args.token);
    return await ctx.runMutation(internal.store.upsertTenant, {
      key: tenantKey(args.key),
      name: boundedText(args.name, "Tenant name", LIMITS.nameChars),
    });
  },
});

export const upsertSite = action({
  args: {
    token: v.string(),
    tenantKey: v.string(),
    name: v.string(),
    code: v.optional(v.string()),
    region: v.optional(v.string()),
    criticality: v.optional(vCriticality),
    example: v.optional(v.boolean()),
  },
  handler: async (ctx, args): Promise<Id<"sites">> => {
    assertAgentToken(args.token);
    const tenant = await requireTenant(ctx, args.tenantKey);
    const name = boundedText(args.name, "Site name", LIMITS.nameChars);
    const code = args.code?.trim()
      ? boundedText(args.code, "Site code", 60)
      : slugCode(name);
    return await ctx.runMutation(internal.store.upsertSite, {
      tenantId: tenant._id,
      name,
      code,
      region: (args.region ?? "").trim().slice(0, LIMITS.nameChars),
      criticality: args.criticality ?? "standard",
      example: args.example ?? false,
    });
  },
});

export const upsertAsset = action({
  args: {
    token: v.string(),
    tenantKey: v.string(),
    siteName: v.string(),
    name: v.string(),
    kind: vAssetKind,
    status: vAssetStatus,
    detail: v.optional(v.string()),
    example: v.optional(v.boolean()),
  },
  handler: async (ctx, args): Promise<Id<"assets">> => {
    assertAgentToken(args.token);
    const tenant = await requireTenant(ctx, args.tenantKey);
    return await ctx.runMutation(internal.store.upsertAsset, {
      tenantId: tenant._id,
      siteName: boundedText(args.siteName, "Site name", LIMITS.nameChars),
      name: boundedText(args.name, "Asset name", LIMITS.nameChars),
      kind: args.kind,
      status: args.status,
      detail: (args.detail ?? "").trim().slice(0, LIMITS.detailChars),
      example: args.example ?? false,
    });
  },
});

export const upsertAlert = action({
  args: {
    token: v.string(),
    tenantKey: v.string(),
    siteName: v.string(),
    assetName: v.optional(v.string()),
    fingerprint: v.string(),
    severity: vSeverity,
    status: vAlertStatus,
    title: v.string(),
    message: v.optional(v.string()),
    openedAt: v.optional(v.number()),
    example: v.optional(v.boolean()),
  },
  handler: async (ctx, args): Promise<Id<"alerts">> => {
    assertAgentToken(args.token);
    const tenant = await requireTenant(ctx, args.tenantKey);
    return await ctx.runMutation(internal.store.upsertAlert, {
      tenantId: tenant._id,
      siteName: boundedText(args.siteName, "Site name", LIMITS.nameChars),
      assetName: (args.assetName ?? "").trim().slice(0, LIMITS.nameChars),
      fingerprint: boundedText(args.fingerprint, "Fingerprint", 200),
      severity: args.severity,
      status: args.status,
      title: boundedText(args.title, "Title", LIMITS.titleChars),
      message: (args.message ?? "").trim().slice(0, LIMITS.messageChars),
      openedAt: args.openedAt ?? Date.now(),
      example: args.example ?? false,
    });
  },
});

export const addRunbook = action({
  args: {
    token: v.string(),
    tenantKey: v.string(),
    key: v.string(),
    title: v.string(),
    body: v.string(),
    siteName: v.optional(v.string()),
    importance: v.optional(v.number()),
  },
  handler: async (ctx, args): Promise<{ entryId: string; status: string; created: boolean }> => {
    assertAgentToken(args.token);
    const tenant = await requireTenant(ctx, args.tenantKey);
    const site = (args.siteName ?? "all").trim().slice(0, LIMITS.nameChars) || "all";
    const saved = await monitoringRag().add(ctx, {
      namespace: tenant.key,
      key: boundedText(args.key, "Runbook key", 200),
      title: boundedText(args.title, "Title", LIMITS.titleChars),
      text: boundedText(args.body, "Runbook", LIMITS.runbookChars),
      importance: Math.min(1, Math.max(0, args.importance ?? 0.5)),
      filterValues: [{ name: "site", value: site }],
    });
    return { entryId: saved.entryId, status: saved.status, created: saved.created };
  },
});
