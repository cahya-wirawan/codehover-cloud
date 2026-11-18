import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import type { Related, SymbolView, TreeEntry, ViewerSource } from "../../lib/api/resources";

const WINDOW = 500; // the API returns at most 500 lines per request (06)
const HOVER_DELAY_MS = 200; // 02 "Component contracts"

/** Split a definition line around the first whole-word occurrence of its name, or null when the name is
 * not on the line (unusual syntax); source text is never altered. */
function splitAtName(text: string, name: string): [string, string] | null {
  if (name === "") return null;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(^|[^\\w$])${escaped}(?![\\w$])`).exec(text);
  if (!match) return null;
  const at = match.index + (match[1] ?? "").length;
  return [text.slice(0, at), text.slice(at + name.length)];
}

function Tree({ source, path, onOpen, current }: { source: ViewerSource; path: string; onOpen: (entry: TreeEntry) => void; current: string | null }) {
  const { data } = useQuery({ queryKey: ["tree", source.key, path], queryFn: () => source.tree(path) });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  return (
    <ul className="pl-3">
      {data?.items.map((entry) =>
        entry.type === "dir" ? (
          <li key={entry.path}>
            <button type="button" aria-expanded={open[entry.path] ?? false} className="text-left"
              onClick={() => setOpen((o) => ({ ...o, [entry.path]: !o[entry.path] }))}>
              <span aria-hidden="true">{open[entry.path] ? "▾" : "▸"}</span> {entry.name}
            </button>
            {open[entry.path] ? <Tree source={source} path={entry.path} onOpen={onOpen} current={current} /> : null}
          </li>
        ) : (
          <li key={entry.path}>
            <button type="button" className="text-left text-primary disabled:text-muted"
              aria-current={current === entry.file_id ? "page" : undefined}
              disabled={entry.status !== "selected" && entry.status !== "unsupported"}
              title={entry.reason ?? undefined} onClick={() => onOpen(entry)}>
              {entry.name}
              {entry.status === "blocked" || entry.status === "excluded" ? " (excluded)" : ""}
            </button>
          </li>
        ),
      )}
    </ul>
  );
}

function provenanceLabel(card: SymbolView["card"]): string {
  const generator = (card as { provenance?: { generator?: string } }).provenance?.generator;
  if (generator === "model") return "AI-generated; check the cited source.";
  if (generator === "replay-fallback") return "No valid model explanation; derived from the source only.";
  return "Derived from the source only (replay).";
}

function Relations({ title, items, onFollow }: { title: string; items: Related[]; onFollow: (r: Related) => void }) {
  return (
    <>
      <h4 className="mt-3 font-medium">{title}</h4>
      <ul className="text-muted">
        {items.map((r) => (
          <li key={`${r.id}`}>
            <button type="button" className="text-left text-primary underline-offset-2 hover:underline"
              onClick={() => onFollow(r)} disabled={!r.file_id}>
              {r.name}
            </button>{" "}
            · {r.path} · matched by name{r.ambiguous ? " (ambiguous: one of several definitions with this name)" : ""}
          </li>
        ))}
        {items.length === 0 ? <li>None found.</li> : null}
      </ul>
    </>
  );
}

function SymbolCard({
  source, symbolId, pinned, onClose, onFollow,
}: { source: ViewerSource; symbolId: string; pinned: boolean; onClose: () => void; onFollow: (r: Related) => void }) {
  const { data } = useQuery({ queryKey: ["symbol", source.key, symbolId], queryFn: () => source.symbol(symbolId) });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (pinned && data) heading.current?.focus();
  }, [pinned, data]);
  if (!data) return <p role="status" className="text-muted">Loading…</p>;
  const card = data.card as SymbolView["card"] & { returns?: string; details?: string };
  return (
    <article
      role={pinned ? "dialog" : undefined}
      aria-modal={pinned ? false : undefined}
      aria-label={`${data.kind} ${data.name}`}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
      className="max-h-[60vh] max-w-[480px] overflow-auto rounded-lg border border-border bg-surface p-3"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 ref={heading} tabIndex={-1} className="font-mono font-medium">{data.name}</h3>
        {pinned ? (
          <button type="button" onClick={onClose} className="rounded border border-border px-2 text-sm">Close</button>
        ) : null}
      </div>
      <p className="text-muted">{data.kind} · {data.path}:{data.start_line}</p>
      {data.signature ? <pre className="mt-2 overflow-x-auto font-mono text-[13px]">{data.signature}</pre> : null}
      <p className="mt-2">{card.summary}</p>
      {card.details ? <p className="mt-2 whitespace-pre-wrap text-muted">{card.details}</p> : null}
      {card.params?.length ? (
        <dl className="mt-2">
          {card.params.map((p) => (
            <div key={p.name}><dt className="inline font-mono">{p.name}</dt> <dd className="inline text-muted">— {p.description}</dd></div>
          ))}
        </dl>
      ) : null}
      {card.returns ? <p className="mt-2"><span className="font-medium">Returns:</span> {card.returns}</p> : null}
      {card.notes ? <p className="mt-2 text-muted">{card.notes}</p> : null}
      <p className="mt-2 text-sm text-muted">{provenanceLabel(data.card)}</p>
      <Relations title="Uses" items={data.uses} onFollow={onFollow} />
      <Relations title="Used by" items={data.used_by} onFollow={onFollow} />
    </article>
  );
}

/** Three panes: tree, source, details (02 S11). File, line range and pinned symbol live in the URL. */
export function CodeViewer({ source }: { source: ViewerSource }) {
  const [params, setParams] = useSearchParams();
  const fileId = params.get("file");
  const line = Number(params.get("line") ?? "0") || null;
  const end = Number(params.get("end") ?? "0") || line;
  const pinned = params.get("symbol");
  const [preview, setPreview] = useState<string | null>(null);
  const hoverTimer = useRef<number | undefined>(undefined);
  const triggers = useRef(new Map<string, HTMLButtonElement>());
  const start = line ? Math.max(1, line - 100) : 1;
  const [windowStart, setWindowStart] = useState<number | null>(null);
  const from = windowStart ?? start;
  const view = useQuery({
    queryKey: ["file", source.key, fileId, from],
    queryFn: () => source.file(fileId ?? "", from),
    enabled: fileId !== null,
  });

  useEffect(() => {
    if (line && view.data) document.getElementById(`L${line}`)?.scrollIntoView?.({ block: "center" });
  }, [line, view.data]);

  const update = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value === null) merged.delete(key);
      else merged.set(key, value);
    }
    setParams(merged);
  };
  const pin = (id: string) => update({ symbol: id });
  const unpin = () => {
    const trigger = pinned ? triggers.current.get(pinned) : undefined;
    update({ symbol: null });
    trigger?.focus();
  };
  const follow = (r: Related) => {
    if (!r.file_id) return;
    setWindowStart(null);
    update({ file: r.file_id, line: String(r.start_line ?? 1), end: null, symbol: r.id });
  };
  const shown = pinned ?? preview;
  const last = view.data ? view.data.start_line + view.data.lines.length - 1 : 0;
  const total = view.data?.line_count ?? last;

  return (
    <div className="mt-4 grid gap-4 lg:grid-cols-[248px_1fr_280px]">
      <nav aria-label="Files" className="max-h-[70vh] overflow-auto rounded-lg border border-border bg-surface py-2">
        <Tree source={source} path="" current={fileId}
          onOpen={(entry) => { setWindowStart(null); update({ file: entry.file_id, line: null, end: null, symbol: null }); }} />
      </nav>
      <section aria-label="Source" className="min-w-0 overflow-x-auto rounded-lg border border-border bg-surface">
        {view.data ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
              <p className="font-mono text-muted">{view.data.path}</p>
              <p className="text-sm text-muted">Lines {view.data.start_line}–{last}{total ? ` of ${total}` : ""}</p>
            </div>
            {view.data.explanation?.summary ? (
              <p className="border-b border-border px-3 py-2 text-sm">
                {view.data.explanation.summary.split("\n\n")[0]}{" "}
                <span className="text-muted">({view.data.explanation.status === "generated" ? "AI-generated; check the cited source" : "partly derived from the source only"})</span>
              </p>
            ) : null}
            {view.data.start_line > 1 ? (
              <button type="button" className="m-2 rounded border border-border px-2 text-sm"
                onClick={() => setWindowStart(Math.max(1, view.data.start_line - WINDOW))}>Earlier lines</button>
            ) : null}
            <pre className="p-3 font-mono text-[13px] leading-[21px]">
              {view.data.lines.map((text, index) => {
                const number = view.data.start_line + index;
                const symbol = view.data.symbols.find((s) => s.start_line === number);
                const highlighted = line !== null && end !== null && number >= line && number <= end;
                // Only the identifier is the control, so its visible text is part of its name (WCAG 2.5.3).
                const parts = symbol ? splitAtName(text, symbol.name) : null;
                const label = symbol ? `Explain ${symbol.kind} ${symbol.name}` : "";
                return (
                  <div key={number} id={`L${number}`} className={highlighted ? "bg-surface-muted" : undefined}>
                    <span aria-hidden="true" className="inline-block w-12 select-none pr-3 text-right text-muted">{number}</span>
                    {symbol ? (
                        <>
                          {parts?.[0]}
                          <button type="button" className="text-left underline decoration-dotted underline-offset-4"
                            ref={(el) => { if (el) triggers.current.set(symbol.id, el); }}
                            aria-label={parts ? label : `${text.trim()}: ${label}`}
                            aria-expanded={pinned === symbol.id}
                            onMouseEnter={() => { hoverTimer.current = window.setTimeout(() => setPreview(symbol.id), HOVER_DELAY_MS); }}
                            onMouseLeave={() => window.clearTimeout(hoverTimer.current)}
                            onClick={() => pin(symbol.id)}>
                            {parts ? symbol.name : text}
                          </button>
                          {parts?.[1]}
                        </>
                    ) : (
                      text
                    )}
                  </div>
                );
              })}
            </pre>
            {total && last < total ? (
              <button type="button" className="m-2 rounded border border-border px-2 text-sm"
                onClick={() => setWindowStart(last + 1)}>Later lines</button>
            ) : null}
          </>
        ) : (
          <p className="p-3 text-muted">Choose a file to read.</p>
        )}
      </section>
      <aside aria-label="Definition details">
        {shown ? (
          <SymbolCard source={source} symbolId={shown} pinned={pinned === shown} onClose={unpin} onFollow={follow} />
        ) : (
          <p className="text-muted">Hover or select a definition.</p>
        )}
      </aside>
    </div>
  );
}
