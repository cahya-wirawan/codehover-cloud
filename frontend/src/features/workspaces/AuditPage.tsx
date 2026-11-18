import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";

import { Button } from "../../components/ui/button";
import { Select } from "../../components/ui/input";
import { EmptyState, PageHeader } from "../../components/ui/section";
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
  const events = log.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <section aria-labelledby="audit-title" className="max-w-5xl">
      <PageHeader
        title={<span id="audit-title">Audit log</span>}
        description="Changes to members, connections, builds, publications, exports and deletion, newest first."
        actions={
          <label className="flex items-center gap-2 text-[13px]">
            Show
            <Select value={action} onChange={(e) => setAction(e.target.value)} className="w-48">
              {FILTERS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </label>
        }
      />
      {denied ? <p className="text-muted">Only workspace admins can read the audit log.</p> : null}
      {events.length ? (
        <div className="panel overflow-x-auto">
          <table className="w-full text-left">
            <caption className="sr-only">Audit events</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted">
                <th scope="col" className="px-4 py-2.5 font-medium">When</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Who</th>
                <th scope="col" className="px-4 py-2.5 font-medium">What</th>
                <th scope="col" className="px-4 py-2.5 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {events.map((e) => (
                <tr key={e.id} className="align-top">
                  <td className="px-4 py-3 whitespace-nowrap text-muted">{new Date(e.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 whitespace-nowrap">{e.actor_name ?? "system"}</td>
                  <td className="px-4 py-3 font-mono text-[13px] whitespace-nowrap">{e.action}</td>
                  <td className="px-4 py-3">
                    <dl className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
                      {Object.entries(e.metadata).map(([key, value]) => (
                        <div key={key} className="flex min-w-0 gap-1.5">
                          <dt className="text-muted">{key.replaceAll("_", " ")}</dt>
                          <dd className="min-w-0 truncate font-mono">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                        </div>
                      ))}
                    </dl>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : log.data && !denied ? (
        <EmptyState title="No events yet" />
      ) : null}
      {log.hasNextPage ? (
        <Button className="mt-4" onClick={() => void log.fetchNextPage()}>
          Older events
        </Button>
      ) : null}
    </section>
  );
}
