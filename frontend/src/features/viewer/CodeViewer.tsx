import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FileCode2, FileX2, Folder, FolderOpen } from "lucide-react";
import { Highlight, type Token } from "prism-react-renderer";
import { Popover } from "radix-ui";
import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { useSearchParams } from "react-router";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import type { Related, TreeEntry, ViewerSource } from "../../lib/api/resources";
import { cn } from "../../lib/cn";
import { useMediaQuery } from "../../lib/useMediaQuery";
import { SymbolCard } from "./SymbolCard";

const WINDOW = 500; // the API returns at most 500 lines per request (06)
const HOVER_DELAY_MS = 200; // 02 "Hover card": pointer open delay
const LEAVE_DELAY_MS = 150; // time to move the pointer from the name into the card
const NO_THEME = { plain: {}, styles: [] }; // colours come from CSS classes, so both themes work

const LANGUAGES: Record<string, string> = {
  py: "python",
  ts: "tsx",
  tsx: "tsx",
  js: "jsx",
  jsx: "jsx",
  mjs: "jsx",
  json: "json",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
  go: "go",
  rs: "rust",
};

function languageOf(path: string): string {
  return LANGUAGES[path.split(".").pop()?.toLowerCase() ?? ""] ?? "plain";
}

/** The first whole-word occurrence of a definition's name on its line, or null (unusual syntax); the
 * source text is never altered. */
function nameRange(text: string, name: string): [number, number] | null {
  if (name === "") return null;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(^|[^\\w$])${escaped}(?![\\w$])`).exec(text);
  if (!match) return null;
  const at = match.index + (match[1] ?? "").length;
  return [at, at + name.length];
}

function tokenClass(token: Token): string | undefined {
  const types = token.types.filter((t) => t !== "plain");
  return types.length ? `token ${types.join(" ")}` : undefined;
}

/** A line's highlighted tokens, with the definition's name replaced by its control. */
function renderTokens(tokens: Token[], range: [number, number] | null, name: (cls: string | undefined) => ReactNode) {
  const out: ReactNode[] = [];
  let position = 0;
  tokens.forEach((token, index) => {
    const start = position;
    const end = position + token.content.length;
    position = end;
    const cls = tokenClass(token);
    if (!range || end <= range[0] || start >= range[1]) {
      out.push(
        <span key={index} className={cls}>
          {token.content}
        </span>,
      );
      return;
    }
    const before = Math.max(range[0], start) - start;
    const after = Math.min(range[1], end) - start;
    if (before > 0) out.push(<span key={`${index}a`} className={cls}>{token.content.slice(0, before)}</span>);
    if (start <= range[0]) out.push(<Fragment key={`${index}n`}>{name(cls)}</Fragment>);
    if (after < token.content.length) out.push(<span key={`${index}b`} className={cls}>{token.content.slice(after)}</span>);
  });
  return out;
}

function Tree({
  source,
  path,
  depth,
  onOpen,
  current,
}: {
  source: ViewerSource;
  path: string;
  depth: number;
  onOpen: (entry: TreeEntry) => void;
  current: string | null;
}) {
  const { data, isPending } = useQuery({ queryKey: ["tree", source.key, path], queryFn: () => source.tree(path) });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (isPending) {
    return (
      <div className="flex flex-col gap-2 px-3 py-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-4 w-24" />
      </div>
    );
  }
  const indent = { paddingLeft: `${8 + depth * 14}px` };
  return (
    <ul>
      {data?.items.map((entry) =>
        entry.type === "dir" ? (
          <li key={entry.path}>
            <button
              type="button"
              aria-expanded={open[entry.path] ?? false}
              style={indent}
              className="flex h-7 w-full items-center gap-1.5 pr-2 text-left text-[13px] hover:bg-surface-muted"
              onClick={() => setOpen((o) => ({ ...o, [entry.path]: !o[entry.path] }))}
            >
              {open[entry.path] ? (
                <ChevronDown aria-hidden="true" className="size-3.5 text-muted" />
              ) : (
                <ChevronRight aria-hidden="true" className="size-3.5 text-muted" />
              )}
              {open[entry.path] ? (
                <FolderOpen aria-hidden="true" className="size-4 text-muted" />
              ) : (
                <Folder aria-hidden="true" className="size-4 text-muted" />
              )}
              <span className="truncate">{entry.name}</span>
            </button>
            {open[entry.path] ? (
              <Tree source={source} path={entry.path} depth={depth + 1} onOpen={onOpen} current={current} />
            ) : null}
          </li>
        ) : (
          <li key={entry.path}>
            <button
              type="button"
              style={{ paddingLeft: `${28 + depth * 14}px` }}
              className={cn(
                "flex h-7 w-full items-center gap-1.5 pr-2 text-left text-[13px] hover:bg-surface-muted disabled:text-muted disabled:hover:bg-transparent",
                current === entry.file_id && "bg-primary/10 font-medium text-primary hover:bg-primary/10",
              )}
              aria-current={current === entry.file_id ? "page" : undefined}
              disabled={entry.status !== "selected" && entry.status !== "unsupported"}
              title={entry.reason ?? undefined}
              onClick={() => onOpen(entry)}
            >
              {entry.status === "blocked" || entry.status === "excluded" ? (
                <FileX2 aria-hidden="true" className="size-4 shrink-0" />
              ) : (
                <FileCode2 aria-hidden="true" className="size-4 shrink-0 text-muted" />
              )}
              <span className="truncate">{entry.name}</span>
              {entry.status === "blocked" || entry.status === "excluded" ? (
                <span className="ml-auto text-xs"> (excluded)</span>
              ) : null}
            </button>
          </li>
        ),
      )}
    </ul>
  );
}

function Pane({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("flex h-full min-h-0 flex-col", className)}>
      {children}
    </section>
  );
}

function ResizeHandle() {
  return (
    <Separator className="group relative w-px bg-border outline-none data-[separator=active]:bg-primary">
      <span className="absolute inset-y-0 -left-1 -right-1 group-hover:bg-primary/20 group-focus-visible:bg-primary/30" />
    </Separator>
  );
}

/** Three panes: tree, source, outline (02 S11). File, line range and pinned symbol live in the URL; the
 * definition card floats at its name (02 "Hover card"). */
export function CodeViewer({ source }: { source: ViewerSource }) {
  const [params, setParams] = useSearchParams();
  const wide = useMediaQuery("(min-width: 1024px)");
  const fileId = params.get("file");
  const line = Number(params.get("line") ?? "0") || null;
  const end = Number(params.get("end") ?? "0") || line;
  const pinned = params.get("symbol");
  const [preview, setPreview] = useState<string | null>(null);
  const openTimer = useRef<number | undefined>(undefined);
  const closeTimer = useRef<number | undefined>(undefined);
  const triggers = useRef(new Map<string, HTMLButtonElement>());
  const start = line ? Math.max(1, line - 100) : 1;
  const [windowStart, setWindowStart] = useState<number | null>(null);
  const from = windowStart ?? start;
  const view = useQuery({
    queryKey: ["file", source.key, fileId, from],
    queryFn: () => source.file(fileId ?? "", from),
    enabled: fileId !== null,
  });
  const shown = pinned ?? preview;
  // The card's anchor is whichever name is shown; floating-ui follows it through scrolling. The element is
  // looked up when floating-ui measures (after commit), never during render.
  const anchor = useMemo(
    () => ({
      current: {
        getBoundingClientRect: () => triggers.current.get(shown ?? "")?.getBoundingClientRect() ?? new DOMRect(),
        get contextElement() {
          return triggers.current.get(shown ?? "");
        },
      },
    }),
    [shown],
  );

  useEffect(() => {
    if (line && view.data) document.getElementById(`L${line}`)?.scrollIntoView?.({ block: "center" });
  }, [line, view.data]);
  useEffect(
    () => () => {
      window.clearTimeout(openTimer.current);
      window.clearTimeout(closeTimer.current);
    },
    [],
  );

  const update = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value === null) merged.delete(key);
      else merged.set(key, value);
    }
    setParams(merged);
  };
  const pin = (id: string) => {
    window.clearTimeout(openTimer.current);
    setPreview(null);
    update({ symbol: id });
  };
  const unpin = () => {
    const trigger = pinned ? triggers.current.get(pinned) : undefined;
    update({ symbol: null });
    trigger?.focus();
  };
  const follow = (r: Related) => {
    if (!r.file_id) return;
    setWindowStart(null);
    setPreview(null);
    update({ file: r.file_id, line: String(r.start_line ?? 1), end: null, symbol: r.id });
  };
  const hoverName = (id: string) => {
    window.clearTimeout(closeTimer.current);
    window.clearTimeout(openTimer.current);
    if (pinned) return; // a pinned card stays until it is closed
    openTimer.current = window.setTimeout(() => setPreview(id), preview ? 0 : HOVER_DELAY_MS);
  };
  const leave = () => {
    window.clearTimeout(openTimer.current);
    closeTimer.current = window.setTimeout(() => setPreview(null), LEAVE_DELAY_MS);
  };

  const data = view.data;
  const last = data ? data.start_line + data.lines.length - 1 : 0;
  const total = data?.line_count ?? last;
  const shownSymbol = data?.symbols.find((s) => s.id === shown);

  const tree = (
    <nav aria-label="Files" className="h-full overflow-auto py-2">
      <Tree
        source={source}
        path=""
        depth={0}
        current={fileId}
        onOpen={(entry) => {
          setWindowStart(null);
          setPreview(null);
          update({ file: entry.file_id, line: null, end: null, symbol: null });
        }}
      />
    </nav>
  );

  const code = (
    <Pane title="Source">
      {data ? (
        <>
          <div className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-border px-4">
            <p className="min-w-0 truncate font-mono text-[13px]">{data.path}</p>
            <p className="shrink-0 text-xs text-muted">
              Lines {data.start_line}–{last}
              {total ? ` of ${total}` : ""}
            </p>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {data.start_line > 1 ? (
              <Button
                size="sm"
                className="m-3"
                onClick={() => setWindowStart(Math.max(1, data.start_line - WINDOW))}
              >
                Earlier lines
              </Button>
            ) : null}
            <Highlight code={data.lines.join("\n")} language={languageOf(data.path)} theme={NO_THEME}>
              {({ tokens }) => (
                <pre className="min-w-max py-3 font-mono text-[13px] leading-[21px]">
                  {tokens.map((lineTokens, index) => {
                    const number = data.start_line + index;
                    const text = data.lines[index] ?? "";
                    const symbol = data.symbols.find((s) => s.start_line === number);
                    const highlighted = line !== null && end !== null && number >= line && number <= end;
                    // Only the identifier is the control, so its visible text is its name (WCAG 2.5.3).
                    const range = symbol ? nameRange(text, symbol.name) : null;
                    const label = symbol ? `Explain ${symbol.kind} ${symbol.name}` : "";
                    const control = (cls: string | undefined, content: ReactNode) =>
                      symbol ? (
                        <button
                          type="button"
                          ref={(el) => {
                            if (el) triggers.current.set(symbol.id, el);
                          }}
                          aria-label={range ? label : `${text.trim()}: ${label}`}
                          aria-expanded={shown === symbol.id}
                          aria-haspopup="dialog"
                          onPointerEnter={() => hoverName(symbol.id)}
                          onPointerLeave={leave}
                          onClick={() => pin(symbol.id)}
                          className={cn(
                            cls,
                            "cursor-pointer rounded-[3px] text-left underline decoration-annotation/60 decoration-dotted decoration-[1.5px] underline-offset-[5px] hover:bg-primary/10 hover:decoration-annotation",
                            shown === symbol.id && "bg-primary/10 decoration-annotation decoration-solid",
                          )}
                        >
                          {content}
                        </button>
                      ) : null;
                    return (
                      <div
                        key={number}
                        id={`L${number}`}
                        className={cn(
                          "flex pr-6",
                          highlighted && "bg-primary/[0.07] shadow-[inset_2px_0_0_var(--primary)]",
                        )}
                      >
                        <span
                          aria-hidden="true"
                          className="sticky left-0 inline-block w-14 shrink-0 bg-surface pr-4 text-right text-muted select-none"
                        >
                          {number}
                        </span>
                        <span className="whitespace-pre">
                          {symbol && !range
                            ? control(undefined, text)
                            : renderTokens(lineTokens, range, (cls) => control(cls, symbol?.name))}
                        </span>
                      </div>
                    );
                  })}
                </pre>
              )}
            </Highlight>
            {total && last < total ? (
              <Button size="sm" className="m-3" onClick={() => setWindowStart(last + 1)}>
                Later lines
              </Button>
            ) : null}
          </div>
        </>
      ) : fileId ? (
        <div className="flex flex-col gap-2 p-4">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      ) : (
        <div className="grid h-full place-items-center p-8 text-center text-muted">
          <p>
            Choose a file to read. Underlined names open an explanation when you hover them; click to keep it open.
          </p>
        </div>
      )}
    </Pane>
  );

  const outline = (
    <Pane title="Definition details" className="overflow-auto">
      <div className="flex flex-col gap-4 p-4">
        {data?.explanation?.summary ? (
          <div>
            <h2 className="text-xs font-medium text-muted">About this file</h2>
            <p className="mt-1.5 leading-[22px]">{data.explanation.summary.split("\n\n")[0]}</p>
            <p className="mt-1.5 text-xs text-muted">
              {data.explanation.status === "generated"
                ? "AI-generated; check the cited source"
                : "Partly derived from the source only"}
            </p>
          </div>
        ) : null}
        <div>
          <h2 className="text-xs font-medium text-muted">Definitions</h2>
          {data?.symbols.length ? (
            <ul className="mt-1.5 flex flex-col">
              {data.symbols.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => {
                      update({ line: String(s.start_line), end: String(s.end_line ?? s.start_line), symbol: s.id });
                    }}
                    className={cn(
                      "flex h-7 w-full items-center gap-2 rounded-[4px] px-1.5 text-left hover:bg-surface-muted",
                      pinned === s.id && "bg-primary/10",
                    )}
                  >
                    <span className="w-14 shrink-0 text-xs text-muted">{s.kind}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[13px]">{s.name}</span>
                    <span className="text-xs text-muted">{s.start_line}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1.5 text-muted">{data ? "No definitions in this file." : "Hover or select a definition."}</p>
          )}
        </div>
      </div>
    </Pane>
  );

  return (
    <div className="mt-4 overflow-hidden rounded-[var(--radius-overlay)] border border-border bg-surface">
      <Popover.Root
        key={shown ?? "none"}
        open={shownSymbol !== undefined} // its name is on screen to anchor to
        onOpenChange={(open) => {
          if (open) return;
          if (pinned) unpin();
          else setPreview(null);
        }}
      >
        <Popover.Anchor virtualRef={anchor} />
        <Popover.Portal>
          <Popover.Content
            side="bottom"
            align="start"
            sideOffset={6}
            collisionPadding={12}
            aria-label={shownSymbol ? `${shownSymbol.kind} ${shownSymbol.name}` : "Definition"}
            onOpenAutoFocus={(event) => event.preventDefault()} // a preview never steals focus
            onCloseAutoFocus={(event) => event.preventDefault()} // unpin() returns focus to the name
            onInteractOutside={(event) => {
              if (pinned) event.preventDefault(); // a pinned card stays while the reader scrolls or selects
            }}
            onPointerEnter={() => window.clearTimeout(closeTimer.current)}
            onPointerLeave={() => {
              if (!pinned) leave();
            }}
            className="z-40 w-[min(480px,calc(100vw-24px))] overflow-hidden rounded-[var(--radius-overlay)] border border-border bg-surface shadow-[var(--shadow-overlay)]"
          >
            {shown ? (
              <SymbolCard
                source={source}
                symbolId={shown}
                pinned={pinned === shown}
                onPin={() => pin(shown)}
                onClose={unpin}
                onFollow={follow}
              />
            ) : null}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      {wide ? (
        // The group sizes itself to its parent (inline height: 100%), so the parent fixes the height.
        <div className="h-[calc(100dvh-15rem)] min-h-[480px]">
        <Group orientation="horizontal">
          <Panel id="tree" defaultSize={248} minSize={180} maxSize={420} className="h-full overflow-hidden bg-surface">
            {tree}
          </Panel>
          <ResizeHandle />
          <Panel id="code" minSize={360} className="h-full overflow-hidden">
            {code}
          </Panel>
          <ResizeHandle />
          <Panel id="outline" defaultSize={280} minSize={220} maxSize={460} className="h-full overflow-hidden">
            {outline}
          </Panel>
        </Group>
        </div>
      ) : (
        <div className="flex flex-col divide-y divide-border">
          <details className="group">
            <summary className="flex h-10 cursor-pointer items-center gap-2 px-4 text-[13px] font-medium">
              <Folder aria-hidden="true" className="size-4 text-muted" /> Files
              <Badge className="ml-auto">{data?.path ?? "none open"}</Badge>
            </summary>
            <div className="max-h-[50vh] overflow-auto">{tree}</div>
          </details>
          <div className="h-[70vh]">{code}</div>
          {outline}
        </div>
      )}
    </div>
  );
}
