import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

const FILTERS: [string, string][] = [
  ["", "All events"],
  ["publication.", "Publication"],
  ["build.", "Builds"],
  ["export.", "Exports"],
  ["claims.", "Waivers"],
  ["member", "Members"],
  ["connection", "Connections"],
];

/** Workspace audit log (FR-023): admins only; newest first. */
export function AuditPage() {
  const { wid = "" } = useParams();
  const [action, setAction] = useState("");
  const log = useInfiniteQuery({
    queryKey: ["audit", wid, action],
    queryFn: ({ pageParam }) => api.audit(wid, action || null, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const denied = log.error instanceof ApiError && log.error.status === 403;
  return (
    <section aria-labelledby="audit-title" className="max-w-4xl">
      <h1 id="audit-title" className="text-2xl font-medium">Audit log</h1>
      {denied ? <p className="mt-2 text-muted">Only workspace admins can read the audit log.</p> : null}
      <label className="mt-4 flex max-w-xs flex-col gap-1">
        Show
        <select value={action} onChange={(e) => setAction(e.target.value)} className="h-9 rounded-lg border border-input-border bg-surface px-2">
          {FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <table className="mt-4 w-full border-collapse text-left">
        <thead><tr className="border-b border-border text-muted"><th className="py-2">When</th><th>Who</th><th>What</th><th>Details</th></tr></thead>
        <tbody>
          {log.data?.pages.flatMap((page) => page.items).map((e) => (
            <tr key={e.id} className="border-b border-border align-top">
              <td className="py-2 pr-3 whitespace-nowrap">{new Date(e.created_at).toLocaleString()}</td>
              <td className="pr-3">{e.actor_name ?? "system"}</td>
              <td className="pr-3 font-mono">{e.action}</td>
              <td className="font-mono text-sm text-muted">{JSON.stringify(e.metadata)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {log.hasNextPage ? (
        <button type="button" onClick={() => void log.fetchNextPage()} className="mt-3 h-9 rounded-lg border border-input-border px-3">Older events</button>
      ) : null}
    </section>
  );
}
