// The backend's guide allowlist (generate.MERMAID_FORBIDDEN), applied here too because repository Markdown
// files never pass through it: no click, style, class or link styling, init directives or HTML, no
// frontmatter configuration, and a bounded size. A diagram that fails is shown as its source only and never
// reaches Mermaid.
const FORBIDDEN = /\b(click|style|classDef|linkStyle|init)\b|%%\{|<[a-z/]/i;
const FRONTMATTER = /^\s*---/;
const MAX_DIAGRAM_CHARS = 20_000;

export function drawable(code: string): boolean {
  return code.length <= MAX_DIAGRAM_CHARS && !FORBIDDEN.test(code) && !FRONTMATTER.test(code);
}
