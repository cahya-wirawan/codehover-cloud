import type { Token } from "prism-react-renderer";
import type { ReactNode } from "react";

import type { FileReference, FileSymbol } from "../../lib/api/resources";

/** The CSS class for a definition kind (its colour); only plain lowercase kinds become classes. */
export function kindClass(kind: string): string {
  return /^[a-z]{1,20}$/.test(kind) ? `k-${kind}` : "";
}

/** A hoverable span of one source line, in UTF-16 offsets of that line's text. */
export interface Control {
  key: string;
  start: number;
  end: number;
  render: (children: ReactNode) => ReactNode;
}

const IDENTIFIER = /[\p{L}\p{N}_$]+/uy;
const WORD = /[\p{L}_$][\p{L}\p{N}_$]*/gu;

/** The API counts columns in Unicode scalar values (06); the DOM counts UTF-16 units. This is the one
 * place they meet. */
export function utf16Offset(text: string, scalarColumn: number): number {
  let offset = 0;
  let scalars = 0;
  for (const ch of text) {
    if (scalars === scalarColumn) return offset;
    offset += ch.length;
    scalars += 1;
  }
  return scalars === scalarColumn ? offset : -1;
}

/** The identifier starting at `offset`, as [start, end), or null when none starts there. */
export function identifierAt(text: string, offset: number): [number, number] | null {
  if (offset < 0) return null;
  IDENTIFIER.lastIndex = offset;
  const match = IDENTIFIER.exec(text);
  return match ? [offset, offset + match[0].length] : null;
}

/** The first whole-word occurrence of a definition's name on its line, or null (unusual syntax). */
export function nameRange(text: string, name: string): [number, number] | null {
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

/** Offsets of string and comment tokens, where no name is hoverable. */
function quoted(tokens: Token[]): [number, number][] {
  const out: [number, number][] = [];
  let position = 0;
  for (const token of tokens) {
    const end = position + token.content.length;
    if (token.types.some((t) => t === "string" || t === "comment" || t === "char" || t === "template-string"))
      out.push([position, end]);
    position = end;
  }
  return out;
}

/** Uses of a parameter inside the innermost definition that documents it (not after a `.`, not in strings
 * or comments). */
export function parameterUses(
  text: string,
  tokens: Token[],
  line: number,
  symbols: FileSymbol[],
): { start: number; end: number; name: string; owner: FileSymbol }[] {
  const owners = symbols.filter((s) => s.params.length && s.start_line <= line && line <= s.end_line);
  if (!owners.length) return [];
  const skip = quoted(tokens);
  const out: { start: number; end: number; name: string; owner: FileSymbol }[] = [];
  for (const match of text.matchAll(WORD)) {
    const start = match.index;
    const name = match[0];
    if (text[start - 1] === "." || skip.some(([a, b]) => start >= a && start < b)) continue;
    const owner = owners
      .filter((s) => s.params.includes(name))
      .sort((a, b) => b.start_line - a.start_line)[0];
    if (owner) out.push({ start, end: start + name.length, name, owner });
  }
  return out;
}

/** Spans of references on a line, as UTF-16 ranges of their identifiers. */
export function referenceRanges(text: string, references: FileReference[]): { start: number; end: number; ref: FileReference }[] {
  const out: { start: number; end: number; ref: FileReference }[] = [];
  for (const ref of references) {
    const range = identifierAt(text, utf16Offset(text, ref.column));
    if (range) out.push({ start: range[0], end: range[1], ref });
  }
  return out;
}

/** Keeps the first of overlapping controls, in priority order (the caller lists definitions first). */
export function withoutOverlaps(controls: Control[]): Control[] {
  const kept: Control[] = [];
  for (const c of controls) {
    if (c.end > c.start && !kept.some((k) => c.start < k.end && k.start < c.end)) kept.push(c);
  }
  return kept.sort((a, b) => a.start - b.start);
}

/** A line's highlighted tokens with each control's text wrapped in its control. The source text is
 * never altered: every character appears exactly once, in order. */
export function renderLine(tokens: Token[], controls: Control[]): ReactNode[] {
  const pieces: { start: number; end: number; text: string; cls: string | undefined }[] = [];
  let position = 0;
  for (const token of tokens) {
    const end = position + token.content.length;
    pieces.push({ start: position, end, text: token.content, cls: tokenClass(token) });
    position = end;
  }
  const out: ReactNode[] = [];
  let at = 0;
  let index = 0;
  const slice = (from: number, to: number, keyPrefix: string): ReactNode[] =>
    pieces
      .filter((p) => p.end > from && p.start < to)
      .map((p, i) => (
        <span key={`${keyPrefix}${i}`} className={p.cls}>
          {p.text.slice(Math.max(from, p.start) - p.start, Math.min(to, p.end) - p.start)}
        </span>
      ));
  for (const control of controls) {
    if (control.start > at) out.push(...slice(at, control.start, `t${index++}.`));
    out.push(<span key={control.key}>{control.render(slice(control.start, control.end, `c${index++}.`))}</span>);
    at = control.end;
  }
  if (at < position) out.push(...slice(at, position, `t${index}.`));
  return out;
}
