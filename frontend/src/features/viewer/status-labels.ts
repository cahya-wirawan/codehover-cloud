export const STATE_LABEL: Record<string, string> = {
  pending: "Pending",
  leased: "Running",
  succeeded: "Done",
  skipped: "Reused",
  retry_wait: "Waiting to retry",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
  budget_exhausted: "Stopped: spending cap reached",
};

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state;
}
