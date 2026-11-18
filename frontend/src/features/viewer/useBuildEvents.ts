import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { API_BASE } from "../../lib/api/client";

const EVENT_TYPES = [
  "build.queued",
  "stage.started",
  "stage.completed",
  "task.retry_scheduled",
  "usage.updated",
  "build.cancel_requested",
  "build.ready",
  "build.failed",
  "build.cancelled",
  "build.budget_exhausted",
  "reset",
  "access.revoked",
];

/**
 * Follow a build over SSE (10): every event refetches the authoritative status. EventSource reconnects
 * with Last-Event-ID by itself; `onUnavailable` fires when the stream is closed for good, so the caller
 * falls back to polling.
 */
export function useBuildEvents(buildId: string, active: boolean, onUnavailable: () => void): void {
  const client = useQueryClient();

  useEffect(() => {
    if (!active || typeof EventSource === "undefined") return undefined;
    const source = new EventSource(`${API_BASE}/builds/${encodeURIComponent(buildId)}/events`);
    const refresh = () => void client.invalidateQueries({ queryKey: ["build", buildId] });
    for (const type of EVENT_TYPES) source.addEventListener(type, refresh);
    source.addEventListener("access.revoked", () => source.close());
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) onUnavailable();
    };
    return () => source.close();
  }, [active, buildId, client, onUnavailable]);
}

export const streamingSupported = () => typeof EventSource !== "undefined";

/** Polling fallback: 5 s with jitter (10). */
export function pollInterval(): number {
  return 5000 + Math.floor(Math.random() * 1000);
}
