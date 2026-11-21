import { useQuery } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, CircleDashed, CircleX, FileCode2 } from "lucide-react";
import { Fragment, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { Badge as UiBadge } from "../../components/ui/badge";
import type { CheckStatus, GuidePage, ViewerSource } from "../../lib/api/resources";
import { cn } from "../../lib/cn";
import { GuideMarkdown } from "./GuideMarkdown";

const BADGE: Record<CheckStatus, { label: string; tone: "success" | "warning" | "destructive" | "neutral"; icon: ReactNode }> = {
  supported: { label: "Supported", tone: "success", icon: <CircleCheck aria-hidden="true" /> },
  partial: { label: "Partly supported", tone: "warning", icon: <CircleAlert aria-hidden="true" /> },
  unsupported: { label: "Unsupported", tone: "destructive", icon: <CircleX aria-hidden="true" /> },
  unchecked: { label: "Not checked", tone: "neutral", icon: <CircleDashed aria-hidden="true" /> },
  invalid: { label: "Invalid citation", tone: "destructive", icon: <CircleX aria-hidden="true" /> },
};

const CITATION = /\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]/g;

/** A claim's check status: icon and words, never colour alone (17). */
export function Badge({ status }: { status: CheckStatus }) {
  const badge = BADGE[status];
  return (
    <UiBadge tone={badge.tone} icon={badge.icon}>
      {badge.label}
    </UiBadge>
  );
}

/** Guide Markdown rendered as elements, never as HTML (19 P04); citations become labeled source ranges. */
export function GuideText({ page, codeBase }: { page: GuidePage; codeBase?: string }) {
  const paths = new Map(page.citations.map((c) => [c.file_id, c.path ?? "unknown file"]));
  const withCitations = (text: string, key: string): ReactNode => {
    const parts: ReactNode[] = [];
    let last = 0;
    for (const match of text.matchAll(CITATION)) {
      parts.push(text.slice(last, match.index));
      const [, fileId = "", start = "", end = ""] = match;
      const label = `${paths.get(fileId) ?? "unknown file"}:${start}–${end}`;
      const href =
        paths.has(fileId) && codeBase
          ? `${codeBase}/code?file=${encodeURIComponent(fileId)}&line=${start}&end=${end}`
          : null;
      parts.push(
        href ? (
          <Link
            key={`${key}.${match.index}`}
            to={href}
            className="mx-0.5 inline-flex items-center gap-1 rounded-[4px] border border-primary/25 bg-primary/[0.06] px-1.5 align-baseline font-mono text-[12px] leading-5 text-primary hover:border-primary/50"
          >
            <FileCode2 aria-hidden="true" className="size-3" />[{label}]
          </Link>
        ) : (
          <span
            key={`${key}.${match.index}`}
            className="mx-0.5 inline-flex items-center rounded-[4px] border border-border px-1.5 align-baseline font-mono text-[12px] leading-5 text-muted"
          >
            [{label}]
          </span>
        ),
      );
      last = (match.index ?? 0) + match[0].length;
    }
    parts.push(text.slice(last));
    return <Fragment key={key}>{parts}</Fragment>;
  };
  return (
    <div className="max-w-[72ch] text-[15px] leading-[26px]">
      <GuideMarkdown markdown={page.markdown} text={withCitations} />
    </div>
  );
}

/** The repository overview, shown before a page is chosen: Markdown rendered the same way as guide pages. */
export function OverviewText({ overview }: { overview: string }) {
  return (
    <div className="max-w-[72ch] text-[15px] leading-[26px]">
      <GuideMarkdown markdown={overview} text={(text, key) => <Fragment key={key}>{text}</Fragment>} />
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
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="guide-title" className="section-title">Guide</h2>
        {quality.data ? (
          <p className="text-[13px] text-muted">
            {quality.data.counts.supported} of {quality.data.claims} cited claims supported
            {quality.data.counts.unchecked ? `, ${quality.data.counts.unchecked} not checked` : ""}
            {quality.data.counts.invalid ? `, ${quality.data.counts.invalid} with invalid citations` : ""}
            {"; "}
            {quality.data.publishable ? "ready for review" : `not publishable yet (${quality.data.blockers.join(", ")})`}
          </p>
        ) : null}
      </div>
      <div className="mt-4 grid gap-8 lg:grid-cols-[240px_minmax(0,1fr)]">
        <nav aria-label="Guide pages" className="lg:sticky lg:top-24 lg:self-start">
          <ul className="flex flex-col gap-0.5">
            {guide.data.pages.map((p, index) => (
              <li key={p.slug}>
                <button
                  type="button"
                  onClick={() => navigate(`${base}/guide/${encodeURIComponent(p.slug)}`)}
                  aria-pressed={slug === p.slug}
                  className={cn(
                    "flex w-full items-start gap-2.5 rounded-[var(--radius-control)] px-2.5 py-2 text-left hover:bg-surface-muted",
                    slug === p.slug && "bg-surface-muted",
                  )}
                >
                  <span className="mt-px font-mono text-xs text-muted">{index + 1}</span>
                  <span className="min-w-0 flex-1">
                    <span className={cn("block", slug === p.slug && "font-medium")}>{p.title}</span>
                    <span className="mt-1 block">
                      <Badge status={p.check_status} />
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="min-w-0">
          {page.data ? (
            <article>
              <GuideText page={page.data} codeBase={base} />
              <h3 className="mt-10 border-t border-border pt-6 section-title">Claims</h3>
              <ul className="mt-3 flex max-w-[72ch] flex-col gap-3">
                {page.data.claims.map((c) => (
                  <li key={c.claim_key} className="flex flex-col gap-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <Badge status={c.status} />
                      {c.waived ? <span className="text-xs text-muted">(waived)</span> : null}
                    </span>
                    <span className="leading-[22px]">{c.text}</span>
                    {c.reason ? <span className="text-[13px] text-muted">{c.reason}</span> : null}
                  </li>
                ))}
              </ul>
            </article>
          ) : guide.data.overview ? (
            <OverviewText overview={guide.data.overview.overview} />
          ) : (
            <p className="text-muted">Choose a page to read.</p>
          )}
        </div>
      </div>
    </section>
  );
}
