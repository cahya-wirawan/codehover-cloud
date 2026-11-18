import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { NavLink, Outlet, useNavigate, useOutletContext, useParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, type Build, buildSource, formatUsd, TERMINAL } from "../../lib/api/resources";
import { CodeViewer } from "./CodeViewer";
import { GuidePanel } from "./GuidePanel";
import { ReviewPanel } from "./ReviewPanel";
import { SearchPanel } from "./SearchPanel";
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

interface BuildContext {
  build: Build;
  base: string;
  generation: boolean;
  canWaive: boolean;
}

const useBuild = () => useOutletContext<BuildContext>();

/** Build detail (02 S09) with the reading tabs of a ready build (S11-S14). */
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
  const project = useQuery({ queryKey: ["project", pid], queryFn: () => api.project(pid), enabled: pid !== "" });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const role = workspaces.data?.items.find((w) => w.id === project.data?.workspace_id)?.role;
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
  const base = `/p/${pid}/builds/${bid}`;
  const error = cancel.error ?? retry.error;
  const tab = ({ isActive }: { isActive: boolean }) =>
    `rounded-lg px-3 py-1 ${isActive ? "bg-surface-muted font-medium" : "text-muted hover:text-foreground"}`;

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
            {data.cancel_requested_at && !terminal ? " · cancelling after the current step" : null}
            {data.rejection_code ? <> · rejected by safety scan ({data.rejection_code})</> : null}
          </p>
          {generation ? (
            <p className="mt-1 text-muted">
              Spent {formatUsd(data.usage.settled_micro_usd)} of a {formatUsd(data.usage.cap_micro_usd)} cap
              {data.usage.reserved_micro_usd ? ` · ${formatUsd(data.usage.reserved_micro_usd)} still held` : ""}
            </p>
          ) : null}
          <div className="mt-3 flex gap-2">
            {!terminal && !data.cancel_requested_at ? (
              <button type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending}
                className="h-9 rounded-lg border border-input-border px-3">Cancel build</button>
            ) : null}
            {terminal ? (
              <button type="button" onClick={() => retry.mutate()} disabled={retry.isPending}
                className="h-9 rounded-lg border border-input-border px-3">Retry as a new build</button>
            ) : null}
          </div>
          {error ? (
            <p role="alert" className="mt-2 text-destructive">
              {error instanceof ApiError ? error.message : "The request failed."}
            </p>
          ) : null}
          <nav aria-label="Build sections" className="mt-4 flex flex-wrap gap-1 border-b border-border pb-2">
            <NavLink end to={base} className={tab}>Overview</NavLink>
            {data.status === "ready" ? (
              <>
                <NavLink to={`${base}/code`} className={tab}>Code</NavLink>
                {generation ? <NavLink to={`${base}/guide`} className={tab}>Guide</NavLink> : null}
                <NavLink to={`${base}/search`} className={tab}>Search</NavLink>
                {generation ? <NavLink to={`${base}/review`} className={tab}>Review</NavLink> : null}
              </>
            ) : null}
          </nav>
          <Outlet context={{ build: data, base, generation, canWaive: role === "admin" || role === "owner" } satisfies BuildContext} />
        </>
      ) : (
        <p role="status" className="mt-3 text-muted">Loading build…</p>
      )}
    </section>
  );
}

export function BuildOverview() {
  const { build } = useBuild();
  return (
    <>
      <ol className="mt-4 flex flex-wrap gap-2" aria-label="Stages">
        {build.stages.map((s) => (
          <li key={s.stage} className="rounded-lg border border-border bg-surface px-3 py-1">
            {s.stage}: {STATE_LABEL[s.state] ?? s.state}
            {s.error_code ? <span className="text-destructive"> ({s.error_code})</span> : null}
          </li>
        ))}
      </ol>
      {build.status === "ready" ? (
        <p className="mt-4 text-muted">
          {build.counts.files} files · {build.counts.symbols} definitions · prompts {build.prompt_version}
        </p>
      ) : null}
    </>
  );
}

export function BuildCode() {
  const { build } = useBuild();
  return <CodeViewer source={buildSource(build.id)} />;
}

export function BuildGuide() {
  const { build, base } = useBuild();
  return <GuidePanel source={buildSource(build.id)} base={base} />;
}

export function BuildSearch() {
  const { build, base } = useBuild();
  return <SearchPanel source={buildSource(build.id)} base={base} />;
}

export function BuildReview() {
  const { build, canWaive } = useBuild();
  return <ReviewPanel buildId={build.id} canWaive={canWaive} />;
}
