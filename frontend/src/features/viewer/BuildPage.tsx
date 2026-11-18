import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useNavigate, useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, formatUsd, TERMINAL } from "../../lib/api/resources";
import { CodeViewer } from "./CodeViewer";
import { GuidePanel } from "./GuidePanel";
import { pollInterval, streamingSupported, useBuildEvents } from "./useBuildEvents";

const STATE_LABEL: Record<string, string> = {
  pending: "Pending",
  leased: "Running",
  succeeded: "Done",
  skipped: "Reused",
  retry_wait: "Waiting to retry",
  failed: "Failed",
  cancelled: "Cancelled",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "queued",
  running: "running",
  ready: "ready",
  failed: "failed",
  cancelled: "cancelled",
  budget_exhausted: "stopped: spending cap reached",
};

export function BuildPage() {
  const { pid = "", bid = "" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [streamFailed, setStreamFailed] = useState(false);
  const streaming = streamingSupported() && !streamFailed;
  const onUnavailable = useCallback(() => setStreamFailed(true), []);
  const build = useQuery({
    queryKey: ["build", bid],
    queryFn: () => api.build(bid),
    // Events drive refetches; polling (5 s with jitter) only when streaming is unavailable (10).
    refetchInterval: (query) =>
      (query.state.data && TERMINAL.has(query.state.data.status)) || streaming ? false : pollInterval(),
  });
  const terminal = build.data ? TERMINAL.has(build.data.status) : false;
  useBuildEvents(bid, build.data !== undefined && !terminal, onUnavailable);
  const cancel = useMutation({
    mutationFn: () => api.cancelBuild(bid),
    onSuccess: (value) => client.setQueryData(["build", bid], value),
  });
  const retry = useMutation({
    mutationFn: () => api.retryBuild(bid),
    onSuccess: (value) => navigate(`/p/${pid}/builds/${value.id}`),
  });
  const data = build.data;
  const generation = data?.estimate_id !== null && data?.estimate_id !== undefined;

  return (
    <section aria-labelledby="b-title">
      <h1 id="b-title" className="text-2xl font-medium">Build</h1>
      {data ? (
        <>
          <p className="mt-1 text-muted">
            Commit <span className="font-mono">{data.commit_sha.slice(0, 12)}</span> · status{" "}
            <strong className={data.status === "failed" || data.status === "budget_exhausted" ? "text-destructive" : ""}>
              {STATUS_LABEL[data.status] ?? data.status}
            </strong>
            {data.cancel_requested_at && !TERMINAL.has(data.status) ? " · cancelling after the current step" : null}
            {data.rejection_code ? <> · rejected by safety scan ({data.rejection_code})</> : null}
          </p>
          {generation ? (
            <p className="mt-1 text-muted">
              Spent {formatUsd(data.usage.settled_micro_usd)} of a {formatUsd(data.usage.cap_micro_usd)} cap
              {data.usage.reserved_micro_usd ? ` · ${formatUsd(data.usage.reserved_micro_usd)} still held` : ""}
            </p>
          ) : null}
          <div className="mt-3 flex gap-2">
            {!TERMINAL.has(data.status) && !data.cancel_requested_at ? (
              <button type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending}
                className="h-9 rounded-lg border border-border px-3">
                Cancel build
              </button>
            ) : null}
            {TERMINAL.has(data.status) ? (
              <button type="button" onClick={() => retry.mutate()} disabled={retry.isPending}
                className="h-9 rounded-lg border border-border px-3">
                Retry as a new build
              </button>
            ) : null}
          </div>
          {cancel.error || retry.error ? (
            <p role="alert" className="mt-2 text-destructive">
              {(cancel.error ?? retry.error) instanceof ApiError
                ? (cancel.error ?? retry.error)?.message
                : "The request failed."}
            </p>
          ) : null}
          <ol className="mt-4 flex flex-wrap gap-2" aria-label="Stages">
            {data.stages.map((s) => (
              <li key={s.stage} className="rounded-lg border border-border bg-surface px-3 py-1">
                {s.stage}: {STATE_LABEL[s.state] ?? s.state}
                {s.error_code ? <span className="text-destructive"> ({s.error_code})</span> : null}
              </li>
            ))}
          </ol>
          {data.status === "ready" ? (
            <>
              <p className="mt-4 text-muted">
                {data.counts.files} files · {data.counts.symbols} definitions ·{" "}
                {generation ? "machine-written explanations; check the cited code." : "explanations are derived from source only (replay); check cited code."}
              </p>
              {generation ? <GuidePanel buildId={bid} /> : null}
              <CodeViewer buildId={bid} />
            </>
          ) : null}
        </>
      ) : (
        <p role="status" className="mt-3 text-muted">Loading build…</p>
      )}
    </section>
  );
}
