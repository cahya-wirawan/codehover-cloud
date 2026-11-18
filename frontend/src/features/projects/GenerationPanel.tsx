import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, type Estimate, formatUsd, type Snapshot } from "../../lib/api/resources";

const lines = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** Snapshot -> scope -> estimate -> capped build (01 FR-006 to FR-008; 03 estimate flow). */
export function GenerationPanel({ projectId, workspaceId }: { projectId: string; workspaceId: string }) {
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [preset, setPreset] = useState("");
  const [include, setInclude] = useState("**");
  const [exclude, setExclude] = useState("");
  const [maxFiles, setMaxFiles] = useState(100);
  const [pages, setPages] = useState(5);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [capDollars, setCapDollars] = useState("");

  const presets = useQuery({ queryKey: ["presets"], queryFn: api.presets });
  const usage = useQuery({ queryKey: ["usage", workspaceId], queryFn: () => api.usage(workspaceId) });
  const scanned = useQuery({
    queryKey: ["snapshot", projectId, snapshot?.id],
    queryFn: () => api.snapshot(projectId, snapshot?.id ?? ""),
    enabled: snapshot !== null,
    refetchInterval: (query) => (query.state.data && query.state.data.status !== "pending" && query.state.data.status !== "acquiring" ? false : 2000),
  });
  const current = scanned.data ?? snapshot;
  const chosenPreset = preset || presets.data?.items[0]?.name || "";

  const acquire = useMutation({ mutationFn: () => api.requestSnapshot(projectId), onSuccess: setSnapshot });
  const quote = useMutation({
    mutationFn: () =>
      api.createEstimate(projectId, current?.id ?? "", {
        model_preset: chosenPreset,
        include: lines(include),
        exclude: lines(exclude),
        max_files: maxFiles,
        guide_pages: pages,
      }),
    onSuccess: (value) => {
      setEstimate(value);
      setCapDollars("");
    },
  });
  const start = useMutation({
    mutationFn: () => {
      const dollars = Number.parseFloat(capDollars);
      const cap = capDollars.trim() === "" || Number.isNaN(dollars) ? null : Math.round(dollars * 100) * 10_000;
      return api.startFromEstimate(projectId, estimate?.id ?? "", cap);
    },
    onSuccess: (build) => navigate(`/p/${projectId}/builds/${build.id}`),
  });

  return (
    <section aria-labelledby="gen-title" className="mt-8 rounded-lg border border-border bg-surface p-4">
      <h2 id="gen-title" className="text-lg font-medium">Generate explanations and a guide</h2>
      {usage.data ? (
        <p className="mt-1 text-muted">
          Generation allowance this month: {formatUsd(usage.data.available_micro_usd)} available of{" "}
          {formatUsd(usage.data.allowance_micro_usd)}.
        </p>
      ) : null}

      <ol className="mt-4 space-y-4">
        <li>
          <h3 className="font-medium">1. Snapshot</h3>
          {current ? (
            <p className="mt-1 text-muted">
              Commit <span className="font-mono">{current.commit_sha.slice(0, 12)}</span> · {current.status}
              {current.status === "scanned"
                ? ` · ${current.scan_summary.selected ?? 0} source files selectable, ${(current.scan_summary.excluded ?? 0) + (current.scan_summary.blocked ?? 0)} excluded or blocked`
                : null}
              {current.rejection_code ? ` · ${current.rejection_code}` : null}
            </p>
          ) : null}
          <button type="button" onClick={() => acquire.mutate()} disabled={acquire.isPending}
            className="mt-2 h-9 rounded-lg border border-border px-3">
            {current ? "Use latest commit" : "Take snapshot"}
          </button>
          {acquire.error ? <p role="alert" className="mt-2 text-destructive">{errorText(acquire.error, "Could not take a snapshot.")}</p> : null}
        </li>

        <li>
          <h3 className="font-medium">2. Scope and model</h3>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              Model preset
              <select value={chosenPreset} onChange={(e) => setPreset(e.target.value)} className="h-9 rounded-lg border border-border bg-surface px-2">
                {presets.data?.items.map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
              <span className="text-sm text-muted">{presets.data?.items.find((p) => p.name === chosenPreset)?.description}</span>
            </label>
            <label className="flex flex-col gap-1">
              Maximum files
              <input type="number" min={1} max={500} value={maxFiles} onChange={(e) => setMaxFiles(Number(e.target.value))}
                className="h-9 rounded-lg border border-border bg-surface px-2" />
            </label>
            <label className="flex flex-col gap-1">
              Include (one glob per line)
              <textarea value={include} onChange={(e) => setInclude(e.target.value)} rows={3}
                className="rounded-lg border border-border bg-surface p-2 font-mono" />
            </label>
            <label className="flex flex-col gap-1">
              Exclude (one glob per line)
              <textarea value={exclude} onChange={(e) => setExclude(e.target.value)} rows={3}
                className="rounded-lg border border-border bg-surface p-2 font-mono" />
            </label>
            <label className="flex flex-col gap-1">
              Guide pages
              <input type="number" min={1} max={8} value={pages} onChange={(e) => setPages(Number(e.target.value))}
                className="h-9 rounded-lg border border-border bg-surface px-2" />
            </label>
          </div>
          <button type="button" onClick={() => quote.mutate()}
            disabled={quote.isPending || current?.status !== "scanned" || !chosenPreset}
            className="mt-3 h-9 rounded-lg border border-border px-3">
            Estimate
          </button>
          {quote.error ? <p role="alert" className="mt-2 text-destructive">{errorText(quote.error, "Could not estimate.")}</p> : null}
        </li>

        {estimate ? (
          <li>
            <h3 className="font-medium">3. Estimate</h3>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              <dt className="text-muted">Files</dt>
              <dd>{estimate.selected_files} selected, {estimate.excluded_files} left out</dd>
              <dt className="text-muted">Tokens</dt>
              <dd>
                {estimate.estimated_tokens.low.input.toLocaleString()}–{estimate.estimated_tokens.high.input.toLocaleString()} in,{" "}
                {estimate.estimated_tokens.low.output.toLocaleString()}–{estimate.estimated_tokens.high.output.toLocaleString()} out
              </dd>
              <dt className="text-muted">Cost range</dt>
              <dd>{formatUsd(estimate.cost_micro_usd.low)} – {formatUsd(estimate.cost_micro_usd.high)}</dd>
              <dt className="text-muted">Prices</dt>
              <dd className="font-mono">{estimate.price_version}</dd>
              <dt className="text-muted">Valid until</dt>
              <dd>{new Date(estimate.expires_at).toLocaleTimeString()}</dd>
            </dl>
            <p className="mt-2 text-sm text-muted">{estimate.disclaimer}</p>
            <label className="mt-3 flex max-w-xs flex-col gap-1">
              Spending cap in USD (empty: the lower of the estimate's high bound and your remaining allowance)
              <input inputMode="decimal" value={capDollars} onChange={(e) => setCapDollars(e.target.value)}
                placeholder={formatUsd(estimate.cost_micro_usd.high).slice(1)}
                className="h-9 rounded-lg border border-border bg-surface px-2" />
            </label>
            <button type="button" onClick={() => start.mutate()} disabled={start.isPending}
              className="mt-3 h-10 rounded-lg bg-primary px-4 font-medium text-primary-foreground">
              Start build
            </button>
            {start.error ? <p role="alert" className="mt-2 text-destructive">{errorText(start.error, "Could not start the build.")}</p> : null}
          </li>
        ) : null}
      </ol>
    </section>
  );
}
