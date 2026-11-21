/**
 * The offline export's data (10): the knowledge base embedded in the ZIP (`kb-export` schema), served to
 * the product's own reading view through the same ViewerSource the API provides. Nothing here calls the
 * network; every answer is computed from the embedded data the way the API computes it.
 */
import type {
  CheckStatus,
  FileView,
  GuideIndex,
  GuidePage,
  Page,
  Related,
  SearchHit,
  SymbolView,
  TreeEntry,
  ViewerSource,
} from "../lib/api/resources";

export interface KbFile {
  id: string;
  path: string;
  language: string | null;
  sha256: string;
  lines: string[];
  explanation: {
    status: string;
    summary: string;
    chunks: { start_line: number; end_line: number; summary: string; status: string; notes?: { start_line: number; end_line: number; text: string }[] }[];
  } | null;
  references?: { line: number; column: number; targets: string[]; resolution: string; ambiguous: boolean }[];
}
export interface KbSymbol {
  id: string;
  file_id: string;
  key: string;
  name: string;
  kind: string;
  start_line: number;
  end_line: number;
  signature: string | null;
  card: SymbolView["card"] & Record<string, unknown>;
  uses: string[];
}
export interface KbGuidePage {
  slug: string;
  title: string;
  section?: string;
  purpose?: string;
  markdown: string;
  check_status: string;
  claims: { key: string; text: string; status: CheckStatus; reason: string; waived: boolean }[];
}
export interface Kb {
  project: { name: string; repository: string };
  build: { commit_sha: string; created_at: string; variant: string };
  files: KbFile[];
  symbols: KbSymbol[];
  guide: KbGuidePage[];
  overview: {
    overview: string;
    key_concepts: { term: string; explanation: string }[];
    reading_order: { file_id: string; reason: string }[];
  } | null;
}

const WINDOW = 500;
const CITATION = /\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]/g;
const MENTION = /`([A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*)(?:\(\))?`/g;
const STATUSES: CheckStatus[] = ["supported", "partial", "unsupported", "unchecked", "invalid"];

const status = (value: string): CheckStatus => (STATUSES.includes(value as CheckStatus) ? (value as CheckStatus) : "unchecked");

function citations(markdown: string): { file_id: string; start: number; end: number }[] {
  return Array.from(markdown.matchAll(CITATION), (m) => ({ file_id: m[1] ?? "", start: Number(m[2]), end: Number(m[3]) }));
}

export function staticSource(kb: Kb): ViewerSource {
  const files = new Map(kb.files.map((f) => [f.id, f]));
  const symbols = new Map(kb.symbols.map((s) => [s.id, s]));
  const pathOf = (fileId: string) => files.get(fileId)?.path ?? null;
  const paramsOf = (s: KbSymbol) => (Array.isArray(s.card.params) ? s.card.params.map((p) => p.name) : []);
  const pageCites = kb.guide.map((g) => ({ page: g, cites: citations(g.markdown) }));

  /** A use from one definition to another is "index" when a reference inside the user resolved it so. */
  const resolution = (from: KbSymbol, to: string): string =>
    (files.get(from.file_id)?.references ?? []).some(
      (r) => r.resolution === "index" && r.targets.includes(to) && r.line >= from.start_line && r.line <= from.end_line,
    )
      ? "index"
      : "name";
  const related = (s: KbSymbol, how: string): Related => ({
    id: s.id,
    name: s.name,
    kind: s.kind,
    path: pathOf(s.file_id) ?? "",
    file_id: s.file_id,
    start_line: s.start_line,
    resolution: how,
    ambiguous: false,
  });

  const page = (g: KbGuidePage, index: number) => ({
    slug: g.slug,
    title: g.title,
    section: g.section ?? "",
    ordinal: index + 1,
    purpose: g.purpose ?? "",
    status: "written" as const,
    check_status: status(g.check_status),
    provenance: {},
  });

  return {
    key: `export:${kb.build.commit_sha}`,
    quality: null,
    async tree(path: string): Promise<Page<TreeEntry>> {
      const prefix = path ? `${path}/` : "";
      const dirs = new Set<string>();
      const entries: TreeEntry[] = [];
      for (const f of kb.files) {
        if (!f.path.startsWith(prefix)) continue;
        const rest = f.path.slice(prefix.length);
        const cut = rest.indexOf("/");
        if (cut >= 0) dirs.add(rest.slice(0, cut));
        else entries.push({ name: rest, path: f.path, type: "file", file_id: f.id, status: "selected", reason: null });
      }
      const folders: TreeEntry[] = [...dirs].sort().map((name) => ({
        name,
        path: `${prefix}${name}`,
        type: "dir",
        file_id: null,
        status: null,
        reason: null,
      }));
      return { items: [...folders, ...entries.sort((a, b) => a.name.localeCompare(b.name))], next_cursor: null };
    },
    async file(fileId: string, startLine: number): Promise<FileView> {
      const f = files.get(fileId);
      if (!f) throw new Error("File not found");
      const first = Math.max(1, startLine);
      const last = Math.min(f.lines.length, first + WINDOW - 1);
      return {
        id: f.id,
        path: f.path,
        start_line: first,
        end_line: last,
        line_count: f.lines.length,
        lines: f.lines.slice(first - 1, last),
        symbols: kb.symbols
          .filter((s) => s.file_id === f.id)
          .map((s) => ({
            id: s.id,
            name: s.name,
            kind: s.kind,
            start_line: s.start_line,
            end_line: s.end_line,
            summary: typeof s.card.summary === "string" ? s.card.summary : null,
            params: paramsOf(s),
          })),
        references: (f.references ?? [])
          .filter((r) => r.line >= first && r.line <= last)
          .map((r) => ({
            line: r.line,
            column: r.column,
            resolution: r.resolution,
            ambiguous: r.ambiguous,
            targets: r.targets.flatMap((id) => {
              const t = symbols.get(id);
              return t ? [{ id: t.id, name: t.name, kind: t.kind }] : [];
            }),
          }))
          .filter((r) => r.targets.length > 0),
        explanation: f.explanation
          ? {
              status: f.explanation.status,
              summary: f.explanation.summary,
              chunks: f.explanation.chunks.map((c) => ({ ...c, notes: c.notes ?? [] })),
            }
          : null,
      };
    },
    async symbol(symbolId: string): Promise<SymbolView> {
      const s = symbols.get(symbolId);
      if (!s) throw new Error("Symbol not found");
      return {
        id: s.id,
        name: s.name,
        kind: s.kind,
        signature: s.signature,
        path: pathOf(s.file_id) ?? "",
        file_id: s.file_id,
        start_line: s.start_line,
        end_line: s.end_line,
        card: s.card,
        uses: s.uses.flatMap((id) => {
          const t = symbols.get(id);
          return t ? [related(t, resolution(s, id))] : [];
        }),
        used_by: kb.symbols.filter((o) => o.uses.includes(s.id)).map((o) => related(o, resolution(o, s.id))),
        guides: pageCites
          .filter(({ cites }) => cites.some((c) => c.file_id === s.file_id && c.start <= s.end_line && c.end >= s.start_line))
          .map(({ page: g }) => ({ slug: g.slug, title: g.title })),
      };
    },
    async guide(): Promise<GuideIndex> {
      return {
        overview: kb.overview
          ? {
              overview: kb.overview.overview,
              status: "generated",
              key_concepts: kb.overview.key_concepts,
              reading_order: kb.overview.reading_order.map((r) => ({ ...r, path: pathOf(r.file_id) })),
            }
          : null,
        pages: kb.guide.map(page),
      };
    },
    async guidePage(slug: string): Promise<GuidePage> {
      const index = kb.guide.findIndex((g) => g.slug === slug);
      const g = kb.guide[index];
      if (!g) throw new Error("Guide page not found");
      const seen = new Map<string, { file_id: string; path: string | null; start_line: number; end_line: number }>();
      for (const c of citations(g.markdown))
        seen.set(`${c.file_id}:${c.start}-${c.end}`, { file_id: c.file_id, path: pathOf(c.file_id), start_line: c.start, end_line: c.end });
      const mentions = new Set(Array.from(g.markdown.matchAll(MENTION), (m) => m[1] ?? ""));
      return {
        ...page(g, index),
        markdown: g.markdown,
        citations: [...seen.values()],
        claims: g.claims.map((c) => ({ claim_key: c.key, text: c.text, status: status(c.status), reason: c.reason || null, waived: c.waived })),
        symbols: [...mentions].flatMap((mention) => {
          const name = mention.split(/\.|::/).pop() ?? mention;
          let found = kb.symbols.filter((s) => s.name === name);
          if (mention.includes(".") || mention.includes("::")) {
            const qualified = mention.replace(/::/g, ".");
            found = found.filter((s) => s.key.replace(/::/g, ".").endsWith(qualified));
          }
          const [only] = found;
          return found.length === 1 && only
            ? [{ mention, id: only.id, name: only.name, kind: only.kind, file_id: only.file_id, start_line: only.start_line }]
            : [];
        }),
      };
    },
    async search(query: string, kind: string | null): Promise<Page<SearchHit>> {
      const q = query.trim().toLowerCase();
      const hits: SearchHit[] = [];
      if (!kind || kind === "symbol") {
        for (const s of kb.symbols) {
          const name = s.name.toLowerCase();
          const basis = name === q ? "exact_name" : name.startsWith(q) ? "name_prefix" : name.includes(q) ? "text" : null;
          if (basis)
            hits.push({
              kind: "symbol",
              ref_id: s.id,
              name: s.name,
              path: pathOf(s.file_id),
              line: s.start_line,
              symbol_kind: s.kind,
              snippet: typeof s.card.summary === "string" ? s.card.summary : "",
              basis,
            });
        }
      }
      if (!kind || kind === "file") {
        for (const f of kb.files)
          if (f.path.toLowerCase().includes(q))
            hits.push({ kind: "file", ref_id: f.id, name: f.path, path: f.path, line: null, symbol_kind: null, snippet: f.explanation?.summary ?? "", basis: "text" });
      }
      if (!kind || kind === "guide") {
        for (const g of kb.guide) {
          const at = g.markdown.toLowerCase().indexOf(q);
          if (at >= 0 || g.title.toLowerCase().includes(q))
            hits.push({
              kind: "guide",
              ref_id: g.slug,
              name: g.title,
              path: g.slug,
              line: null,
              symbol_kind: null,
              snippet: at >= 0 ? g.markdown.slice(Math.max(0, at - 60), at + 100) : "",
              basis: "text",
            });
        }
      }
      const rank = { exact_name: 0, name_prefix: 1, text: 2 };
      return { items: hits.sort((a, b) => rank[a.basis] - rank[b.basis]).slice(0, 50), next_cursor: null };
    },
  };
}
