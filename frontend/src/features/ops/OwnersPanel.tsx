import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type Entitlement, type OpsOwner } from "../../lib/api/resources";

function CreditForm({ owner }: { owner: OpsOwner }) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<Entitlement | null>(null);
  const credit = useMutation({
    mutationFn: () => api.opsCreditOwner(owner.namespace_id, reason),
    onSuccess: async (grant) => {
      setDone(grant);
      setReason("");
      await queryClient.invalidateQueries({ queryKey: ["ops"] });
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    credit.mutate();
  };
  return (
    <div className="mt-2">
      <form className="flex flex-wrap items-end gap-2" onSubmit={submit}>
        <label className="flex flex-col gap-1">
          Reason for crediting {owner.handle}
          <input required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} className="field" />
        </label>
        <button type="submit" disabled={credit.isPending} className="btn">
          Credit this month&apos;s spending
        </button>
      </form>
      {done ? (
        <p role="status" className="mt-2 text-sm">
          Credited {formatUsd(done.allowance_micro_usd)} until {new Date(done.expires_at).toLocaleDateString()}, through
          workspace <span className="font-mono">{done.workspace_id}</span>.
        </p>
      ) : null}
      {credit.error ? (
        <p role="alert" className="mt-2 text-destructive">
          {credit.error instanceof ApiError ? credit.error.message : "Could not credit."}
        </p>
      ) : null}
    </div>
  );
}

/** Owners (0044): people and organizations with this month's allowance and spending across their
 * workspaces. A credit hands back what was settled this month as a grant until the month ends; the
 * ledger keeps the real spending. */
export function OwnersPanel() {
  const [handle, setHandle] = useState("");
  const [filter, setFilter] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;
  const list = useQuery({
    queryKey: ["ops", "owners", filter, cursor],
    queryFn: () => api.opsOwners({ handle: filter, cursor }),
    retry: false,
  });
  const [crediting, setCrediting] = useState<string | null>(null);
  const search = (event: FormEvent) => {
    event.preventDefault();
    setCursors([null]);
    setFilter(handle.trim());
  };
  const rows = list.data?.items;
  const chosen = rows?.find((o) => o.namespace_id === crediting);

  return (
    <section aria-labelledby="owners-title" className="panel p-4">
      <h2 id="owners-title" className="section-title">Owners</h2>
      <p className="mt-1 text-sm text-muted">
        People and organizations with their plan allowance this month (the plan or the default, plus grants) and what their
        workspaces used.
      </p>
      <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={search}>
        <label className="flex flex-col gap-1">
          Handle starts with
          <input value={handle} onChange={(e) => setHandle(e.target.value)} maxLength={40} className="field" />
        </label>
        <button type="submit" className="btn">Search</button>
      </form>
      {list.error ? <p role="alert" className="mt-2 text-destructive">Could not load owners.</p> : null}
      {rows?.length ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Owners</caption>
            <thead>
              <tr className="text-left text-muted">
                <th scope="col">Handle</th><th scope="col">Plan</th>
                <th scope="col" className="text-right">Workspaces</th><th scope="col" className="text-right">Projects</th>
                <th scope="col" className="text-right">Sites</th><th scope="col" className="text-right">Builds</th>
                <th scope="col" className="text-right">Used</th><th scope="col" className="text-right">Allowance</th>
                <th scope="col"><span className="sr-only">Credit</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.namespace_id}>
                  <td>
                    {o.handle}
                    {o.kind === "organization" ? " (organization)" : ""}
                    {o.status !== "active" ? <strong> · {o.status === "disabled" ? "suspended" : o.status}</strong> : null}
                  </td>
                  <td>{o.plan_key ?? "default"}</td>
                  <td className="text-right font-mono">{o.workspaces}</td>
                  <td className="text-right font-mono">{o.projects}</td>
                  <td className="text-right font-mono">{o.published_sites}</td>
                  <td className="text-right font-mono">{o.builds_this_month}</td>
                  <td className="text-right font-mono">
                    {formatUsd(o.settled_micro_usd)}
                    {o.reserved_micro_usd ? <span className="block text-xs text-muted">{formatUsd(o.reserved_micro_usd)} held</span> : null}
                  </td>
                  <td className="text-right font-mono">
                    {formatUsd(o.allowance_micro_usd)}
                    {o.granted_micro_usd ? <span className="block text-xs text-muted">{formatUsd(o.granted_micro_usd)} granted</span> : null}
                  </td>
                  <td className="text-right">
                    <button type="button" className="btn" onClick={() => setCrediting(o.namespace_id)} aria-label={`Credit ${o.handle}`}>
                      Credit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : rows ? (
        <p className="mt-3 text-sm text-muted">No owner matches.</p>
      ) : null}
      {cursors.length > 1 || list.data?.next_cursor ? (
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn" disabled={cursors.length === 1} onClick={() => setCursors(cursors.slice(0, -1))}>
            Previous
          </button>
          <button
            type="button"
            className="btn"
            disabled={!list.data?.next_cursor}
            onClick={() => list.data?.next_cursor && setCursors([...cursors, list.data.next_cursor])}
          >
            Next
          </button>
        </div>
      ) : null}
      {chosen ? <CreditForm key={chosen.namespace_id} owner={chosen} /> : null}
    </section>
  );
}
