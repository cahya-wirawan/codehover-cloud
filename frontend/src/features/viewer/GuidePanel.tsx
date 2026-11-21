import { useQuery } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, CircleDashed, CircleX } from "lucide-react";
import { Fragment, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { Badge as UiBadge } from "../../components/ui/badge";
import type { CheckStatus, GuideIndex, GuidePage, ViewerSource } from "../../lib/api/resources";
import { cn } from "../../lib/cn";
import { kindClass } from "./codeLine";
import { GuideMarkdown } from "./GuideMarkdown";
import { InlineText } from "./SymbolCard";

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

const plainText = (text: string, key: string) => <Fragment key={key}>{text}</Fragment>;

/** Guide Markdown rendered as elements, never as HTML (19 P04). Citations become chips that open the
 * exact lines, links to other pages stay in this guide, and definitions named in backticks link to their
 * code when the build has exactly one definition of that name. */
export function GuideText({ page, codeBase }: { page: GuidePage; codeBase?: string }) {
  const paths = new Map(page.citations.map((c) => [c.file_id, c.path ?? "unknown file"]));
  const mentions = new Map(page.symbols.map((s) => [s.mention, s]));
  const withCitations = (text: string, key: string): ReactNode => {
    const parts: ReactNode[] = [];
    let last = 0;
    for (const match of text.matchAll(CITATION)) {
      parts.push(text.slice(last, match.index));
      const [, fileId = "", start = "", end = ""] = match;
      const label = (
        <>
          {paths.get(fileId) ?? "unknown file"}:<span className="cl">{start === end ? start : `${start}–${end}`}</span>
        </>
      );
      const href =
        paths.has(fileId) && codeBase
          ? `${codeBase}/code?file=${encodeURIComponent(fileId)}&line=${start}&end=${end}`
          : null;
      parts.push(
        href ? (
          <Link key={`${key}.${match.index}`} to={href} className="cite" title="Open these lines">
            {label}
          </Link>
        ) : (
          <span key={`${key}.${match.index}`} className="cite">
            {label}
          </span>
        ),
      );
      last = (match.index ?? 0) + match[0].length;
    }
    parts.push(text.slice(last));
    return <Fragment key={key}>{parts}</Fragment>;
  };
  const code = (content: string, key: string): ReactNode => {
    const symbol = mentions.get(content.replace(/\(\)$/, ""));
    if (!symbol || !codeBase) return <code key={key}>{content}</code>;
    return (
      <Link
        key={key}
        to={`${codeBase}/code?file=${encodeURIComponent(symbol.file_id)}&line=${symbol.start_line}&symbol=${encodeURIComponent(symbol.id)}`}
        className={`sym-link ${kindClass(symbol.kind)}`}
        title={`Open ${symbol.kind} ${symbol.name}`}
      >
        <code>{content}</code>
      </Link>
    );
  };
  return (
    <div className="prose">
      <GuideMarkdown
        markdown={page.markdown}
        text={withCitations}
        guideHref={(slug) => (codeBase ? `${codeBase}/guide/${encodeURIComponent(slug)}` : null)}
        code={code}
      />
    </div>
  );
}

/** The repository overview, shown before a page is chosen: Markdown rendered the same way as guide pages. */
export function OverviewText({ overview }: { overview: string }) {
  return (
    <div className="home-overview">
      <GuideMarkdown markdown={overview} text={plainText} />
    </div>
  );
}

/** The guide's landing view: what the project is, its key ideas, where to start, and the pages. */
function GuideHome({ index, base, open }: { index: GuideIndex; base: string; open: (slug: string) => void }) {
  const overview = index.overview;
  return (
    <div className="home max-w-[860px]">
      {overview ? <OverviewText overview={overview.overview} /> : null}
      {overview?.key_concepts.length ? (
        <>
          <h3 className="home-h">Key ideas</h3>
          <dl className="concepts">
            {overview.key_concepts.map((c) => (
              <Fragment key={c.term}>
                <dt>{c.term}</dt>
                <dd>
                  <InlineText text={c.explanation} />
                </dd>
              </Fragment>
            ))}
          </dl>
        </>
      ) : null}
      {overview?.reading_order.length ? (
        <>
          <h3 className="home-h">Where to start reading</h3>
          <ol className="steps">
            {overview.reading_order.map((r) => (
              <li key={r.file_id}>
                <Link to={`${base}/code?file=${encodeURIComponent(r.file_id)}`} className="font-mono text-[15px] font-semibold">
                  {r.path ?? "Open the file"}
                </Link>
                <span className="why">
                  <InlineText text={r.reason} />
                </span>
              </li>
            ))}
          </ol>
        </>
      ) : null}
      <h3 className="home-h">The guide</h3>
      <ol className="steps guide-steps">
        {index.pages.map((p) => (
          <li key={p.slug}>
            <a
              href={`${base}/guide/${encodeURIComponent(p.slug)}`}
              onClick={(event) => {
                event.preventDefault();
                open(p.slug);
              }}
              className="text-[20px] font-medium"
            >
              {p.title}
            </a>
            {p.purpose ? <span className="why">{p.purpose}</span> : null}
          </li>
        ))}
      </ol>
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
  const pages = guide.data.pages;
  const open = (s: string) => navigate(`${base}/guide/${encodeURIComponent(s)}`);
  const at = pages.findIndex((p) => p.slug === slug);
  const previous = at > 0 ? pages[at - 1] : undefined;
  const next = at >= 0 ? pages[at + 1] : undefined;
  const current = at >= 0 ? pages[at] : undefined;

  return (
    <section aria-labelledby="guide-title" className="reading mt-6 rounded-[var(--radius-overlay)] bg-background px-5 py-6 sm:px-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="guide-title" className="text-[15px] font-medium text-muted">
          {slug ? (
            <a
              href={`${base}/guide`}
              onClick={(event) => {
                event.preventDefault();
                navigate(`${base}/guide`);
              }}
              className="underline-offset-2 hover:underline"
            >
              Guide
            </a>
          ) : (
            "Guide"
          )}
        </h2>
        {quality.data ? (
          <p className="text-[14px] text-muted">
            {quality.data.counts.supported} of {quality.data.claims} cited claims supported
            {quality.data.counts.unchecked ? `, ${quality.data.counts.unchecked} not checked` : ""}
            {quality.data.counts.invalid ? `, ${quality.data.counts.invalid} with invalid citations` : ""}
            {"; "}
            {quality.data.publishable ? "ready for review" : `not publishable yet (${quality.data.blockers.join(", ")})`}
          </p>
        ) : null}
      </div>
      <div className="mt-5 grid gap-10 lg:grid-cols-[230px_minmax(0,1fr)]">
        <nav aria-label="Guide pages" className="guide-nav lg:sticky lg:top-24 lg:self-start">
          <ul className="flex flex-col gap-0.5">
            {pages.map((p, index) => (
              <li key={p.slug}>
                {p.section && p.section !== pages[index - 1]?.section ? (
                  <span className="guide-section">{p.section}</span>
                ) : null}
                <button
                  type="button"
                  onClick={() => open(p.slug)}
                  aria-pressed={slug === p.slug}
                  className={cn(
                    "flex w-full items-start gap-2.5 rounded-[4px] px-2 py-1.5 text-left hover:bg-surface-muted",
                    slug === p.slug && "bg-surface font-semibold shadow-[inset_2px_0_0_var(--foreground)]",
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block">{p.title}</span>
                    {p.check_status !== "supported" ? (
                      <span className="mt-1 block">
                        <Badge status={p.check_status} />
                      </span>
                    ) : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="min-w-0">
          {page.data ? (
            <article className="guide-page">
              {current?.section ? <p className="crumb">{current.section}</p> : null}
              <GuideText page={page.data} codeBase={base} />
              <p className="checked mt-8">
                <Badge status={page.data.check_status} />{" "}
                {page.data.claims.length} cited {page.data.claims.length === 1 ? "claim" : "claims"} on this page
              </p>
              <h3 className="mt-6 border-t border-border pt-5 text-[20px] font-medium">Claims</h3>
              <ul className="mt-3 flex max-w-[68ch] flex-col gap-3 text-[16px]">
                {page.data.claims.map((c) => (
                  <li key={c.claim_key} className="flex flex-col gap-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <Badge status={c.status} />
                      {c.waived ? <span className="text-xs text-muted">(waived)</span> : null}
                    </span>
                    <span className="leading-[1.45]">{c.text}</span>
                    {c.reason ? <span className="text-[14px] text-muted">{c.reason}</span> : null}
                  </li>
                ))}
              </ul>
              <nav aria-label="Next and previous pages" className="pager">
                {previous ? (
                  <a
                    href={`${base}/guide/${encodeURIComponent(previous.slug)}`}
                    onClick={(event) => {
                      event.preventDefault();
                      open(previous.slug);
                    }}
                  >
                    <span className="pg-l">Previous</span>
                    {previous.title}
                  </a>
                ) : null}
                {next ? (
                  <a
                    href={`${base}/guide/${encodeURIComponent(next.slug)}`}
                    onClick={(event) => {
                      event.preventDefault();
                      open(next.slug);
                    }}
                    className="next"
                  >
                    <span className="pg-l">Next</span>
                    {next.title}
                  </a>
                ) : null}
              </nav>
            </article>
          ) : slug ? null : (
            <GuideHome index={guide.data} base={base} open={open} />
          )}
        </div>
      </div>
    </section>
  );
}
