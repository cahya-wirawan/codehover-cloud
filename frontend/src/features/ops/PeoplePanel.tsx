import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type OpsPerson } from "../../lib/api/resources";

function when(value: string | null): string {
  return value ? new Date(value).toLocaleDateString(undefined, { dateStyle: "medium" }) : "never";
}

function failure(error: unknown, fallback: string) {
  return error ? (
    <p role="alert" className="mt-2 text-destructive">{error instanceof ApiError ? error.message : fallback}</p>
  ) : null;
}

function PersonDetail({ userId, onChanged }: { userId: string; onChanged: () => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ["ops", "person", userId], queryFn: () => api.opsPerson(userId), retry: false });
  const [reason, setReason] = useState("");
  const [cancelled, setCancelled] = useState<number | null>(null);
  const change = useMutation({
    mutationFn: async (suspend: boolean) => {
      if (suspend) {
        const next = await api.opsSuspendPerson(userId, reason);
        setCancelled(next.cancelled_builds);
      } else {
        await api.opsReinstatePerson(userId, reason);
        setCancelled(null);
      }
    },
    onSuccess: async () => {
      setReason("");
      await queryClient.invalidateQueries({ queryKey: ["ops"] });
      onChanged();
    },
  });
  const person = detail.data;
  if (!person) return detail.error ? failure(detail.error, "Person not found.") : <p className="mt-3 text-muted">Loading…</p>;
  const suspended = person.status === "disabled";
  const submit = (event: FormEvent) => {
    event.preventDefault();
    change.mutate(!suspended);
  };
  return (
    <div className="mt-4 rounded-[var(--radius-overlay)] border border-border p-4">
      <h3 className="font-medium">
        {person.handle ?? "No handle"} <span className="font-mono text-xs text-muted">{person.user_id}</span>
      </h3>
      <p className="mt-1 text-sm">
        {suspended ? <strong>Suspended since {when(person.disabled_at)}</strong> : "Active"}
        {person.operator ? " · operator" : ""} · joined {when(person.created_at)} · last active {when(person.last_active_at)}
      </p>
      <p className="mt-1 text-sm text-muted">
        Signs in with {person.sign_in.join(" and ") || "nothing"} · {person.live_sessions} live sessions · {person.live_tokens} live
        access tokens
      </p>
      {person.workspaces.length ? (
        <table className="mt-3 w-full text-sm">
          <caption className="sr-only">Workspaces of this person</caption>
          <thead>
            <tr className="text-left text-muted">
              <th scope="col">Workspace</th><th scope="col">Owner</th><th scope="col">Role</th>
              <th scope="col" className="text-right">Builds</th><th scope="col" className="text-right">Spent this month</th>
            </tr>
          </thead>
          <tbody>
            {person.workspaces.map((w) => (
              <tr key={w.workspace_id}>
                <td className="font-mono">{w.workspace_id}</td>
                <td>{w.owner_handle ?? ""}{w.owner_kind === "organization" ? " (organization)" : ""}</td>
                <td>{w.role}{w.via_organization ? " via organization" : ""}</td>
                <td className="text-right font-mono">{w.builds_this_month}</td>
                <td className="text-right font-mono">{formatUsd(w.spent_this_month_micro_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mt-3 text-sm text-muted">Not in any workspace.</p>
      )}
      {person.organizations.length ? (
        <p className="mt-2 text-sm">
          Organizations: {person.organizations.map((o) => `${o.handle ?? o.organization_id} (${o.role})`).join(", ")}
        </p>
      ) : null}
      {person.operator ? (
        <p className="mt-3 text-sm text-muted">Operators cannot be suspended here. Revoke the operator role first.</p>
      ) : (
        <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submit}>
          <label className="flex flex-col gap-1">
            Reason
            <input required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} className="field" />
          </label>
          <button type="submit" disabled={change.isPending} className="btn">
            {suspended ? "Reinstate" : "Suspend"}
          </button>
        </form>
      )}
      {!suspended && !person.operator ? (
        <p className="mt-2 text-xs text-muted">
          Suspending ends their sessions, access tokens and MCP sign-ins, cancels their running builds and stops sign-in.
          Their workspaces and sites stay up. Recorded in each of their workspaces' audit logs.
        </p>
      ) : null}
      {cancelled !== null && suspended ? <p role="status" className="mt-2 text-sm">Suspended. {cancelled} running builds cancelled.</p> : null}
      {failure(change.error, "Could not change the person.")}
    </div>
  );
}

/** People (0044): handles, states, counts and money only. An exact email finds someone without being shown. */
export function PeoplePanel() {
  const [handle, setHandle] = useState("");
  const [status, setStatus] = useState("");
  const [filters, setFilters] = useState<{ handle: string; status: string }>({ handle: "", status: "" });
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1] ?? null;
  const list = useQuery({
    queryKey: ["ops", "people", filters, cursor],
    queryFn: () => api.opsPeople({ ...filters, cursor }),
    retry: false,
  });
  const [email, setEmail] = useState("");
  const lookup = useMutation({ mutationFn: () => api.opsLookupPerson(email) });
  const [selected, setSelected] = useState<string | null>(null);
  const rows: OpsPerson[] | undefined = lookup.data ? lookup.data.items : list.data?.items;

  const search = (event: FormEvent) => {
    event.preventDefault();
    lookup.reset();
    setCursors([null]);
    setFilters({ handle: handle.trim(), status });
  };
  const find = (event: FormEvent) => {
    event.preventDefault();
    lookup.mutate();
  };

  return (
    <section aria-labelledby="people-title" className="panel p-4">
      <h2 id="people-title" className="section-title">People</h2>
      <p className="mt-1 text-sm text-muted">Handles, states, counts and spending. Names and emails are never shown.</p>
      <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={search}>
        <label className="flex flex-col gap-1">
          Handle starts with
          <input value={handle} onChange={(e) => setHandle(e.target.value)} maxLength={40} className="field" />
        </label>
        <label className="flex flex-col gap-1">
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="field">
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="disabled">Suspended</option>
          </select>
        </label>
        <button type="submit" className="btn">Search</button>
      </form>
      <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={find}>
        <label className="flex flex-col gap-1">
          Exact email
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="field w-72" />
        </label>
        <button type="submit" disabled={lookup.isPending} className="btn">Find</button>
        {lookup.data ? (
          <button type="button" className="btn" onClick={() => lookup.reset()}>Back to the list</button>
        ) : null}
      </form>
      {lookup.data ? (
        <p role="status" className="mt-2 text-sm">
          {lookup.data.items.length ? `${lookup.data.items.length} found with that email.` : "Nobody has that email."}
        </p>
      ) : null}
      {failure(lookup.error ?? list.error, "Could not load people.")}
      {rows?.length ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">People</caption>
            <thead>
              <tr className="text-left text-muted">
                <th scope="col">Handle</th><th scope="col">Status</th><th scope="col">Joined</th><th scope="col">Last active</th>
                <th scope="col" className="text-right">Workspaces</th><th scope="col" className="text-right">Builds</th>
                <th scope="col" className="text-right">Spent this month</th><th scope="col"><span className="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.user_id}>
                  <td>{p.handle ?? <span className="font-mono text-xs">{p.user_id}</span>}{p.operator ? " · operator" : ""}</td>
                  <td>{p.status === "disabled" ? <strong>Suspended</strong> : "Active"}</td>
                  <td>{when(p.created_at)}</td>
                  <td>{when(p.last_active_at)}</td>
                  <td className="text-right font-mono">{p.workspaces}</td>
                  <td className="text-right font-mono">{p.builds_this_month}</td>
                  <td className="text-right font-mono">{formatUsd(p.spent_this_month_micro_usd)}</td>
                  <td className="text-right">
                    <button type="button" className="btn" onClick={() => setSelected(p.user_id)} aria-label={`Open ${p.handle ?? p.user_id}`}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : rows ? (
        <p className="mt-3 text-sm text-muted">Nobody matches.</p>
      ) : null}
      {!lookup.data && (cursors.length > 1 || list.data?.next_cursor) ? (
        <div className="mt-2 flex gap-2">
          <button type="button" className="btn" disabled={cursors.length === 1} onClick={() => setCursors(cursors.slice(0, -1))}>
            Newer
          </button>
          <button
            type="button"
            className="btn"
            disabled={!list.data?.next_cursor}
            onClick={() => list.data?.next_cursor && setCursors([...cursors, list.data.next_cursor])}
          >
            Older
          </button>
        </div>
      ) : null}
      {selected ? <PersonDetail key={selected} userId={selected} onChanged={() => void list.refetch()} /> : null}
    </section>
  );
}
