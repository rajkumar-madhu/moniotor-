export const ASSET_STATUSES = ["up", "down", "degraded", "unknown"] as const;

export type AssetStatus = (typeof ASSET_STATUSES)[number];

export type StatusCounts = Record<AssetStatus, number>;

export function emptyStatusCounts(): StatusCounts {
  return { up: 0, down: 0, degraded: 0, unknown: 0 };
}

export function rollupStatuses(statuses: readonly string[]): StatusCounts {
  const counts = emptyStatusCounts();
  for (const status of statuses) {
    if (status === "up" || status === "down" || status === "degraded" || status === "unknown") {
      counts[status] += 1;
    }
  }
  return counts;
}
