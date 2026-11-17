import { apiGet } from "./client";

export interface LiveStatus {
  status: "ok";
  version: string;
}

export function parseLiveStatus(value: unknown): LiveStatus {
  if (
    typeof value === "object" &&
    value !== null &&
    (value as LiveStatus).status === "ok" &&
    typeof (value as LiveStatus).version === "string"
  ) {
    return { status: "ok", version: (value as LiveStatus).version };
  }
  throw new Error("Unexpected health response");
}

export const healthKeys = { live: ["health", "live"] as const };

export function fetchLiveStatus(signal?: AbortSignal): Promise<LiveStatus> {
  return apiGet("/health/live", parseLiveStatus, signal);
}
