import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FileCode2, FileX2, Folder, FolderOpen } from "lucide-react";
import { Highlight } from "prism-react-renderer";
import { Popover } from "radix-ui";
import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { useSearchParams } from "react-router";

import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import type { FileView, Related, TreeEntry, ViewerSource } from "../../lib/api/resources";
import { cn } from "../../lib/cn";
import { useMediaQuery } from "../../lib/useMediaQuery";
import { type Control, kindClass, nameRange, parameterUses, referenceRanges, renderLine, withoutOverlaps } from "./codeLine";
import { CandidatesCard, InlineText, NoteCard, ParamCard, SymbolCard } from "./SymbolCard";

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
const LANGUAGE_NAMES: Record<string, string> = {
  python: "Python",
  tsx: "TypeScript",
  jsx: "JavaScript",
  json: "JSON",
  markdown: "Markdown",
  yaml: "YAML",
  go: "Go",
  rust: "Rust",
};

function languageOf(path: string): string {
  return LANGUAGES[path.split(".").pop()?.toLowerCase() ?? ""] ?? "plain";
}

/** What a card shows: a definition, a use of one (one or several candidates), a parameter or a note. */
type Shown =
  | { type: "def"; symbolId: string }
  | { type: "ref"; symbolId: string }
  | { type: "refs"; name: string; targets: { id: string; name: string; kind: string }[]; resolution: string }
  | { type: "param"; ownerId: string; name: string }
  | { type: "note"; start: number; end: number; text: string };

interface Note {
  start: number;
  end: number;
  text: string;
}

function notesOf(data: FileView | undefined): Note[] {
  return (data?.explanation?.chunks ?? [])
    .flatMap((c) => c.notes)
    .map((n) => ({ start: n.start_line, end: n.end_line, text: n.text }))
    .sort((a, b) => a.start - b.start || a.end - b.end);
}

function Tree({
  source,
  path,
  depth,
  onOpen,
  current,
  currentPath,
}: {
  source: ViewerSource;
  path: string;
  depth: number;
  onOpen: (entry: TreeEntry) => void;
  current: string | null;
  currentPath: string | null;
}) {
  const { data, isPending } = useQuery({ queryKey: ["tree", source.key, path], queryFn: () => source.tree(path) });
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  // Folders holding the open file start open; the reader's own choice wins.
  const isOpen = (dir: string) => toggled[dir] ?? currentPath?.startsWith(`${dir}/`) ?? false;
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
              aria-expanded={isOpen(entry.path)}
              style={indent}
              className="flex h-7 w-full items-center gap-1.5 pr-2 text-left text-muted hover:bg-surface-muted"
              onClick={() => setToggled((o) => ({ ...o, [entry.path]: !isOpen(entry.path) }))}
            >
              {isOpen(entry.path) ? (
                <ChevronDown aria-hidden="true" className="size-3.5" />
              ) : (
                <ChevronRight aria-hidden="true" className="size-3.5" />
              )}
              {isOpen(entry.path) ? (
                <FolderOpen aria-hidden="true" className="size-4" />
              ) : (
                <Folder aria-hidden="true" className="size-4" />
              )}
              <span className="truncate">{entry.name}</span>
            </button>
            {isOpen(entry.path) ? (
              <Tree
                source={source}
                path={entry.path}
                depth={depth + 1}
                onOpen={onOpen}
                current={current}
                currentPath={currentPath}
              />
            ) : null}
          </li>
        ) : (
          <li key={entry.path}>
            <button
              type="button"
              style={{ paddingLeft: `${28 + depth * 14}px` }}
              className={cn(
                "flex h-7 w-full items-center gap-1.5 pr-2 text-left hover:bg-surface-muted disabled:text-muted disabled:hover:bg-transparent",
                current === entry.file_id && "bg-surface font-semibold shadow-[inset_2px_0_0_var(--foreground)]",
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

function FilePath({ path }: { path: string }) {
  const cut = path.lastIndexOf("/") + 1;
  return (
    <h2 className="file-path">
      {cut ? <span className="dir">{path.slice(0, cut)}</span> : null}
      {path.slice(cut)}
    </h2>
  );
}

/** Three panes: files, source, outline (02 S11). File, line range, pinned definition and the notes mode
 * live in the URL. Every definition, every use of one and every documented parameter opens a card that
 * floats at the name, and so does every noted line (02 "Hover card"). */
export function CodeViewer({ source, base }: { source: ViewerSource; base?: string | undefined }) {
  const [params, setParams] = useSearchParams();
  const wide = useMediaQuery("(min-width: 1024px)");
  const fileId = params.get("file");
  const line = Number(params.get("line") ?? "0") || null;
  const end = Number(params.get("end") ?? "0") || line;
  const pinnedSymbol = params.get("symbol");
  const inlineNotes = params.get("notes") === "inline";
  const [preview, setPreview] = useState<string | null>(null);
  const [pinnedLocal, setPinnedLocal] = useState<string | null>(null);
  const openTimer = useRef<number | undefined>(undefined);
  const closeTimer = useRef<number | undefined>(undefined);
  const triggers = useRef(new Map<string, HTMLElement>());
  const start = line ? Math.max(1, line - 100) : 1;
  const [windowStart, setWindowStart] = useState<number | null>(null);
  const from = windowStart ?? start;
  const view = useQuery({
    queryKey: ["file", source.key, fileId, from],
    queryFn: () => source.file(fileId ?? "", from),
    enabled: fileId !== null,
  });
  const data = view.data;
  const notes = useMemo(() => notesOf(data), [data]);
  const pinnedKey = pinnedSymbol ? `def:${pinnedSymbol}` : pinnedLocal;
  const shownKey = pinnedKey ?? preview;
  // The card's anchor is whichever control is shown; floating-ui follows it through scrolling. The element
  // is looked up when floating-ui measures (after commit), never during render.
  const anchor = useMemo(
    () => ({
      current: {
        getBoundingClientRect: () => triggers.current.get(shownKey ?? "")?.getBoundingClientRect() ?? new DOMRect(),
        get contextElement() {
          return triggers.current.get(shownKey ?? "");
        },
      },
    }),
    [shownKey],
  );

  useEffect(() => {
    if (line && data) document.getElementById(`L${line}`)?.scrollIntoView?.({ block: "center" });
  }, [line, data]);
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
  const pin = (key: string) => {
    window.clearTimeout(openTimer.current);
    setPreview(null);
    if (key.startsWith("def:")) {
      setPinnedLocal(null);
      update({ symbol: key.slice(4) });
    } else {
      if (pinnedSymbol) update({ symbol: null });
      setPinnedLocal(key);
    }
  };
  const unpin = () => {
    const trigger = pinnedKey ? triggers.current.get(pinnedKey) : undefined;
    if (pinnedSymbol) update({ symbol: null });
    setPinnedLocal(null);
    trigger?.focus();
  };
  const follow = (r: Related) => {
    if (!r.file_id) return;
    setWindowStart(null);
    setPreview(null);
    setPinnedLocal(null);
    update({ file: r.file_id, line: String(r.start_line ?? 1), end: null, symbol: r.id });
  };
  const openSymbol = (id: string) => {
    void source.symbol(id).then((s) => follow({ ...s, resolution: "name", ambiguous: false }));
  };
  const hover = (key: string) => {
    window.clearTimeout(closeTimer.current);
    window.clearTimeout(openTimer.current);
    if (pinnedKey) return; // a pinned card stays until it is closed
    openTimer.current = window.setTimeout(() => setPreview(key), preview ? 0 : HOVER_DELAY_MS);
  };
  const leave = () => {
    window.clearTimeout(openTimer.current);
    closeTimer.current = window.setTimeout(() => setPreview(null), LEAVE_DELAY_MS);
  };

  const last = data ? data.start_line + data.lines.length - 1 : 0;
  /** The line that names a definition: its first line, or one of the next few when its range starts with
   * attributes or a doc comment. */
  const nameLine = (s: { name: string; start_line: number; end_line: number }): number => {
    for (let n = s.start_line; n <= Math.min(s.end_line, s.start_line + 5); n++) {
      if (data && nameRange(data.lines[n - data.start_line] ?? "", s.name)) return n;
    }
    return s.start_line;
  };
  const total = data?.line_count ?? last;

  // Every control of the window, by key: what its card shows. Computed from the data alone, so the card
  // knows what to show before the controls are drawn.
  const described = useMemo(() => {
    const out = new Map<string, Shown>();
    if (!data) return out;
    for (const s of data.symbols) out.set(`def:${s.id}`, { type: "def", symbolId: s.id });
    for (const n of notes) out.set(`note:${n.start}:${n.end}`, { type: "note", ...n });
    return out;
  }, [data, notes]);
  /** What a control's card shows. Uses and parameters are keyed by line and UTF-16 offset, so their card
   * is found again from the data alone (the lines are drawn later, inside Highlight). */
  const shownOf = (key: string | null): Shown | undefined => {
    if (!key || !data) return undefined;
    const known = described.get(key);
    if (known) return known;
    const [kind, lineText, offsetText] = key.split(":");
    const number = Number(lineText);
    const offset = Number(offsetText);
    const text = data.lines[number - data.start_line] ?? "";
    if (kind === "ref") {
      const hit = referenceRanges(
        text,
        data.references.filter((r) => r.line === number),
      ).find((r) => r.start === offset);
      const first = hit?.ref.targets[0];
      if (!hit || !first) return undefined;
      return hit.ref.targets.length > 1
        ? { type: "refs", name: text.slice(hit.start, hit.end), targets: hit.ref.targets, resolution: hit.ref.resolution }
        : { type: "ref", symbolId: first.id };
    }
    if (kind === "prm") {
      const use = parameterUses(text, [{ types: ["plain"], content: text }], number, data.symbols).find(
        (u) => u.start === offset,
      );
      return use ? { type: "param", ownerId: use.owner.id, name: use.name } : undefined;
    }
    return undefined;
  };

  /** A button that opens a card; its visible text is the name itself (WCAG 2.5.3). */
  const trigger = (key: string, label: string, className: string, focusable: boolean) =>
    function wrap(children: ReactNode) {
      return (
        <button
          type="button"
          ref={(el) => {
            if (el) triggers.current.set(key, el);
            else triggers.current.delete(key);
          }}
          aria-label={label}
          aria-expanded={shownKey === key}
          aria-haspopup="dialog"
          tabIndex={focusable ? undefined : -1}
          onPointerEnter={() => hover(key)}
          onPointerLeave={leave}
          onClick={() => pin(key)}
          className={cn("sym", className, shownKey === key && "on")}
        >
          {children}
        </button>
      );
    };

  const hotKey = shownKey?.startsWith("note:") ? shownKey : null;
  const hot = hotKey ? notes.find((n) => `note:${n.start}:${n.end}` === hotKey) : undefined;

  const code = (
    <Pane title="Source">
      {data ? (
        <div className="min-h-0 flex-1 overflow-auto">
          <header className="file-head">
            <FilePath path={data.path} />
            <p className="file-facts">
              {total ? `${total} lines` : ""}
              {LANGUAGE_NAMES[languageOf(data.path)] ? `, ${LANGUAGE_NAMES[languageOf(data.path)]}` : ""}
              {data.start_line > 1 || last < total ? `; showing ${data.start_line}–${last}` : ""}
            </p>
          </header>
          {data.explanation?.summary ? (
            <div className="file-about">
              <p>
                <InlineText text={data.explanation.summary.split("\n\n")[0] ?? ""} />
              </p>
              <p className="text-[14px] text-muted">
                {data.explanation.status === "generated"
                  ? "AI-generated; check the cited source."
                  : "Partly derived from the source only."}
              </p>
            </div>
          ) : null}
          <div className="file-tools">
            {notes.length ? (
              <div className="notes-mode" role="group" aria-label="How line notes are shown">
                <button type="button" aria-pressed={!inlineNotes} onClick={() => update({ notes: null })}>
                  Notes on hover
                </button>
                <button type="button" aria-pressed={inlineNotes} onClick={() => update({ notes: "inline" })}>
                  Notes inline
                </button>
              </div>
            ) : null}
            {data.start_line > 1 ? (
              <Button size="sm" onClick={() => setWindowStart(Math.max(1, data.start_line - WINDOW))}>
                Earlier lines
              </Button>
            ) : null}
          </div>
          <Highlight code={data.lines.join("\n")} language={languageOf(data.path)} theme={NO_THEME}>
            {({ tokens }) => (
              <div className="code">
                {tokens.map((lineTokens, index) => {
                  const number = data.start_line + index;
                  const text = data.lines[index] ?? "";
                  const controls: Control[] = [];
                  for (const symbol of data.symbols.filter((s) => nameLine(s) === number)) {
                    const range = nameRange(text, symbol.name);
                    if (!range) continue;
                    const key = `def:${symbol.id}`;
                    controls.push({
                      key,
                      start: range[0],
                      end: range[1],
                      render: trigger(key, `Explain ${symbol.kind} ${symbol.name}`, `def ${kindClass(symbol.kind)}`, true),
                    });
                  }
                  const lineRefs = data.references.filter((r) => r.line === number);
                  for (const { start: a, end: b, ref } of referenceRanges(text, lineRefs)) {
                    const first = ref.targets[0];
                    if (!first) continue;
                    const key = `ref:${number}:${a}`;
                    const several = ref.targets.length > 1;
                    controls.push({
                      key,
                      start: a,
                      end: b,
                      render: trigger(
                        key,
                        several
                          ? `${text.slice(a, b)}: one of ${ref.targets.length} definitions`
                          : `Explain ${first.kind} ${first.name}, used here`,
                        `ref ${several ? "k-variable" : kindClass(first.kind)}`,
                        false,
                      ),
                    });
                  }
                  for (const use of parameterUses(text, lineTokens, number, data.symbols)) {
                    const key = `prm:${number}:${use.start}`;
                    controls.push({
                      key,
                      start: use.start,
                      end: use.end,
                      render: trigger(key, `Explain parameter ${use.name} of ${use.owner.name}`, "prm k-param", false),
                    });
                  }
                  const lineNotes = notes.filter((n) => n.start <= number && number <= n.end);
                  const startsHere = notes.filter((n) => n.start === number);
                  const endsHere = notes.filter((n) => n.end === number);
                  const targeted = line !== null && end !== null && number >= line && number <= end;
                  const firstNote = lineNotes[0];
                  return (
                    <Fragment key={number}>
                      <div
                        id={`L${number}`}
                        className={cn(
                          "l",
                          firstNote && "nr",
                          hot && hot.start <= number && number <= hot.end && "hot",
                          targeted && "target",
                        )}
                        onPointerEnter={
                          !inlineNotes && firstNote
                            ? (event) => {
                                if ((event.target as HTMLElement).closest(".sym")) return;
                                hover(`note:${firstNote.start}:${firstNote.end}`);
                              }
                            : undefined
                        }
                        onPointerLeave={!inlineNotes && firstNote ? leave : undefined}
                      >
                        <span className="g">
                          {startsHere.map((note) => {
                            const key = `note:${note.start}:${note.end}`;
                            return (
                              <button
                                key={key}
                                type="button"
                                className="note-dot"
                                ref={(el) => {
                                  if (el) triggers.current.set(key, el);
                                  else triggers.current.delete(key);
                                }}
                                aria-label={
                                  note.start === note.end
                                    ? `Note on line ${note.start}`
                                    : `Note on lines ${note.start}–${note.end}`
                                }
                                aria-expanded={shownKey === key}
                                aria-haspopup="dialog"
                                onClick={() => pin(key)}
                              />
                            );
                          })}
                          <span aria-hidden="true">{number}</span>
                        </span>
                        <span className="c">{renderLine(lineTokens, withoutOverlaps(controls))}</span>
                      </div>
                      {inlineNotes
                        ? endsHere.map((note) => (
                            <div key={`inl:${note.start}:${note.end}`} className="inl">
                              <span className="inl-at">
                                {note.start === note.end ? `line ${note.start}` : `lines ${note.start}–${note.end}`}
                              </span>
                              <InlineText text={note.text} />
                            </div>
                          ))
                        : null}
                    </Fragment>
                  );
                })}
              </div>
            )}
          </Highlight>
          {total && last < total ? (
            <Button size="sm" className="m-3" onClick={() => setWindowStart(last + 1)}>
              Later lines
            </Button>
          ) : null}
        </div>
      ) : fileId ? (
        <div className="flex flex-col gap-2 p-4">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      ) : (
        <div className="grid h-full place-items-center p-8 text-center text-muted">
          <p>
            Choose a file to read. Names and noted lines open an explanation when you hover them; click to keep it
            open.
          </p>
        </div>
      )}
    </Pane>
  );

  const tree = (
    <nav aria-label="Files" className="files-pane h-full overflow-auto py-2">
      <Tree
        source={source}
        path=""
        depth={0}
        current={fileId}
        currentPath={data?.path ?? null}
        onOpen={(entry) => {
          setWindowStart(null);
          setPreview(null);
          setPinnedLocal(null);
          update({ file: entry.file_id, line: null, end: null, symbol: null });
        }}
      />
    </nav>
  );

  const outline = (
    <Pane title="Outline" className="outline-pane overflow-auto">
      <div className="flex flex-col gap-4 p-4">
        <div>
          <h2>In this file</h2>
          {data?.symbols.length ? (
            <ul className="flex flex-col">
              {data.symbols.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => update({ line: String(s.start_line), end: String(s.end_line), symbol: s.id })}
                    title={s.summary ?? undefined}
                    className={cn(
                      `o ${kindClass(s.kind)} flex h-7 w-full items-center rounded-[4px] px-1.5 text-left hover:bg-surface-muted`,
                      pinnedSymbol === s.id && "bg-surface-muted",
                    )}
                  >
                    <span className="min-w-0 flex-1 truncate">{s.name}</span>
                    <span className="text-xs text-muted">{s.start_line}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">{data ? "No definitions in this file." : "Choose a file."}</p>
          )}
        </div>
        {notes.length ? (
          <p className="notes-count">
            {notes.length} line {notes.length === 1 ? "note" : "notes"}, marked in the margin.
          </p>
        ) : null}
      </div>
    </Pane>
  );

  const shown = shownOf(shownKey);
  const isPinned = shownKey !== null && shownKey === pinnedKey;
  const cardProps = { pinned: isPinned, onPin: () => shownKey && pin(shownKey), onClose: unpin };
  const card =
    shown?.type === "def" || shown?.type === "ref" ? (
      <SymbolCard
        source={source}
        symbolId={shown.symbolId}
        {...cardProps}
        onFollow={follow}
        guideBase={base}
        showOpen={shown.type === "ref"}
      />
    ) : shown?.type === "refs" ? (
      <CandidatesCard
        name={shown.name}
        targets={shown.targets}
        resolution={shown.resolution}
        {...cardProps}
        onOpen={openSymbol}
      />
    ) : shown?.type === "param" ? (
      <ParamCard source={source} ownerId={shown.ownerId} name={shown.name} {...cardProps} />
    ) : shown?.type === "note" ? (
      <NoteCard start={shown.start} end={shown.end} text={shown.text} {...cardProps} />
    ) : null;
  const named = (id: string) =>
    data?.symbols.find((x) => x.id === id) ?? data?.references.flatMap((x) => x.targets).find((t) => t.id === id);
  const cardLabel =
    shown?.type === "def" || shown?.type === "ref"
      ? (() => {
          const n = named(shown.symbolId);
          return n ? `${n.kind} ${n.name}` : "Definition";
        })()
      : shown?.type === "param"
        ? `parameter ${shown.name}`
        : shown?.type === "note"
          ? "Line note"
          : shown?.type === "refs"
            ? `${shown.name}: candidates`
            : "Explanation";

  return (
    <div className="reading code-frame mt-4 overflow-hidden rounded-[var(--radius-overlay)] border border-border">
      <Popover.Root
        key={shownKey ?? "none"}
        open={shown !== undefined} // its control is on screen to anchor to
        onOpenChange={(open) => {
          if (open) return;
          if (pinnedKey) unpin();
          else setPreview(null);
        }}
      >
        <Popover.Anchor virtualRef={anchor} />
        <Popover.Portal>
          <Popover.Content
            side="bottom"
            align="start"
            sideOffset={6}
            collisionPadding={{ top: 72, right: 12, bottom: 12, left: 12 }} // clear of the app's top bar
            aria-label={cardLabel}
            onOpenAutoFocus={(event) => event.preventDefault()} // a preview never steals focus
            onCloseAutoFocus={(event) => event.preventDefault()} // unpin() returns focus to the name
            onInteractOutside={(event) => {
              if (pinnedKey) event.preventDefault(); // a pinned card stays while the reader scrolls or selects
            }}
            onPointerEnter={() => window.clearTimeout(closeTimer.current)}
            onPointerLeave={() => {
              if (!pinnedKey) leave();
            }}
            className="reading z-40 w-[min(460px,calc(100vw-24px))] overflow-hidden rounded-[9px] border border-border bg-surface shadow-[var(--gloss-shadow)]"
          >
            {card}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      {wide ? (
        // The group sizes itself to its parent (inline height: 100%), so the parent fixes the height.
        <div className="h-[calc(100dvh-15rem)] min-h-[480px]">
          <Group orientation="horizontal">
            <Panel id="tree" defaultSize={230} minSize={180} maxSize={420} className="h-full overflow-hidden">
              {tree}
            </Panel>
            <ResizeHandle />
            <Panel id="code" minSize={360} className="h-full overflow-hidden bg-background">
              {code}
            </Panel>
            <ResizeHandle />
            <Panel id="outline" defaultSize={220} minSize={180} maxSize={420} className="h-full overflow-hidden">
              {outline}
            </Panel>
          </Group>
        </div>
      ) : (
        <div className="flex flex-col divide-y divide-border">
          <details className="group">
            <summary className="flex h-10 cursor-pointer items-center gap-2 px-4 text-[14px] font-medium">
              <Folder aria-hidden="true" className="size-4 text-muted" /> Files
              <span className="ml-auto truncate font-mono text-[12px] text-muted">{data?.path ?? "none open"}</span>
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
