import { listUIMessages, syncStreams, vStreamArgs } from "@convex-dev/agent";
import { paginationOptsValidator } from "convex/server";
import { v } from "convex/values";
import { components } from "./_generated/api";
import { query } from "./_generated/server";
import { safeEqual } from "./keys";

export const listMessages = query({
  args: {
    threadId: v.string(),
    accessKey: v.string(),
    paginationOpts: paginationOptsValidator,
    streamArgs: vStreamArgs,
  },
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("threads")
      .withIndex("by_threadId", (q) => q.eq("threadId", args.threadId))
      .unique();
    if (!row || !safeEqual(row.accessKey, args.accessKey)) {
      throw new Error("Unauthorized");
    }
    const paginated = await listUIMessages(ctx, components.agent, {
      threadId: args.threadId,
      paginationOpts: args.paginationOpts,
    });
    const streams = await syncStreams(ctx, components.agent, {
      threadId: args.threadId,
      streamArgs: args.streamArgs,
    });
    return { ...paginated, streams };
  },
});
