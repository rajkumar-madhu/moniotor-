import { createThread, saveMessage } from "@convex-dev/agent";
import { v } from "convex/values";
import { components, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { action, internalAction } from "./_generated/server";
import { monitoringAgent } from "./agent";
import { assertAgentToken, boundedText, LIMITS, operatorId, randomKey, tenantKey } from "./keys";
import { monitoringRag } from "./rag";

const EXAMPLE_RUNBOOK = `Disk pressure on a monitored host.

When a host asset is down or degraded and the alert mentions disk space:
1. Confirm the asset status and the site with the monitoring tools before changing anything.
2. Check that the volume named in the alert is the volume on that host.
3. Free space by removing files the owner already marked as disposable. Leave databases and incident logs in place.
4. If the volume stays above the threshold that opened the alert, page the site operator. Record the site name, asset name, and the free-space reading the check reported.
5. Resolve the alert only after a later check shows the asset up.

This note is sample guidance for the example tenant. It is not a live procedure for a production site.`;

export const startThread = action({
  args: {
    token: v.string(),
    tenantKey: v.string(),
    operatorId: v.string(),
    title: v.optional(v.string()),
  },
  handler: async (ctx, args): Promise<{ threadId: string; accessKey: string }> => {
    assertAgentToken(args.token);
    const key = tenantKey(args.tenantKey);
    const operator = operatorId(args.operatorId);
    const tenant: { _id: Id<"tenants">; key: string; name: string } | null = await ctx.runQuery(
      internal.monitoring.getTenantByKey,
      { key },
    );
    if (!tenant) throw new Error("Tenant not found");
    const title = args.title?.trim()
      ? boundedText(args.title, "Title", LIMITS.titleChars)
      : "Monitoring question";
    const threadId = await createThread(ctx, components.agent, {
      userId: `${key}:${operator}`,
      title,
    });
    const accessKey = randomKey();
    await ctx.runMutation(internal.store.recordThread, {
      threadId,
      tenantId: tenant._id,
      operatorId: operator,
      accessKey,
      title,
    });
    return { threadId, accessKey };
  },
});

export const sendMessage = action({
  args: {
    threadId: v.string(),
    accessKey: v.string(),
    prompt: v.string(),
  },
  handler: async (ctx, args): Promise<{ messageId: string }> => {
    const prompt = boundedText(args.prompt, "Prompt", LIMITS.promptChars);
    const thread = await ctx.runQuery(internal.monitoring.getThread, {
      threadId: args.threadId,
      accessKey: args.accessKey,
    });
    if (!thread) throw new Error("Unauthorized");
    const saved = await saveMessage(ctx, components.agent, {
      threadId: args.threadId,
      prompt,
    });
    await ctx.runMutation(internal.store.touchThread, { threadId: args.threadId });
    await ctx.scheduler.runAfter(0, internal.chatActions.respond, {
      threadId: args.threadId,
      promptMessageId: saved.messageId,
    });
    return { messageId: saved.messageId };
  },
});

export const listThreads = action({
  args: { token: v.string(), tenantKey: v.string(), operatorId: v.string() },
  handler: async (
    ctx,
    args,
  ): Promise<{ threadId: string; title: string; lastActivityAt: number }[]> => {
    assertAgentToken(args.token);
    const tenant: { _id: Id<"tenants">; key: string; name: string } | null = await ctx.runQuery(
      internal.monitoring.getTenantByKey,
      { key: tenantKey(args.tenantKey) },
    );
    if (!tenant) throw new Error("Tenant not found");
    return await ctx.runQuery(internal.monitoring.listThreads, {
      tenantId: tenant._id,
      operatorId: operatorId(args.operatorId),
    });
  },
});

export const respond = internalAction({
  args: { threadId: v.string(), promptMessageId: v.string() },
  handler: async (ctx, args): Promise<void> => {
    const thread = await ctx.runQuery(internal.monitoring.getThreadById, {
      threadId: args.threadId,
    });
    if (!thread) throw new Error("Thread not found");
    const agent = monitoringAgent(thread.tenantId, thread.tenantKey);
    const result = await agent.streamText(
      ctx,
      { threadId: args.threadId, userId: `${thread.tenantKey}:${thread.operatorId}` },
      { promptMessageId: args.promptMessageId },
      { saveStreamDeltas: { chunking: "word", throttleMs: 250 } },
    );
    await result.consumeStream();
  },
});

export const seedExample = action({
  args: { token: v.string() },
  handler: async (ctx, args): Promise<{ tenantKey: string }> => {
    assertAgentToken(args.token);
    const tenant: { tenantId: Id<"tenants">; created: boolean } = await ctx.runMutation(
      internal.store.upsertTenant,
      { key: "example", name: "Example tenant" },
    );
    await ctx.runMutation(internal.store.upsertSite, {
      tenantId: tenant.tenantId,
      name: "Example DC",
      code: "example-dc",
      region: "lab",
      criticality: "standard",
      example: true,
    });
    await ctx.runMutation(internal.store.upsertAsset, {
      tenantId: tenant.tenantId,
      siteName: "Example DC",
      name: "example-host-1",
      kind: "host",
      status: "degraded",
      detail: "Sample disk check. Not a live host.",
      example: true,
    });
    await ctx.runMutation(internal.store.upsertAlert, {
      tenantId: tenant.tenantId,
      siteName: "Example DC",
      assetName: "example-host-1",
      fingerprint: "example:disk",
      severity: "warning",
      status: "open",
      title: "Sample disk warning",
      message: "Example only. The sample host reports high disk use.",
      openedAt: Date.now(),
      example: true,
    });
    await monitoringRag().add(ctx, {
      namespace: "example",
      key: "example:disk",
      title: "Sample disk runbook",
      text: EXAMPLE_RUNBOOK,
      importance: 0.4,
      filterValues: [{ name: "site", value: "Example DC" }],
    });
    return { tenantKey: "example" };
  },
});
