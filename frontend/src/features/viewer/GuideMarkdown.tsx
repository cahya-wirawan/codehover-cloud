import MarkdownIt, { type Token } from "markdown-it";
import type { ReactNode } from "react";

/**
 * Guide Markdown as React elements (19 P04). The parser is markdown-it's CommonMark preset, the one the
 * backend's allowlist checks pages with, so both see the same tokens. Raw HTML is off, so tags in model
 * output arrive as text; nothing here sets HTML. Tokens map onto a fixed set of elements; images render
 * as their alt text, and only https and in-page links become links. Anything else is shown as text.
 */
const parser = new MarkdownIt("commonmark", { html: false });

type Text = (text: string, key: string) => ReactNode;

const SAFE_HREF = /^(https:\/\/|#)/i;

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

function inline(tokens: Token[], text: Text, prefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (!token) continue;
    const key = `${prefix}.${i}`;
    if (token.nesting === 1) {
      const end = closing(tokens, i);
      const children = inline(tokens.slice(i + 1, end), text, key);
      i = end;
      if (token.type === "strong_open") out.push(<strong key={key} className="font-semibold">{children}</strong>);
      else if (token.type === "em_open") out.push(<em key={key}>{children}</em>);
      else if (token.type === "link_open") {
        const href = attr(token, "href") ?? "";
        out.push(
          SAFE_HREF.test(href) ? (
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
    else if (token.type === "code_inline")
      out.push(
        <code key={key} className="rounded-[4px] bg-surface-muted px-1 py-0.5 font-mono text-[13px]">
          {token.content}
        </code>,
      );
    else if (token.type === "softbreak") out.push("\n");
    else if (token.type === "hardbreak") out.push(<br key={key} />);
    else if (token.type === "image") out.push(text(token.content, key)); // alt text only: no images (19 P04)
    else if (token.content) out.push(text(token.content, key));
  }
  return out;
}

const HEADING_OFFSET = 2;

const HEADING: Record<string, string> = {
  h1: "text-[24px] leading-8 font-semibold tracking-[-0.01em]",
  h2: "mt-8 text-[19px] leading-7 font-semibold",
  h3: "mt-6 text-[16px] leading-6 font-semibold",
  h4: "mt-5 text-[15px] leading-6 font-semibold",
  h5: "mt-4 text-[15px] leading-6 font-medium",
  h6: "mt-4 text-[15px] leading-6 font-medium text-muted",
};

function blocks(tokens: Token[], text: Text, prefix: string, tight = false): ReactNode[] {
  const out: ReactNode[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (!token) continue;
    const key = `${prefix}.${i}`;
    if (token.type === "inline") {
      out.push(...inline(token.children ?? [], text, key));
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
            {blocks(inner, text, key)}
          </Tag>,
        );
        break;
      }
      case "paragraph_open":
        // Tight list items hide their paragraphs, as CommonMark renders them.
        out.push(
          tight ? (
            <span key={key}>{blocks(inner, text, key)}</span>
          ) : (
            <p key={key} className="mt-4">
              {blocks(inner, text, key)}
            </p>
          ),
        );
        break;
      case "bullet_list_open":
      case "ordered_list_open": {
        const isTight = inner.some((t) => t.type === "paragraph_open" && t.hidden);
        const items = blocks(inner, text, key, isTight);
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
            {blocks(inner, text, key, tight)}
          </li>,
        );
        break;
      case "blockquote_open":
        out.push(
          <blockquote key={key} className="mt-4 border-l-2 border-border pl-4 text-muted [&>*:first-child]:mt-0">
            {blocks(inner, text, key)}
          </blockquote>,
        );
        break;
      default:
        out.push(<div key={key}>{blocks(inner, text, key)}</div>);
    }
  }
  return out;
}

/** Renders `markdown`; `text` turns each run of plain text into nodes (the guide uses it for citations). */
export function GuideMarkdown({ markdown, text }: { markdown: string; text: Text }) {
  return <div className="[&>*:first-child]:mt-0">{blocks(parser.parse(markdown, {}), text, "md")}</div>;
}
