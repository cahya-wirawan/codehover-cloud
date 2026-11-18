import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type TreeEntry } from "../../lib/api/resources";

function Tree({ buildId, path, onOpen }: { buildId: string; path: string; onOpen: (entry: TreeEntry) => void }) {
  const { data } = useQuery({ queryKey: ["tree", buildId, path], queryFn: () => api.tree(buildId, path) });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  return (
    <ul className="pl-3">
      {data?.items.map((entry) =>
        entry.type === "dir" ? (
          <li key={entry.path}>
            <button type="button" aria-expanded={open[entry.path] ?? false} className="text-left"
              onClick={() => setOpen((o) => ({ ...o, [entry.path]: !o[entry.path] }))}>
              {open[entry.path] ? "▾" : "▸"} {entry.name}
            </button>
            {open[entry.path] ? <Tree buildId={buildId} path={entry.path} onOpen={onOpen} /> : null}
          </li>
        ) : (
          <li key={entry.path}>
            <button type="button" className="text-left text-primary disabled:text-muted"
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

function SymbolCard({ buildId, symbolId }: { buildId: string; symbolId: string }) {
  const { data } = useQuery({ queryKey: ["symbol", buildId, symbolId], queryFn: () => api.symbol(buildId, symbolId) });
  if (!data) return <p role="status" className="text-muted">Loading…</p>;
  return (
    <article aria-label={`${data.kind} ${data.name}`} className="rounded-lg border border-border bg-surface p-3">
      <h3 className="font-mono font-medium">{data.name}</h3>
      <p className="text-muted">{data.kind} · {data.path}:{data.start_line}</p>
      {data.signature ? <pre className="mt-2 overflow-x-auto font-mono text-[13px]">{data.signature}</pre> : null}
      <p className="mt-2">{data.card.summary}</p>
      {data.card.params?.length ? (
        <ul className="mt-2 list-disc pl-5">{data.card.params.map((p) => <li key={p.name}><code>{p.name}</code></li>)}</ul>
      ) : null}
      <h4 className="mt-3 font-medium">Uses</h4>
      <ul className="text-muted">
        {data.uses.map((u) => <li key={u.id}>{u.name} · {u.path} · matched by name{u.ambiguous ? " (ambiguous)" : ""}</li>)}
        {data.uses.length === 0 ? <li>None found.</li> : null}
      </ul>
      <h4 className="mt-3 font-medium">Used by</h4>
      <ul className="text-muted">
        {data.used_by.map((u) => <li key={u.id}>{u.name} · {u.path}</li>)}
        {data.used_by.length === 0 ? <li>None found.</li> : null}
      </ul>
    </article>
  );
}

export function CodeViewer({ buildId }: { buildId: string }) {
  const [file, setFile] = useState<TreeEntry | null>(null);
  const [symbolId, setSymbolId] = useState<string | null>(null);
  const view = useQuery({
    queryKey: ["file", buildId, file?.file_id],
    queryFn: () => api.file(buildId, file?.file_id ?? ""),
    enabled: file?.file_id != null,
  });
  return (
    <div className="mt-4 grid gap-4 lg:grid-cols-[248px_1fr_280px]">
      <nav aria-label="Files" className="overflow-auto rounded-lg border border-border bg-surface py-2">
        <Tree buildId={buildId} path="" onOpen={(entry) => { setFile(entry); setSymbolId(null); }} />
      </nav>
      <div className="min-w-0 overflow-x-auto rounded-lg border border-border bg-surface">
        {view.data ? (
          <>
            <p className="border-b border-border px-3 py-2 font-mono text-muted">{view.data.path}</p>
            <pre className="p-3 font-mono text-[13px] leading-[21px]">
              {view.data.lines.map((line, index) => {
                const number = view.data.start_line + index;
                const symbol = view.data.symbols.find((s) => s.start_line === number);
                return (
                  <div key={number} id={`L${number}`}>
                    <span aria-hidden="true" className="inline-block w-10 select-none pr-3 text-right text-muted">{number}</span>
                    {symbol ? (
                      <button type="button" className="text-left underline decoration-dotted"
                        aria-label={`Explain ${symbol.kind} ${symbol.name}`} onClick={() => setSymbolId(symbol.id)}>
                        {line}
                      </button>
                    ) : (
                      line
                    )}
                  </div>
                );
              })}
            </pre>
          </>
        ) : (
          <p className="p-3 text-muted">Choose a file to read.</p>
        )}
      </div>
      <aside aria-label="Definition details">
        {symbolId ? <SymbolCard buildId={buildId} symbolId={symbolId} /> : <p className="text-muted">Select a definition.</p>}
      </aside>
    </div>
  );
}
