import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

/** Support access (08): admins let platform operators read this workspace, read-only, for a few hours. */
export function SupportAccessPanel({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const grants = useQuery({ queryKey: ["support", workspaceId], queryFn: () => api.supportGrants(workspaceId) });
  const [hours, setHours] = useState("4");
  const [reason, setReason] = useState("");
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["support", workspaceId] });
  const grant = useMutation({
    mutationFn: () => api.grantSupport(workspaceId, Number(hours), reason),
    onSuccess: async () => {
      setReason("");
      await refresh();
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => api.revokeSupport(workspaceId, id), onSuccess: refresh });
  const active = grants.data?.items.filter((g) => g.active) ?? [];

  return (
    <section aria-labelledby="support-title" className="mt-8 rounded-lg border border-border bg-surface p-4">
      <h2 id="support-title" className="text-lg font-medium">Support access</h2>
      <p className="mt-1">
        Lets the codehover operators read this workspace's projects, builds and sites to help you. They cannot change
        anything, access ends automatically, and every reading session appears in the audit log.
      </p>
      {active.length ? (
        <ul className="mt-2 space-y-1">
          {active.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center gap-2">
              <span role="status">
                Active until {new Date(g.expires_at).toLocaleString()}: {g.reason}
                {g.granted_by_name ? ` (granted by ${g.granted_by_name})` : ""}
              </span>
              <button type="button" onClick={() => revoke.mutate(g.id)} disabled={revoke.isPending}
                className="h-8 rounded-lg border border-input-border px-3">End access now</button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-muted">No support access.</p>
      )}
      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); grant.mutate(); }}>
        <label className="flex flex-col gap-1">
          Hours
          <select value={hours} onChange={(e) => setHours(e.target.value)} className="h-9 rounded-lg border border-input-border bg-surface px-2">
            {["1", "4", "24", "72"].map((h) => <option key={h} value={h}>{h}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          What should they look at?
          <input required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)}
            className="h-9 w-72 rounded-lg border border-input-border bg-surface px-2" />
        </label>
        <button type="submit" disabled={grant.isPending} className="h-9 rounded-lg border border-input-border px-3 font-medium">
          Grant support access
        </button>
      </form>
      {grant.error ? (
        <p role="alert" className="mt-2 text-destructive">{grant.error instanceof ApiError ? grant.error.message : "Could not grant access."}</p>
      ) : null}
    </section>
  );
}
