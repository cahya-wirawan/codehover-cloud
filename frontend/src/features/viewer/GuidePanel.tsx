import { useQuery } from "@tanstack/react-query";
import { Fragment } from "react";
import { Link, useNavigate, useParams } from "react-router";

import type { CheckStatus, GuidePage, ViewerSource } from "../../lib/api/resources";

const BADGE: Record<CheckStatus, { label: string; className: string }> = {
  supported: { label: "Supported", className: "border-border" },
  partial: { label: "Partly supported", className: "border-border" },
  unsupported: { label: "Unsupported", className: "border-destructive text-destructive" },
  unchecked: { label: "Not checked", className: "border-border text-muted" },
  invalid: { label: "Invalid citation", className: "border-destructive text-destructive" },
};

const CITATION = /\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]/g;

export function Badge({ status }: { status: CheckStatus }) {
  const badge = BADGE[status];
  return <span className={`rounded border px-1.5 text-sm ${badge.className}`}>{badge.label}</span>;
}

/** Guide Markdown is shown as text, never as HTML (19 P04); citations become labeled source ranges. */
export function GuideText({ page, codeBase }: { page: GuidePage; codeBase?: string }) {
  const paths = new Map(page.citations.map((c) => [c.file_id, c.path ?? "unknown file"]));
  const parts: (string | { label: string; key: string; href: string | null })[] = [];
  let last = 0;
  for (const match of page.markdown.matchAll(CITATION)) {
    parts.push(page.markdown.slice(last, match.index));
    const [, fileId = "", start = "", end = ""] = match;
    const known = paths.has(fileId);
    parts.push({
      label: `${paths.get(fileId) ?? "unknown file"}:${start}–${end}`,
      key: `${match.index}`,
      href: known && codeBase ? `${codeBase}/code?file=${encodeURIComponent(fileId)}&line=${start}&end=${end}` : null,
    });
    last = (match.index ?? 0) + match[0].length;
  }
  parts.push(page.markdown.slice(last));
  return (
    <div className="mt-3 whitespace-pre-wrap leading-relaxed">
      {parts.map((part, i) =>
        typeof part === "string" ? (
          <Fragment key={i}>{part}</Fragment>
        ) : (
          part.href ? (
            <Link key={part.key} to={part.href} className="rounded bg-surface px-1 font-mono text-sm text-primary underline-offset-2 hover:underline">
              [{part.label}]
            </Link>
          ) : (
            <span key={part.key} className="rounded bg-surface px-1 font-mono text-sm text-primary">[{part.label}]</span>
          )
        ),
      )}
    </div>
  );
}

/** Guide index and page (02 S12); the open page is part of the URL. */
export function GuidePanel({ source, base }: { source: ViewerSource; base: string }) {
  const { slug = null } = useParams();
  const navigate = useNavigate();
  const guide = useQuery({ queryKey: ["guide", source.key], queryFn: () => source.guide() });
  const quality = useQuery({
    queryKey: ["quality", source.key],
    queryFn: () => (source.quality ? source.quality() : Promise.resolve(null)),
    enabled: source.quality !== null,
  });
  const page = useQuery({
    queryKey: ["guide-page", source.key, slug],
    queryFn: () => source.guidePage(slug ?? ""),
    enabled: slug !== null,
  });
  if (!guide.data || guide.data.pages.length === 0) return null;

  return (
    <section aria-labelledby="guide-title" className="mt-6">
      <h2 id="guide-title" className="text-lg font-medium">Guide</h2>
      {quality.data ? (
        <p className="mt-1 text-muted">
          {quality.data.counts.supported} of {quality.data.claims} cited claims supported
          {quality.data.counts.unchecked ? `, ${quality.data.counts.unchecked} not checked` : ""}
          {quality.data.counts.invalid ? `, ${quality.data.counts.invalid} with invalid citations` : ""}
          {" · "}
          {quality.data.publishable ? "ready for review" : `not publishable yet (${quality.data.blockers.join(", ")})`}
        </p>
      ) : null}
      {guide.data.overview ? <p className="mt-3 whitespace-pre-wrap">{guide.data.overview.overview}</p> : null}
      <ul className="mt-3 flex flex-wrap gap-2">
        {guide.data.pages.map((p) => (
          <li key={p.slug}>
            <button type="button" onClick={() => navigate(`${base}/guide/${encodeURIComponent(p.slug)}`)} aria-pressed={slug === p.slug}
              className="flex items-center gap-2 rounded-lg border border-border px-3 py-1">
              {p.title} <Badge status={p.check_status} />
            </button>
          </li>
        ))}
      </ul>
      {page.data ? (
        <article className="mt-4 rounded-lg border border-border bg-surface p-4">
          <GuideText page={page.data} codeBase={base} />
          <h3 className="mt-4 font-medium">Claims</h3>
          <ul className="mt-2 space-y-1">
            {page.data.claims.map((c) => (
              <li key={c.claim_key} className="flex flex-wrap items-baseline gap-2">
                <Badge status={c.status} />
                {c.waived ? <span className="text-sm text-muted">(waived)</span> : null}
                <span>{c.text}</span>
                {c.reason ? <span className="text-sm text-muted">— {c.reason}</span> : null}
              </li>
            ))}
          </ul>
        </article>
      ) : null}
    </section>
  );
}
