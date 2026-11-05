import { Agent, createTool, stepCountIs } from "@convex-dev/agent";
import { z } from "zod";
import { components, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { LIMITS } from "./keys";
import { chatModel, textEmbeddingModel } from "./models";
import { monitoringRag } from "./rag";

const INSTRUCTIONS = `You are the LinkedEye operator assistant for one tenant of a multi-tenant infrastructure monitoring platform.
Answer only about this tenant. Use tools for live asset status, open alerts, site health, and runbooks.
Report only what the tools return. If a tool returns no rows, say the monitoring snapshot has no matching records.
Do not invent hosts, latencies, or counts.
Severity words are critical, warning, and info. Asset states are up, down, degraded, and unknown.
When a row has example set to true, say it is sample data.
Keep answers short and name the site when the tool result includes one.`;

export function monitoringAgent(tenantId: Id<"tenants">, tenantKey: string) {
  return new Agent(components.agent, {
    name: "LinkedEye",
    languageModel: chatModel(),
    embeddingModel: textEmbeddingModel(),
    instructions: INSTRUCTIONS,
    stopWhen: stepCountIs(5),
    tools: {
      listOpenAlerts: createTool({
        description:
          "List open and acknowledged alerts for this tenant. Optionally limit to one site or severity.",
        inputSchema: z.object({
          siteName: z.string().optional().describe("Exact site name"),
          severity: z
            .enum(["critical", "warning", "info"])
            .optional()
            .describe("Alert severity"),
        }),
        execute: async (ctx, input): Promise<string> => {
          const rows = await ctx.runQuery(internal.monitoring.listOpenAlerts, {
            tenantId,
            siteName: input.siteName,
            severity: input.severity,
          });
          return JSON.stringify(rows);
        },
      }),
      siteHealth: createTool({
        description:
          "Summarize asset states and the open-alert count for one site in this tenant.",
        inputSchema: z.object({
          siteName: z.string().describe("Exact site name"),
        }),
        execute: async (ctx, input): Promise<string> => {
          const health = await ctx.runQuery(internal.monitoring.siteHealth, {
            tenantId,
            siteName: input.siteName,
          });
          return JSON.stringify(health ?? { error: "Site not found" });
        },
      }),
      getAsset: createTool({
        description: "Look up an asset by exact name inside this tenant.",
        inputSchema: z.object({
          name: z.string().describe("Exact asset name"),
        }),
        execute: async (ctx, input): Promise<string> => {
          const matches = await ctx.runQuery(internal.monitoring.findAssets, {
            tenantId,
            name: input.name,
          });
          return JSON.stringify(matches);
        },
      }),
      searchRunbooks: createTool({
        description:
          "Search this tenant's runbooks and return the closest passages. Use this before giving a procedure.",
        inputSchema: z.object({
          query: z.string().describe("What the operator needs to do or understand"),
          siteName: z
            .string()
            .optional()
            .describe("Limit to runbooks tagged for this site, or omit to search the tenant"),
        }),
        execute: async (ctx, input): Promise<string> => {
          try {
            const found = await monitoringRag().search(ctx, {
              namespace: tenantKey,
              query: input.query,
              limit: 4,
              chunkContext: { before: 1, after: 1 },
              filters: input.siteName
                ? [{ name: "site", value: input.siteName }]
                : undefined,
            });
            const text = found.text.trim().slice(0, LIMITS.excerptChars);
            return text || "No matching runbooks.";
          } catch (error) {
            console.error("runbook search failed", error instanceof Error ? error.name : "error");
            return "Runbook search is unavailable. Answer from the monitoring tools only.";
          }
        },
      }),
    },
  });
}
