import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../lib/api/client";
import { api, type CheckStatus } from "../../lib/api/resources";
import { Badge } from "./GuidePanel";

const BLOCKER_TEXT: Record<string, string> = {
  invalid_citations: "some citations point outside the snapshot or at excluded files (cannot be waived)",
  unchecked_claims: "some claims were not checked; a retry build re-runs the checks",
  unsupported_claims: "some claims are unsupported and not waived",
  failed_pages: "some guide pages could not be written",
  uncited_pages: "some pages have no checked claim",
};

/** Verification review (02 S14): coverage, claim checks, waivers. Waiving needs the admin role. */
export function ReviewPanel({ buildId, canWaive }: { buildId: string; canWaive: boolean }) {
  const client = useQueryClient();
  const [status, setStatus] = useState<CheckStatus | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const quality = useQuery({ queryKey: ["quality", buildId], queryFn: () => api.quality(buildId) });
  const checks = useQuery({
    queryKey: ["quality-checks", buildId, status],
    queryFn: () => api.qualityChecks(buildId, status, null),
  });
  const waive = useMutation({
    mutationFn: () => api.waive(buildId, [...selected], reason.trim()),
    onSuccess: () => {
      setSelected(new Set());
      setReason("");
      void client.invalidateQueries({ queryKey: ["quality", buildId] });
      void client.invalidateQueries({ queryKey: ["quality-checks", buildId] });
    },
  });
  const q = quality.data;

  return (
    <section aria-labelledby="review-title" className="mt-4">
      <h2 id="review-title" className="text-lg font-medium">Review</h2>
      {q ? (
        <>
          <p className="mt-1">
            {q.publishable ? "Ready to publish." : "Not publishable yet."}{" "}
            {q.requires_acknowledgment ? "Partly supported claims need an admin acknowledgment at publication." : null}
          </p>
          {q.blockers.length ? (
            <ul className="mt-1 list-disc pl-5 text-destructive">
              {q.blockers.map((b) => <li key={b}>{BLOCKER_TEXT[b] ?? b}</li>)}
            </ul>
          ) : null}
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
            <dt className="text-muted">Cited claims</dt><dd>{q.claims}</dd>
            {(Object.keys(q.counts) as CheckStatus[]).map((s) => (
              <div key={s} className="contents"><dt className="text-muted"><Badge status={s} /></dt><dd>{q.counts[s]}</dd></div>
            ))}
            <dt className="text-muted">Waived</dt><dd>{q.waived.unsupported + q.waived.partial}</dd>
            <dt className="text-muted">Uncited sentences</dt><dd>{q.uncited_sentences} (reported separately)</dd>
          </dl>
        </>
      ) : null}
      <label className="mt-4 flex max-w-xs flex-col gap-1">
        Show claims
        <select value={status ?? ""} onChange={(e) => setStatus((e.target.value || null) as CheckStatus | null)}
          className="h-9 rounded-lg border border-input-border bg-surface px-2">
          <option value="">All</option>
          <option value="unsupported">Unsupported</option>
          <option value="partial">Partly supported</option>
          <option value="unchecked">Not checked</option>
          <option value="invalid">Invalid citation</option>
          <option value="supported">Supported</option>
        </select>
      </label>
      <ul className="mt-3 divide-y divide-border rounded-lg border border-border bg-surface">
        {checks.data?.items.map((c) => {
          const waivable = canWaive && !c.waived && (c.status === "unsupported" || c.status === "partial");
          return (
            <li key={c.id} className="flex flex-wrap items-baseline gap-2 p-3">
              {waivable ? (
                <input type="checkbox" aria-label={`Select claim: ${c.claim_text}`} checked={selected.has(c.claim_key)}
                  onChange={(e) => setSelected((prev) => {
                    const next = new Set(prev);
                    if (e.target.checked) next.add(c.claim_key);
                    else next.delete(c.claim_key);
                    return next;
                  })} />
              ) : null}
              <Badge status={c.status} />
              {c.waived ? <span className="text-sm text-muted">waived: {c.waived_reason}</span> : null}
              <span>{c.claim_text}</span>
              <span className="font-mono text-sm text-muted">{c.path ?? c.citation}:{c.start_line}–{c.end_line}</span>
              {c.reason ? <span className="text-sm text-muted">— {c.reason}</span> : null}
            </li>
          );
        })}
        {checks.data && checks.data.items.length === 0 ? <li className="p-3 text-muted">No claims in this view.</li> : null}
      </ul>
      {canWaive && selected.size ? (
        <form className="mt-3 flex max-w-xl flex-col gap-2" onSubmit={(e) => { e.preventDefault(); waive.mutate(); }}>
          <label className="flex flex-col gap-1">
            Reason for waiving {selected.size} claim(s) (visible on the site and in the audit log)
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} minLength={10} maxLength={1000} required rows={2}
              className="rounded-lg border border-input-border bg-surface p-2" />
          </label>
          <button type="submit" disabled={waive.isPending || reason.trim().length < 10}
            className="h-9 self-start rounded-lg border border-input-border px-3">Waive</button>
          {waive.error ? <p role="alert" className="text-destructive">{waive.error instanceof ApiError ? waive.error.message : "Could not waive."}</p> : null}
        </form>
      ) : null}
    </section>
  );
}
