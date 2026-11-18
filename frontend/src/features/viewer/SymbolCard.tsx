import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Bot, FileCode2, Link2, Pin, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { Related, SymbolView, ViewerSource } from "../../lib/api/resources";

type Card = SymbolView["card"] & { returns?: string; details?: string };

function provenance(card: SymbolView["card"]): { label: string; model: boolean } {
  const generator = (card as { provenance?: { generator?: string } }).provenance?.generator;
  if (generator === "model") return { label: "AI-generated; check the cited source.", model: true };
  if (generator === "replay-fallback")
    return { label: "No valid model explanation; derived from the source only.", model: false };
  return { label: "Derived from the source only.", model: false };
}

function Relations({ title, items, onFollow }: { title: string; items: Related[]; onFollow: (r: Related) => void }) {
  return (
    <div>
      <h4 className="text-xs font-medium text-muted">{title}</h4>
      {items.length ? (
        <ul className="mt-1.5 flex flex-col gap-1">
          {items.map((r) => (
            <li key={r.id} className="flex min-w-0 items-baseline gap-2">
              <button
                type="button"
                className="shrink-0 rounded-[4px] font-mono text-[13px] text-primary underline-offset-2 hover:underline disabled:text-muted"
                onClick={() => onFollow(r)}
                disabled={!r.file_id}
              >
                {r.name}
              </button>
              <span className="min-w-0 truncate text-xs text-muted">
                {r.path} · matched by name
                {r.ambiguous ? " (ambiguous: one of several definitions with this name)" : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1.5 text-xs text-muted">None found.</p>
      )}
    </div>
  );
}

/** The hover card's content (02 "Hover card"): signature strip, explanation, parameters, provenance and
 * relations. In the pinned state it is a non-modal dialog whose heading takes focus. */
export function SymbolCard({
  source,
  symbolId,
  pinned,
  onPin,
  onClose,
  onFollow,
}: {
  source: ViewerSource;
  symbolId: string;
  pinned: boolean;
  onPin: () => void;
  onClose: () => void;
  onFollow: (r: Related) => void;
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
  const origin = provenance(data.card);
  return (
    <article className="flex max-h-[60vh] flex-col">
      <header className="flex items-start gap-3 border-b border-border px-4 pt-3 pb-2.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Badge tone="primary">{data.kind}</Badge>
            <h3 ref={heading} tabIndex={-1} className="truncate font-mono text-[14px] font-medium outline-none">
              {data.name}
            </h3>
          </div>
          <p className="mt-1 flex items-center gap-1 truncate text-xs text-muted">
            <FileCode2 aria-hidden="true" className="size-3.5 shrink-0" />
            {data.path}:{data.start_line}
          </p>
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
      <div className="flex flex-col gap-3 overflow-y-auto px-4 py-3">
        {data.signature ? (
          <pre className="overflow-x-auto rounded-[var(--radius-control)] bg-surface-muted px-2.5 py-2 font-mono text-[12.5px] leading-5">
            {data.signature}
          </pre>
        ) : null}
        {card.summary ? <p className="leading-[22px]">{card.summary}</p> : null}
        {card.details ? <p className="whitespace-pre-wrap leading-[22px] text-muted">{card.details}</p> : null}
        {card.params?.length ? (
          <dl className="flex flex-col gap-1">
            {card.params.map((p) => (
              <div key={p.name} className="grid grid-cols-[auto_1fr] gap-x-3">
                <dt className="font-mono text-[13px]">{p.name}</dt>
                <dd className="text-muted">{p.description}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {card.returns ? (
          <p>
            <span className="font-medium">Returns </span>
            <span className="text-muted">{card.returns}</span>
          </p>
        ) : null}
        {card.notes ? <p className="border-l-2 border-warning/60 pl-2.5 text-muted">{card.notes}</p> : null}
        <p className="flex items-center gap-1.5 text-xs text-muted">
          {origin.model ? <Bot aria-hidden="true" className="size-3.5" /> : <Link2 aria-hidden="true" className="size-3.5" />}
          {origin.label}
        </p>
        <div className="grid gap-3 border-t border-border pt-3 sm:grid-cols-2">
          <Relations title="Uses" items={data.uses} onFollow={onFollow} />
          <Relations title="Used by" items={data.used_by} onFollow={onFollow} />
        </div>
      </div>
      {pinned ? null : (
        <p className="flex items-center gap-1 border-t border-border px-4 py-1.5 text-xs text-muted">
          <ArrowUpRight aria-hidden="true" className="size-3" />
          Click the name or press Enter to keep this card open
        </p>
      )}
    </article>
  );
}
