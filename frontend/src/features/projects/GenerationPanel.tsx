import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api, type Estimate, formatUsd, LANGUAGES, LEVELS, type Snapshot } from "../../lib/api/resources";
import { ownKeysLabel } from "../workspaces/providers";

const lines = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** Snapshot -> scope -> estimate -> capped build (01 FR-006 to FR-008; 03 estimate flow). */
export function GenerationPanel({
  projectId,
  workspaceId,
  uploaded = false,
}: {
  projectId: string;
  workspaceId: string;
  uploaded?: boolean;
}) {
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [preset, setPreset] = useState("");
  const [include, setInclude] = useState("**");
  const [exclude, setExclude] = useState("");
  const [maxFiles, setMaxFiles] = useState(100);
  const [pages, setPages] = useState(5);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [capDollars, setCapDollars] = useState("");

  // "Make a variant" on a ready build opens this panel with its build and snapshot (FR-026).
  const [search] = useSearchParams();
  const variantOf = search.get("variant_of");
  const variantSnapshot = search.get("snapshot");
  const [language, setLanguage] = useState(variantOf ? "de" : "en");
  const [level, setLevel] = useState("standard");
  // Several languages (FR-026): the base language plus these, each a variant build started beside the base.
  const [extra, setExtra] = useState<string[]>([]);
  const others = variantOf ? [] : extra.filter((code) => code !== language);
  const presets = useQuery({ queryKey: ["presets", workspaceId], queryFn: () => api.presets(workspaceId) });
  const [batch, setBatch] = useState(false);
  // P07: further languages translate the first build's text instead of being written from the source.
  const [translate, setTranslate] = useState(false);
  const canTranslate = others.length > 0 || variantOf !== null;
  const [ownKey, setOwnKey] = useState(false);
  const usage = useQuery({ queryKey: ["usage", workspaceId], queryFn: () => api.usage(workspaceId) });
  const scanned = useQuery({
    queryKey: ["snapshot", projectId, snapshot?.id ?? variantSnapshot],
    queryFn: () => api.snapshot(projectId, snapshot?.id ?? variantSnapshot ?? ""),
    enabled: snapshot !== null || variantSnapshot !== null,
    refetchInterval: (query) => (query.state.data && query.state.data.status !== "pending" && query.state.data.status !== "acquiring" ? false : 2000),
  });
  const current = scanned.data ?? snapshot;
  const chosenPreset = preset || presets.data?.items[0]?.name || "";
  const chosen = presets.data?.items.find((p) => p.name === chosenPreset);

  const acquire = useMutation({ mutationFn: () => api.requestSnapshot(projectId), onSuccess: setSnapshot });
  const send = useMutation({ mutationFn: (file: File) => api.uploadArchive(projectId, file), onSuccess: setSnapshot });
  const quote = useMutation({
    mutationFn: () =>
      api.createEstimate(projectId, current?.id ?? "", {
        model_preset: chosenPreset,
        include: lines(include),
        exclude: lines(exclude),
        max_files: maxFiles,
        guide_pages: pages,
        languages: [language, ...others],
        levels: [level],
        ...(variantOf ? { variant_of: variantOf } : {}),
        ...(batch && chosen?.batch ? { batch: true } : {}),
        ...(translate && canTranslate ? { translate: true } : {}),
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
      return api.startFromEstimate(projectId, estimate?.id ?? "", cap, ownKey && Boolean(chosen?.own_key));
    },
    onSuccess: (build) => navigate(`/p/${projectId}/builds/${build.id}`),
  });

  return (
    <section aria-labelledby="gen-title" className="page-section">
      <h2 id="gen-title" className="section-title">Generate explanations and a guide</h2>
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
          {uploaded ? (
            <label className="mt-2 flex flex-col gap-1.5">
              <span className="text-[13px] font-medium">{current ? "Upload a new version (ZIP)" : "Upload the repository (ZIP)"}</span>
              <input
                type="file"
                accept=".zip,application/zip"
                disabled={send.isPending}
                className="field py-1.5"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) send.mutate(file);
                  e.target.value = "";
                }}
              />
              <span className="text-xs text-muted">At most 100 MiB. Secrets, binaries and vendored files are excluded by the scan.</span>
            </label>
          ) : (
            <button type="button" onClick={() => acquire.mutate()} disabled={acquire.isPending}
              className="mt-2 btn">
              {current ? "Use latest commit" : "Take snapshot"}
            </button>
          )}
          {acquire.error ? <p role="alert" className="mt-2 text-destructive">{errorText(acquire.error, "Could not take a snapshot.")}</p> : null}
          {send.error ? <p role="alert" className="mt-2 text-destructive">{errorText(send.error, "Could not upload the archive.")}</p> : null}
        </li>

        <li>
          <h3 className="font-medium">2. Scope and model</h3>
          {variantOf ? (
            <p role="status" className="mt-1 text-muted">
              A variant of{" "}
              <Link to={`/p/${projectId}/builds/${variantOf}`} className="text-primary underline underline-offset-2">
                this build
              </Link>
              : the same files and guide pages, written for another language or reader level. Its scope is the base
              build&apos;s.
            </p>
          ) : null}
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              Language
              <select value={language} onChange={(e) => setLanguage(e.target.value)} className="field">
                {Object.entries(LANGUAGES).map(([code, name]) => (
                  <option key={code} value={code}>{name}</option>
                ))}
              </select>
            </label>
            {variantOf ? null : (
              <fieldset className="flex flex-col gap-1">
                <legend>Also write in</legend>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {Object.entries(LANGUAGES)
                    .filter(([code]) => code !== language)
                    .map(([code, name]) => (
                      <label key={code} className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={extra.includes(code)}
                          onChange={(e) =>
                            setExtra((current) =>
                              e.target.checked ? [...current, code] : current.filter((c) => c !== code),
                            )
                          }
                        />
                        {name}
                      </label>
                    ))}
                </div>
                <span className="text-sm text-muted">
                  Each further language is its own build, started beside the first: the same files and guide pages,
                  written in that language.
                </span>
              </fieldset>
            )}
            <label className="flex flex-col gap-1">
              Reader level
              <select value={level} onChange={(e) => setLevel(e.target.value)} className="field">
                {Object.entries(LEVELS).map(([code, name]) => (
                  <option key={code} value={code}>{name}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Model preset
              <select value={chosenPreset} onChange={(e) => setPreset(e.target.value)} className="field">
                {presets.data?.items.map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
              <span className="text-sm text-muted">{presets.data?.items.find((p) => p.name === chosenPreset)?.description}</span>
            </label>
            {variantOf ? null : (
              <>
            <label className="flex flex-col gap-1">
              Maximum files
              <input type="number" min={1} max={500} value={maxFiles} onChange={(e) => setMaxFiles(Number(e.target.value))}
                className="field" />
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
                className="field" />
            </label>
              </>
            )}
          </div>
          {canTranslate ? (
            <label className="mt-3 flex items-start gap-2">
              <input type="checkbox" className="mt-1" checked={translate} onChange={(e) => setTranslate(e.target.checked)} />
              <span>
                {variantOf ? "Translate the base build's text (cheaper)" : "Translate the other languages from the first (cheaper)"}
                <span className="block text-sm text-muted">
                  The explanations, overview and guide pages are translated from the {variantOf ? "base build" : "first language"} instead
                  of written from the source again, so every language says the same and its mistakes too. Translated pages are
                  still checked against the source. The reader level stays the same.
                </span>
              </span>
            </label>
          ) : null}
          {chosen?.batch ? (
            <label className="mt-3 flex items-start gap-2">
              <input type="checkbox" className="mt-1" checked={batch} onChange={(e) => setBatch(e.target.checked)} />
              <span>
                Explain files in a batch
                <span className="block text-sm text-muted">
                  Cheaper per file but slower: the provider may take up to 24 hours. Files the batch cannot answer are
                  explained at the standard price, within the cap.
                </span>
              </span>
            </label>
          ) : null}
          <button type="button" onClick={() => quote.mutate()}
            disabled={quote.isPending || current?.status !== "scanned" || !chosenPreset}
            className="mt-3 btn">
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
              {estimate.config.languages.length > 1 && !estimate.config.variant_of ? (
                <>
                  <dt className="text-muted">Languages</dt>
                  <dd>
                    {estimate.per_translation_micro_usd ? (
                      <>
                        {LANGUAGES[estimate.config.languages[0] ?? ""] ?? estimate.config.languages[0]}:{" "}
                        {formatUsd(estimate.per_language_micro_usd.low)} – {formatUsd(estimate.per_language_micro_usd.high)};{" "}
                        {estimate.config.languages.slice(1).map((code) => LANGUAGES[code] ?? code).join(", ")}, translated:{" "}
                        {formatUsd(estimate.per_translation_micro_usd.low)} – {formatUsd(estimate.per_translation_micro_usd.high)} each.
                        The cap is shared in proportion to these estimates.
                      </>
                    ) : (
                      <>
                        {estimate.config.languages.map((code) => LANGUAGES[code] ?? code).join(", ")}:{" "}
                        {formatUsd(estimate.per_language_micro_usd.low)} – {formatUsd(estimate.per_language_micro_usd.high)} each.
                        The cap is shared equally among them.
                      </>
                    )}
                  </dd>
                </>
              ) : null}
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
                className="field" />
            </label>
            {chosen?.own_key ? (
              <label className="mt-3 flex items-start gap-2">
                <input type="checkbox" className="mt-1" checked={ownKey} onChange={(e) => setOwnKey(e.target.checked)} />
                <span>
                  Use this workspace&apos;s {ownKeysLabel(chosen.providers)}
                  <span className="block text-sm text-muted">
                    The provider bills your account directly and the plan allowance is not used. Set a spending cap.
                  </span>
                </span>
              </label>
            ) : null}
            <button type="button" onClick={() => start.mutate()} disabled={start.isPending || (ownKey && capDollars.trim() === "")}
              className="mt-3 btn btn-primary btn-lg">
              Start build
            </button>
            {start.error ? <p role="alert" className="mt-2 text-destructive">{errorText(start.error, "Could not start the build.")}</p> : null}
          </li>
        ) : null}
      </ol>
    </section>
  );
}
