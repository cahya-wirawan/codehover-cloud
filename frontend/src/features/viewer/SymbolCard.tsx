import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Pin, X } from "lucide-react";
import { Fragment, type ReactNode, useEffect, useRef } from "react";
import { Link } from "react-router";

import { Button } from "../../components/ui/button";
import type { Related, SymbolView, ViewerSource } from "../../lib/api/resources";
import { kindClass } from "./codeLine";

type Card = SymbolView["card"] & { returns?: string; details?: string };

/** Model text with `backticks` shown as code; nothing else is interpreted. */
export function InlineText({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <code key={i}>{part.slice(1, -1)}</code>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  );
}

function provenance(card: SymbolView["card"]): string {
  const generator = (card as { provenance?: { generator?: string } }).provenance?.generator;
  if (generator === "model") return "AI-generated; check the cited source.";
  if (generator === "replay-fallback") return "No valid model explanation; derived from the source only.";
  return "Derived from the source only.";
}

/** 07/A12: only references a SCIP indexer resolved may claim more than a name match. */
function resolutionLabel(r: Related): string {
  return r.resolution === "index" ? "resolved by type analysis" : "matched by name";
}

function Relations({ title, items, onFollow }: { title: string; items: Related[]; onFollow: (r: Related) => void }) {
  if (!items.length) return null;
  return (
    <div className="gloss-sec">
      <h4 className="gloss-label">{title}</h4>
      <ul className="mt-1 flex flex-col gap-0.5">
        {items.map((r) => (
          <li key={r.id} className="flex min-w-0 items-baseline gap-2">
            <button
              type="button"
              className={`sym ref ${kindClass(r.kind)} shrink-0 font-mono text-[13px] disabled:text-muted`}
              onClick={() => onFollow(r)}
              disabled={!r.file_id}
            >
              {r.name}
            </button>
            <span className="min-w-0 truncate text-[13.5px] text-muted">
              {r.path} · {resolutionLabel(r)}
              {r.ambiguous ? " (ambiguous: one of several definitions with this name)" : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Shell({
  label,
  kind,
  name,
  location,
  pinned,
  onPin,
  onClose,
  heading,
  children,
}: {
  label: string;
  kind: string;
  name: string;
  location?: string | undefined;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
  heading?: React.Ref<HTMLHeadingElement>;
  children: ReactNode;
}) {
  return (
    <article className={`gloss ${kindClass(kind)} flex max-h-[min(70vh,560px)] flex-col`}>
      <header className="flex items-start gap-2 px-4 pt-3 pb-1.5">
        <div className="gloss-head min-w-0 flex-1">
          <span className="gloss-kind">{label}</span>
          <h3 ref={heading} tabIndex={-1} className="gloss-name outline-none">
            {name}
          </h3>
          {location ? <span className="gloss-loc">{location}</span> : null}
        </div>
        {pinned ? (
          <Button variant="ghost" size="icon" className="-mr-1.5 size-8" onClick={onClose}>
            <X aria-hidden="true" />
            <span className="sr-only">Close</span>
          </Button>
        ) : (
          <Button variant="ghost" size="sm" className="-mr-1.5 text-muted" onClick={onPin}>
            <Pin aria-hidden="true" />
            Pin
          </Button>
        )}
      </header>
      <div className="flex flex-col gap-2.5 overflow-y-auto px-4 pb-3">{children}</div>
      {pinned ? null : (
        <p className="gloss-foot flex items-center gap-1 border-t border-border px-4 py-1.5">
          <ArrowUpRight aria-hidden="true" className="size-3" />
          Click or press Enter to keep this card open
        </p>
      )}
    </article>
  );
}

/** The hover card (02 "Hover card"): signature strip, explanation, parameters, provenance, relations and
 * guide pages. In the pinned state it is a non-modal dialog whose heading takes focus. */
export function SymbolCard({
  source,
  symbolId,
  pinned,
  onPin,
  onClose,
  onFollow,
  guideBase,
  showOpen = false,
}: {
  source: ViewerSource;
  symbolId: string;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
  onFollow: (r: Related) => void;
  /** Where this view's guide lives; without it the card does not link to guide pages. */
  guideBase?: string | undefined;
  /** Offer to open the definition (for a card shown at a use, not at the definition). */
  showOpen?: boolean;
}) {
  const { data } = useQuery({ queryKey: ["symbol", source.key, symbolId], queryFn: () => source.symbol(symbolId) });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (pinned && data) heading.current?.focus();
  }, [pinned, data]);
  if (!data) {
    return (
      <p role="status" className="p-4 text-muted">
        Loading definition…
      </p>
    );
  }
  const card = data.card as Card;
  return (
    <Shell
      label={data.kind}
      kind={data.kind}
      name={data.name}
      location={`${data.path}:${data.start_line}`}
      pinned={pinned}
      onPin={onPin}
      onClose={onClose}
      heading={heading}
    >
      {data.signature ? <pre className="gloss-sig">{data.signature}</pre> : null}
      {card.summary ? (
        <p className="gloss-sum">
          <InlineText text={card.summary} />
        </p>
      ) : (
        <p className="text-muted italic">No explanation for this definition.</p>
      )}
      {card.details ? (
        <p className="gloss-det whitespace-pre-wrap">
          <InlineText text={card.details} />
        </p>
      ) : null}
      {card.params?.length ? (
        <div className="gloss-sec">
          <span className="gloss-label">Parameters</span>
          <dl className="gloss-params">
            {card.params.map((p) => (
              <Fragment key={p.name}>
                <dt>{p.name}</dt>
                <dd>
                  <InlineText text={p.description} />
                </dd>
              </Fragment>
            ))}
          </dl>
        </div>
      ) : null}
      {card.returns ? (
        <p className="gloss-sec">
          <span className="gloss-label">Returns</span>
          <InlineText text={card.returns} />
        </p>
      ) : null}
      {card.notes ? (
        <p className="gloss-note">
          <InlineText text={card.notes} />
        </p>
      ) : null}
      <Relations title="Uses" items={data.uses} onFollow={onFollow} />
      <Relations title="Used by" items={data.used_by} onFollow={onFollow} />
      {guideBase && data.guides.length ? (
        <p className="card-guides gloss-sec">
          <span className="card-label">In the guide</span>
          {data.guides.map((g, i) => (
            <span key={g.slug}>
              {i ? ", " : ""}
              <Link to={`${guideBase}/guide/${encodeURIComponent(g.slug)}`}>{g.title}</Link>
            </span>
          ))}
        </p>
      ) : null}
      <p className="gloss-foot flex flex-wrap items-center gap-x-3">
        <span>{provenance(data.card)}</span>
        {showOpen ? (
          <button
            type="button"
            className="ml-auto underline underline-offset-2"
            onClick={() =>
              onFollow({
                id: data.id,
                name: data.name,
                kind: data.kind,
                path: data.path,
                file_id: data.file_id,
                start_line: data.start_line,
                resolution: "name",
                ambiguous: false,
              })
            }
          >
            Open definition
          </button>
        ) : null}
      </p>
    </Shell>
  );
}

/** A parameter's use: its owner's description of it, beside the owner's other parameters. */
export function ParamCard({
  source,
  ownerId,
  name,
  pinned,
  onPin,
  onClose,
}: {
  source: ViewerSource;
  ownerId: string;
  name: string;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
}) {
  const { data } = useQuery({ queryKey: ["symbol", source.key, ownerId], queryFn: () => source.symbol(ownerId) });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (pinned && data) heading.current?.focus();
  }, [pinned, data]);
  const params = (data?.card as Card | undefined)?.params ?? [];
  const mine = params.find((p) => p.name === name);
  return (
    <Shell
      label="parameter"
      kind="param"
      name={name}
      location={data ? `of ${data.name}` : undefined}
      pinned={pinned}
      onPin={onPin}
      onClose={onClose}
      heading={heading}
    >
      {!data ? (
        <p role="status" className="text-muted">
          Loading…
        </p>
      ) : (
        <>
          <p className="gloss-sum">{mine ? <InlineText text={mine.description} /> : "Not described."}</p>
          {params.length > 1 ? (
            <dl className="gloss-params gloss-sec">
              {params.map((p) => (
                <Fragment key={p.name}>
                  <dt className={p.name === name ? "me" : undefined}>{p.name}</dt>
                  <dd className="text-[14.5px] text-muted">
                    <InlineText text={p.description} />
                  </dd>
                </Fragment>
              ))}
            </dl>
          ) : null}
        </>
      )}
    </Shell>
  );
}

/** A line note (07 explanation `notes`). */
export function NoteCard({
  start,
  end,
  text,
  pinned,
  onPin,
  onClose,
}: {
  start: number;
  end: number;
  text: string;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (pinned) heading.current?.focus();
  }, [pinned]);
  return (
    <Shell
      label="note"
      kind="note"
      name={start === end ? `Line ${start}` : `Lines ${start}–${end}`}
      pinned={pinned}
      onPin={onPin}
      onClose={onClose}
      heading={heading}
    >
      <p className="gloss-note">
        <InlineText text={text} />
      </p>
    </Shell>
  );
}

/** A use that may refer to several definitions with the same name (07 "explicit ambiguity"). */
export function CandidatesCard({
  name,
  targets,
  resolution,
  pinned,
  onPin,
  onClose,
  onOpen,
}: {
  name: string;
  targets: { id: string; name: string; kind: string }[];
  resolution: string;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
  onOpen: (id: string) => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (pinned) heading.current?.focus();
  }, [pinned]);
  return (
    <Shell label="one of" kind="ref" name={name} pinned={pinned} onPin={onPin} onClose={onClose} heading={heading}>
      <p className="text-[14.5px] text-muted">
        {targets.length} definitions have this name ({resolution === "index" ? "resolved by type analysis" : "matched by name"}).
      </p>
      <ul className="flex flex-col gap-1">
        {targets.map((t) => (
          <li key={t.id}>
            <button type="button" className={`sym ref ${kindClass(t.kind)} font-mono text-[13.5px]`} onClick={() => onOpen(t.id)}>
              {t.name}
            </button>{" "}
            <span className="text-[14px] text-muted italic">{t.kind}</span>
          </li>
        ))}
      </ul>
    </Shell>
  );
}
