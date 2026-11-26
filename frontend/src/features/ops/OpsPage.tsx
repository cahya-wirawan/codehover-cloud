import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { ApiError } from "../../lib/api/client";
import { api, formatUsd, type OpsBuild } from "../../lib/api/resources";
import { OwnersPanel } from "./OwnersPanel";
import { PeoplePanel } from "./PeoplePanel";

function failure(error: unknown, fallback: string) {
  return error ? (
    <p role="alert" className="mt-2 text-destructive">{error instanceof ApiError ? error.message : fallback}</p>
  ) : null;
}

const input = "field";
const button = "btn";

/** S21 operations (02): counts, IDs and codes only. Tenant content needs that workspace's support grant. */
export function OpsPage() {
  const queryClient = useQueryClient();
  const summary = useQuery({ queryKey: ["ops", "summary"], queryFn: api.opsSummary, retry: false, refetchInterval: 15_000 });
  const events = useQuery({ queryKey: ["ops", "events"], queryFn: api.opsEvents, retry: false });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["ops"] });

  const [pauseReason, setPauseReason] = useState("");
  const pause = useMutation({
    mutationFn: () => api.opsPause(!summary.data?.builds_paused, pauseReason),
    onSuccess: async () => {
      setPauseReason("");
      await refresh();
    },
  });

  const [buildId, setBuildId] = useState("");
  const [diagnostics, setDiagnostics] = useState<OpsBuild | null>(null);
  const lookup = useMutation({ mutationFn: () => api.opsBuild(buildId.trim()), onSuccess: setDiagnostics });

  const [projectId, setProjectId] = useState("");
  const [takedownReason, setTakedownReason] = useState("");
  const takedown = useMutation({
    mutationFn: (suspend: boolean) =>
      suspend ? api.opsSuspend(projectId.trim(), takedownReason) : api.opsReinstate(projectId.trim(), takedownReason),
    onSuccess: refresh,
  });

  const [workspaceId, setWorkspaceId] = useState("");
  const [amount, setAmount] = useState("5");
  const [days, setDays] = useState("30");
  const [grantReason, setGrantReason] = useState("");
  const grants = useQuery({
    queryKey: ["ops", "grants", workspaceId.trim()],
    queryFn: () => api.opsEntitlements(workspaceId.trim()),
    enabled: /^[0-9a-f-]{36}$/.test(workspaceId.trim()),
    retry: false,
  });
  const grant = useMutation({
    mutationFn: () =>
      api.opsGrant(workspaceId.trim(), {
        allowance_micro_usd: Math.round(Number(amount) * 1_000_000),
        days: Number(days),
        reason: grantReason,
      }),
    onSuccess: refresh,
  });
  const revoke = useMutation({ mutationFn: (id: string) => api.opsRevokeGrant(id, "revoked from the operations view"), onSuccess: refresh });

  if (summary.error) {
    return (
      <section aria-labelledby="ops-title">
        <h1 id="ops-title" className="text-[22px] leading-8 font-semibold">Not found</h1>
      </section>
    );
  }
  const data = summary.data;
  const submit = (action: () => void) => (event: FormEvent) => {
    event.preventDefault();
    action();
  };

  return (
    <section aria-labelledby="ops-title" className="max-w-4xl space-y-6">
      <h1 id="ops-title" className="text-[22px] leading-8 font-semibold">Operations</h1>
      <p className="text-muted">Identifiers, handles, states and counts only. Reading a workspace needs its admins' support grant.</p>

      <section aria-labelledby="queue-title" className="panel p-4">
        <h2 id="queue-title" className="section-title">Job system</h2>
        {data ? (
          <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
            {Object.entries(data.queue).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-2">
                <dt className="text-muted">{key.replaceAll("_", " ")}</dt>
                <dd className="font-mono">{Math.round(value)}</dd>
              </div>
            ))}
            {Object.entries(data.builds_last_24h).map(([status, count]) => (
              <div key={status} className="flex justify-between gap-2">
                <dt className="text-muted">builds {status} (24 h)</dt>
                <dd className="font-mono">{count}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p role="status" className="mt-2 text-muted">Loading…</p>
        )}
      </section>

      <section aria-labelledby="pause-title" className="panel p-4">
        <h2 id="pause-title" className="section-title">New builds</h2>
        <p className="mt-1" role="status">
          {data?.builds_paused ? <strong>Paused: {data.paused_reason}</strong> : "Accepting new builds."} Reading always works.
        </p>
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={submit(() => pause.mutate())}>
          <label className="flex flex-col gap-1">
            Reason
            <input required minLength={3} maxLength={500} value={pauseReason} onChange={(e) => setPauseReason(e.target.value)} className={input} />
          </label>
          <button type="submit" disabled={pause.isPending || !data} className={button}>
            {data?.builds_paused ? "Resume new builds" : "Pause new builds"}
          </button>
        </form>
        {failure(pause.error, "Could not change the switch.")}
      </section>

      <section aria-labelledby="build-title" className="panel p-4">
        <h2 id="build-title" className="section-title">Build diagnostics</h2>
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={submit(() => lookup.mutate())}>
          <label className="flex flex-col gap-1">
            Build ID
            <input required value={buildId} onChange={(e) => setBuildId(e.target.value)} className={`${input} w-80 font-mono`} />
          </label>
          <button type="submit" disabled={lookup.isPending} className={button}>Look up</button>
        </form>
        {failure(lookup.error, "Build not found.")}
        {diagnostics ? (
          <div className="mt-3 overflow-x-auto">
            <p>
              {diagnostics.status} · workspace <span className="font-mono">{diagnostics.workspace_id}</span> · spent{" "}
              {formatUsd(diagnostics.settled_micro_usd)} of {formatUsd(diagnostics.cap_micro_usd)}
              {diagnostics.reserved_micro_usd ? `, ${formatUsd(diagnostics.reserved_micro_usd)} held` : ""} · {diagnostics.events} events
            </p>
            <table className="mt-2 w-full text-sm">
              <caption className="sr-only">Tasks</caption>
              <thead>
                <tr className="text-left text-muted">
                  <th scope="col">Stage</th><th scope="col">State</th><th scope="col">Attempts</th><th scope="col">Error</th><th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {diagnostics.tasks.map((t) => (
                  <tr key={t.id}>
                    <td>{t.stage}</td><td>{t.state}</td><td>{t.attempts}</td><td className="font-mono">{t.error_code ?? ""}</td>
                    <td>{new Date(t.updated_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul className="mt-2 text-sm">
              {diagnostics.provider_attempts.map((a) => (
                <li key={`${a.purpose}-${a.state}`}>
                  {a.purpose} {a.state}: {a.count} ({formatUsd(a.settled_micro_usd)}){a.error_codes.length ? ` · ${a.error_codes.join(", ")}` : ""}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <PeoplePanel />

      <OwnersPanel />

      <section aria-labelledby="takedown-title" className="panel p-4">
        <h2 id="takedown-title" className="section-title">Site takedown</h2>
        <p className="mt-1 text-sm text-muted">Stops the project's site at once; its admins cannot lift or unpublish it. Recorded in the workspace's audit log.</p>
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={submit(() => takedown.mutate(true))}>
          <label className="flex flex-col gap-1">
            Project ID
            <input required value={projectId} onChange={(e) => setProjectId(e.target.value)} className={`${input} w-80 font-mono`} />
          </label>
          <label className="flex flex-col gap-1">
            Reason
            <input required minLength={3} maxLength={500} value={takedownReason} onChange={(e) => setTakedownReason(e.target.value)} className={input} />
          </label>
          <button type="submit" disabled={takedown.isPending} className={button}>Suspend site</button>
          <button type="button" disabled={takedown.isPending || takedownReason.length < 3} onClick={() => takedown.mutate(false)} className={button}>
            Reinstate
          </button>
        </form>
        {takedown.isSuccess ? <p role="status" className="mt-2">Done.</p> : null}
        {failure(takedown.error, "No publication to change.")}
      </section>

      <section aria-labelledby="grant-title" className="panel p-4">
        <h2 id="grant-title" className="section-title">Entitlements</h2>
        <p className="mt-1 text-sm text-muted">Extra generation allowance for the closed beta, for every month the grant overlaps.</p>
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={submit(() => grant.mutate())}>
          <label className="flex flex-col gap-1">
            Workspace ID
            <input required value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)} className={`${input} w-80 font-mono`} />
          </label>
          <label className="flex flex-col gap-1">
            USD
            <input required type="number" min="0.01" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} className={`${input} w-24`} />
          </label>
          <label className="flex flex-col gap-1">
            Days
            <input required type="number" min="1" max="366" value={days} onChange={(e) => setDays(e.target.value)} className={`${input} w-20`} />
          </label>
          <label className="flex flex-col gap-1">
            Reason
            <input required minLength={3} maxLength={500} value={grantReason} onChange={(e) => setGrantReason(e.target.value)} className={input} />
          </label>
          <button type="submit" disabled={grant.isPending} className={button}>Grant</button>
        </form>
        {failure(grant.error, "Could not grant.")}
        {grants.data?.items.length ? (
          <ul className="mt-2 divide-y divide-border text-sm">
            {grants.data.items.map((g) => (
              <li key={g.id} className="flex items-center justify-between gap-2 py-1">
                <span>
                  {g.kind === "credit" ? "Credit of " : ""}
                  {formatUsd(g.allowance_micro_usd)} until {new Date(g.expires_at).toLocaleDateString()} · {g.reason}
                  {g.revoked_at ? " · revoked" : ""}
                </span>
                {g.revoked_at ? null : (
                  <button type="button" onClick={() => revoke.mutate(g.id)} disabled={revoke.isPending} className={button}>Revoke</button>
                )}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section aria-labelledby="events-title" className="panel p-4">
        <h2 id="events-title" className="section-title">Operator log</h2>
        <ul className="mt-2 divide-y divide-border text-sm">
          {events.data?.items.map((e) => (
            <li key={e.id} className="py-1">
              {new Date(e.created_at).toLocaleString()} · {e.action}
              {e.workspace_id ? <> · <span className="font-mono">{e.workspace_id}</span></> : null}
              {e.reason ? ` · ${e.reason}` : ""}
            </li>
          ))}
        </ul>
      </section>
    </section>
  );
}
