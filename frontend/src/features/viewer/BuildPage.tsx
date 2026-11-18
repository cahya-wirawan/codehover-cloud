import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { NavLink, Outlet, useNavigate, useOutletContext, useParams } from "react-router";

import { Ban, RotateCcw } from "lucide-react";

import { Button } from "../../components/ui/button";
import { PageHeader } from "../../components/ui/section";
import { Skeleton } from "../../components/ui/skeleton";
import { ApiError } from "../../lib/api/client";
import { cn } from "../../lib/cn";
import { api, type Build, buildSource, formatUsd, TERMINAL } from "../../lib/api/resources";
import { CodeViewer } from "./CodeViewer";
import { ExportPanel } from "./ExportPanel";
import { GuidePanel } from "./GuidePanel";
import { ReviewPanel } from "./ReviewPanel";
import { SearchPanel } from "./SearchPanel";
import { StageIcon, StatusBadge } from "./StatusBadge";
import { stateLabel } from "./status-labels";
import { pollInterval, streamingSupported, useBuildEvents } from "./useBuildEvents";

interface BuildContext {
  build: Build;
  base: string;
  generation: boolean;
  canWaive: boolean;
  canExport: boolean;
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
    cn(
      "-mb-px border-b-2 px-1 pb-2.5 text-[13px] font-medium transition-colors duration-150",
      isActive ? "border-primary text-foreground" : "border-transparent text-muted hover:text-foreground",
    );

  if (!data) {
    return (
      <section aria-label="Build" className="flex flex-col gap-3">
        <p role="status" className="sr-only">Loading build…</p>
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-96" />
      </section>
    );
  }
  return (
    <section aria-labelledby="b-title">
      <PageHeader
        title={
          <span id="b-title">
            Build <span className="font-mono text-[18px] font-normal text-muted">{data.commit_sha.slice(0, 12)}</span>
          </span>
        }
        meta={
          <>
            <StatusBadge status={data.status} />
            <span>Started {new Date(data.created_at).toLocaleString()}</span>
            {data.cancel_requested_at && !terminal ? <span>Cancelling after the current step</span> : null}
            {data.rejection_code ? <span className="text-destructive">Rejected by the safety scan ({data.rejection_code})</span> : null}
            {generation ? (
              <span>
                Spent {formatUsd(data.usage.settled_micro_usd)} of a {formatUsd(data.usage.cap_micro_usd)} cap
                {data.usage.reserved_micro_usd ? `, ${formatUsd(data.usage.reserved_micro_usd)} still held` : ""}
              </span>
            ) : null}
          </>
        }
        actions={
          <>
            {!terminal && !data.cancel_requested_at ? (
              <Button onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                <Ban aria-hidden="true" /> Cancel build
              </Button>
            ) : null}
            {terminal ? (
              <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
                <RotateCcw aria-hidden="true" /> Retry as a new build
              </Button>
            ) : null}
          </>
        }
      />
      {data.content_purged_at ? (
        <p role="status" className="mb-4 rounded-[var(--radius-control)] border border-warning/40 bg-warning/10 px-3 py-2">
          Retention removed this build&apos;s explanations and guide on {new Date(data.content_purged_at).toLocaleDateString()};
          start a new build to read it again.
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mb-4 text-destructive">
          {error instanceof ApiError ? error.message : "The request failed."}
        </p>
      ) : null}
      <nav aria-label="Build sections" className="flex flex-wrap gap-6 border-b border-border">
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
      <Outlet
        context={{
          build: data,
          base,
          generation,
          canWaive: role === "admin" || role === "owner",
          canExport: role !== undefined && role !== "viewer",
        } satisfies BuildContext}
      />
    </section>
  );
}

export function BuildOverview() {
  const { build, canExport } = useBuild();
  return (
    <div className="mt-6 flex flex-col gap-8">
      <ol aria-label="Stages" className="flex flex-wrap items-center gap-y-3">
        {build.stages.map((s, index) => (
          <li key={s.stage} className="flex items-center">
            {index ? <span aria-hidden="true" className="mx-3 h-px w-8 bg-border" /> : null}
            <span className="flex items-center gap-2">
              <StageIcon state={s.state} />
              <span className="font-medium">{s.stage}</span>
              <span className="text-muted">
                <span className="sr-only">: </span>
                {stateLabel(s.state)}
              </span>
              {s.error_code ? <span className="font-mono text-xs text-destructive">{s.error_code}</span> : null}
            </span>
          </li>
        ))}
      </ol>
      {build.status === "ready" ? (
        <>
          <dl className="grid max-w-2xl grid-cols-3 gap-6">
            {(
              [
                ["Files", build.counts.files],
                ["Definitions", build.counts.symbols],
                ["Prompt set", build.prompt_version],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt className="text-xs text-muted">{label}</dt>
                <dd className="mt-1 text-[20px] leading-7 font-semibold tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
          <ExportPanel buildId={build.id} canExport={canExport} />
        </>
      ) : null}
    </div>
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
