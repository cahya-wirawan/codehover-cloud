import { drawable } from "./mermaidPolicy";

test("only plain diagrams reach Mermaid; directives, styling, HTML and frontmatter config stay source", () => {
  expect(drawable("flowchart TD\n  A[Parse] --> B[Check]")).toBe(true);
  expect(drawable("sequenceDiagram\n  A->>B: hello")).toBe(true);
  for (const hostile of [
    '%%{init: {"securityLevel": "loose"}}%%\nflowchart TD\n  A --> B',
    'flowchart TD\n  A --> B\n  click A "javascript:alert(1)"',
    "flowchart TD\n  A --> B\n  style A fill:#f00",
    "flowchart TD\n  classDef x fill:#f00\n  A:::x --> B",
    "flowchart TD\n  linkStyle 0 stroke:#f00\n  A --> B",
    'flowchart TD\n  A["<img src=x onerror=alert(1)>"] --> B',
    "---\nconfig:\n  securityLevel: loose\n---\nflowchart TD\n  A --> B",
    `flowchart TD\n${"  A --> B\n".repeat(3000)}`,
  ]) {
    expect(drawable(hostile)).toBe(false);
  }
});
