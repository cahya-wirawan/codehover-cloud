import { CircleCheck, CircleDashed, CircleSlash, CircleX, Clock, Loader2, Repeat } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { STATUS_LABEL } from "./status-labels";

/** A build's status: icon plus words, never colour alone (17). */
export function StatusBadge({ status }: { status: string }) {
  const label = STATUS_LABEL[status] ?? status;
  if (status === "ready") return <Badge tone="success" icon={<CircleCheck aria-hidden="true" />}>{label}</Badge>;
  if (status === "running") return <Badge tone="primary" icon={<Loader2 aria-hidden="true" className="motion-safe:animate-spin" />}>{label}</Badge>;
  if (status === "queued") return <Badge icon={<Clock aria-hidden="true" />}>{label}</Badge>;
  if (status === "cancelled") return <Badge icon={<CircleSlash aria-hidden="true" />}>{label}</Badge>;
  return <Badge tone="destructive" icon={<CircleX aria-hidden="true" />}>{label}</Badge>;
}

export function StageIcon({ state }: { state: string }) {
  const common = "size-4 shrink-0";
  if (state === "succeeded") return <CircleCheck aria-hidden="true" className={`${common} text-success`} />;
  if (state === "leased") return <Loader2 aria-hidden="true" className={`${common} text-primary motion-safe:animate-spin`} />;
  if (state === "retry_wait") return <Clock aria-hidden="true" className={`${common} text-warning`} />;
  if (state === "failed") return <CircleX aria-hidden="true" className={`${common} text-destructive`} />;
  if (state === "cancelled") return <CircleSlash aria-hidden="true" className={`${common} text-muted`} />;
  if (state === "skipped") return <Repeat aria-hidden="true" className={`${common} text-muted`} />;
  return <CircleDashed aria-hidden="true" className={`${common} text-muted`} />;
}
