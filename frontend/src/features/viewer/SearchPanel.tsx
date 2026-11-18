import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { SearchHit, ViewerSource } from "../../lib/api/resources";

const BASIS: Record<SearchHit["basis"], string> = {
  exact_name: "exact name",
  name_prefix: "name prefix",
  text: "text match",
};

function target(hit: SearchHit, base: string): string {
  if (hit.kind === "guide") return `${base}/guide/${encodeURIComponent(hit.path ?? "")}`;
  const params = new URLSearchParams();
  if (hit.kind === "symbol") {
    params.set("symbol", hit.ref_id);
    if (hit.line) params.set("line", String(hit.line));
  }
  return `${base}/code?${params.toString()}`;
}

/** Build-scoped search (02 S13): `/` focuses the query; nothing is kept in history beyond the URL. */
export function SearchPanel({ source, base }: { source: ViewerSource; base: string }) {
  const [params, setParams] = useSearchParams();
  const [draft, setDraft] = useState(params.get("q") ?? "");
  const query = params.get("q") ?? "";
  const kind = params.get("kind");
  const input = useRef<HTMLInputElement>(null);
  const results = useQuery({
    queryKey: ["search", source.key, query, kind],
    queryFn: () => source.search(query, kind),
    enabled: query.trim().length > 0,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = event.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
      if (event.key === "/" && !typing) {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <section aria-labelledby="search-title" className="mt-4">
      <h2 id="search-title" className="sr-only">Search this build</h2>
      <form
        role="search"
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const next = new URLSearchParams({ q: draft.trim() });
          if (kind) next.set("kind", kind);
          setParams(next);
        }}
      >
        <label className="flex flex-col gap-1">
          Search definitions, files and guide pages
          <input ref={input} value={draft} maxLength={200} onChange={(e) => setDraft(e.target.value)}
            className="h-9 w-80 rounded-lg border border-input-border bg-surface px-2" />
        </label>
        <label className="flex flex-col gap-1">
          Kind
          <select value={kind ?? ""} onChange={(e) => setParams(new URLSearchParams({ q: query, ...(e.target.value ? { kind: e.target.value } : {}) }))}
            className="field">
            <option value="">All</option>
            <option value="symbol">Definitions</option>
            <option value="file">Files</option>
            <option value="guide">Guide pages</option>
          </select>
        </label>
        <button type="submit" className="btn">Search</button>
      </form>
      {results.data ? (
        results.data.items.length ? (
          <ol className="mt-4 panel divide-y divide-border" aria-label="Results">
            {results.data.items.map((hit) => (
              <li key={`${hit.kind}-${hit.ref_id}`} className="p-3">
                <Link to={target(hit, base)} className="font-mono text-primary">{hit.name}</Link>{" "}
                <span className="text-sm text-muted">
                  {hit.kind === "symbol" ? hit.symbol_kind : hit.kind}
                  {hit.path ? ` · ${hit.path}` : ""}
                  {hit.line ? `:${hit.line}` : ""} · {BASIS[hit.basis]}
                </span>
                {hit.snippet ? <p className="mt-1 text-sm">{hit.snippet}</p> : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-4 text-muted">No results. Try an identifier such as a function or class name.</p>
        )
      ) : null}
    </section>
  );
}
