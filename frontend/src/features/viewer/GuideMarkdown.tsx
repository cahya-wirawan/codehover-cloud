import MarkdownIt, { type Token } from "markdown-it";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { MermaidDiagram } from "./MermaidDiagram";

/**
 * Guide Markdown as React elements (19 P04). The parser is markdown-it's CommonMark preset with tables,
 * the configuration the backend's allowlist checks pages with, so both see the same tokens. Raw HTML is
 * off, so tags in model output arrive as text; nothing here sets HTML. Tokens map onto a fixed set of
 * elements; images render as their alt text, and only https, in-page and `guide:<slug>` links become
 * links. Mermaid blocks are drawn as pictures (MermaidDiagram). Anything else is shown as text.
 */
const parser = new MarkdownIt("commonmark", { html: false }).enable("table");

type Text = (text: string, key: string) => ReactNode;
/** Where a `guide:<slug>` link goes, or null when this view has no guide to link to. */
type GuideHref = (slug: string) => string | null;
/** Inline code, which the guide turns into a link when it names a definition. */
type InlineCode = (code: string, key: string) => ReactNode;

const SAFE_HREF = /^(https:\/\/|#)/i;
const GUIDE_HREF = /^guide:([a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)$/;

function attr(token: Token, name: string): string | null {
  const value = token.attrGet(name);
  return value === null ? null : String(value);
}

/** The closing index of the token opened at `start` (same nesting level). */
function closing(tokens: Token[], start: number): number {
  let depth = 0;
  for (let i = start; i < tokens.length; i++) {
    depth += tokens[i]?.nesting ?? 0;
    if (depth === 0) return i;
  }
  return tokens.length - 1;
}

interface Hooks {
  text: Text;
  guideHref: GuideHref;
  code: InlineCode;
}

function inline(tokens: Token[], hooks: Hooks, prefix: string): ReactNode[] {
  const { text, guideHref } = hooks;
  const out: ReactNode[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (!token) continue;
    const key = `${prefix}.${i}`;
    if (token.nesting === 1) {
      const end = closing(tokens, i);
      const children = inline(tokens.slice(i + 1, end), hooks, key);
      i = end;
      if (token.type === "strong_open") out.push(<strong key={key} className="font-semibold">{children}</strong>);
      else if (token.type === "em_open") out.push(<em key={key}>{children}</em>);
      else if (token.type === "link_open") {
        const href = attr(token, "href") ?? "";
        const page = GUIDE_HREF.exec(href)?.[1];
        const internal = page ? guideHref(page) : null;
        out.push(
          internal ? (
            <Link key={key} to={internal} className="guide-link">
              {children}
            </Link>
          ) : SAFE_HREF.test(href) ? (
            <a
              key={key}
              href={href}
              {...(href.startsWith("#") ? {} : { target: "_blank", rel: "noopener noreferrer nofollow" })}
              className="text-primary underline underline-offset-2 hover:no-underline"
            >
              {children}
            </a>
          ) : (
            <span key={key}>{children}</span>
          ),
        );
      } else out.push(<span key={key}>{children}</span>);
      continue;
    }
    if (token.type === "text") out.push(text(token.content, key));
    else if (token.type === "code_inline") out.push(hooks.code(token.content, key));
    else if (token.type === "softbreak") out.push("\n");
    else if (token.type === "hardbreak") out.push(<br key={key} />);
    else if (token.type === "image") out.push(text(token.content, key)); // alt text only: no images (19 P04)
    else if (token.content) out.push(text(token.content, key));
  }
  return out;
}

const HEADING_OFFSET = 2;

const HEADING: Record<string, string> = {
  h1: "page-title",
  h2: "mt-11 mb-3 text-[26px] leading-[1.2] font-medium tracking-[-0.01em]",
  h3: "mt-8 mb-2 text-[20px] leading-[1.3] font-semibold",
  h4: "mt-6 mb-2 text-[18px] leading-[1.35] font-semibold",
  h5: "mt-5 text-[17px] font-semibold",
  h6: "mt-5 text-[17px] font-medium text-muted",
};

const ALIGN: Record<string, string> = { "text-align:left": "left", "text-align:center": "center", "text-align:right": "right" };

function blocks(tokens: Token[], hooks: Hooks, prefix: string, tight = false): ReactNode[] {
  const { text } = hooks;
  const nested = (inner: Token[], key: string, isTight = false) => blocks(inner, hooks, key, isTight);
  const out: ReactNode[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (!token) continue;
    const key = `${prefix}.${i}`;
    if (token.type === "inline") {
      out.push(...inline(token.children ?? [], hooks, key));
      continue;
    }
    if (token.type === "fence" && /^mermaid$/i.test(token.info.trim().split(/\s+/)[0] ?? "")) {
      out.push(<MermaidDiagram key={key} code={token.content} />);
      continue;
    }
    if (token.type === "fence" || token.type === "code_block") {
      const language = token.info.trim().split(/\s+/)[0] ?? "";
      out.push(
        <pre
          key={key}
          className="mt-4 overflow-x-auto rounded-[var(--radius-control)] border border-border bg-surface-muted p-3 font-mono text-[13px] leading-5"
          {...(language ? { "aria-label": `${language} code` } : {})}
        >
          <code>{token.content}</code>
        </pre>,
      );
      continue;
    }
    if (token.type === "hr") {
      out.push(<hr key={key} className="mt-6 border-border" />);
      continue;
    }
    if (token.nesting !== 1) {
      if (token.content) out.push(text(token.content, key)); // a block this renderer does not know
      continue;
    }
    const end = closing(tokens, i);
    const inner = tokens.slice(i + 1, end);
    i = end;
    switch (token.type) {
      case "heading_open": {
        // The page sits under the viewer's "Guide" heading (h2), so its levels move down by two; the
        // look follows the Markdown level.
        const level = Number(token.tag.slice(1)) || 6;
        const Tag = `h${Math.min(6, level + HEADING_OFFSET)}` as "h3" | "h4" | "h5" | "h6";
        out.push(
          <Tag key={key} className={HEADING[`h${Math.min(6, level)}`]}>
            {nested(inner, key)}
          </Tag>,
        );
        break;
      }
      case "paragraph_open":
        // Tight list items hide their paragraphs, as CommonMark renders them.
        out.push(
          tight ? (
            <span key={key}>{nested(inner, key)}</span>
          ) : (
            <p key={key} className="mt-4">
              {nested(inner, key)}
            </p>
          ),
        );
        break;
      case "bullet_list_open":
      case "ordered_list_open": {
        const isTight = inner.some((t) => t.type === "paragraph_open" && t.hidden);
        const items = nested(inner, key, isTight);
        const start = Number(attr(token, "start") ?? "1");
        out.push(
          token.type === "bullet_list_open" ? (
            <ul key={key} className="mt-4 list-disc space-y-1.5 pl-6">
              {items}
            </ul>
          ) : (
            <ol key={key} start={Number.isFinite(start) ? start : 1} className="mt-4 list-decimal space-y-1.5 pl-6">
              {items}
            </ol>
          ),
        );
        break;
      }
      case "list_item_open":
        out.push(
          <li key={key} className="pl-1 [&>*:first-child]:mt-0">
            {nested(inner, key, tight)}
          </li>,
        );
        break;
      case "blockquote_open":
        out.push(
          <blockquote key={key} className="mt-4 border-l-2 border-border pl-4 text-muted [&>*:first-child]:mt-0">
            {nested(inner, key)}
          </blockquote>,
        );
        break;
      case "table_open":
        out.push(
          <div key={key} className="guide-table">
            <table>{nested(inner, key)}</table>
          </div>,
        );
        break;
      case "thead_open":
        out.push(<thead key={key}>{nested(inner, key)}</thead>);
        break;
      case "tbody_open":
        out.push(<tbody key={key}>{nested(inner, key)}</tbody>);
        break;
      case "tr_open":
        out.push(<tr key={key}>{nested(inner, key)}</tr>);
        break;
      case "th_open":
      case "td_open": {
        const Cell = token.type === "th_open" ? "th" : "td";
        const align = ALIGN[attr(token, "style") ?? ""];
        out.push(
          <Cell key={key} {...(align ? { style: { textAlign: align as "left" | "center" | "right" } } : {})}>
            {nested(inner, key)}
          </Cell>,
        );
        break;
      }
      default:
        out.push(<div key={key}>{nested(inner, key)}</div>);
    }
  }
  return out;
}

const NO_GUIDE: GuideHref = () => null;
const PLAIN_CODE: InlineCode = (code, key) => <code key={key}>{code}</code>;

/** Renders `markdown`; `text` turns each run of plain text into nodes (the guide uses it for citations),
 * `guideHref` places links to other guide pages and `code` renders inline code. */
export function GuideMarkdown({
  markdown,
  text,
  guideHref = NO_GUIDE,
  code = PLAIN_CODE,
}: {
  markdown: string;
  text: Text;
  guideHref?: GuideHref;
  code?: InlineCode;
}) {
  return (
    <div className="[&>*:first-child]:mt-0">{blocks(parser.parse(markdown, {}), { text, guideHref, code }, "md")}</div>
  );
}
