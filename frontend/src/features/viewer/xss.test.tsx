/**
 * XSS corpus (13 "security"; M4 exit criterion: zero critical XSS issues). Repository content, model output
 * and names reach the page only as text: no element, attribute, handler or URL scheme from them survives,
 * in the product viewer, the guide, and the offline export viewer (run here in jsdom).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";

import { routes } from "../../app/routes";
// The offline export's own files, from the backend that ships them in every ZIP.
import exportPage from "../../../../backend/app/engine/export_assets/index.html?raw";
import viewerScript from "../../../../backend/app/engine/export_assets/viewer.js?raw";
import { GuideText } from "./GuidePanel";

const PAYLOADS = [
  "<script>window.__pwned = 1</script>",
  '<img src=x onerror="window.__pwned = 1">',
  '"><svg onload="window.__pwned=1">',
  "javascript:window.__pwned=1",
  '<a href="javascript:window.__pwned=1">click</a>',
  "<iframe srcdoc='<script>parent.__pwned=1</script>'></iframe>",
];
const HOSTILE = PAYLOADS.join(" ");

function assertInert(root: ParentNode) {
  expect(root.querySelector("script, img, iframe, object, embed, style, form")).toBeNull();
  // The only SVGs are our own decorative icons (Lucide, hidden from assistive technology).
  for (const svg of Array.from(root.querySelectorAll("svg"))) {
    expect(svg.getAttribute("class") ?? "").toMatch(/\blucide\b/);
    expect(svg.getAttribute("aria-hidden")).toBe("true");
  }
  for (const element of Array.from(root.querySelectorAll("*"))) {
    for (const attribute of Array.from(element.attributes)) {
      expect(attribute.name.startsWith("on")).toBe(false);
      expect(/^\s*javascript:/i.test(attribute.value)).toBe(false);
    }
  }
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
}

afterEach(() => vi.unstubAllGlobals());

test("guide markdown and forged citations render as text", () => {
  const { container } = render(
    <MemoryRouter>
      <GuideText
        page={{
          slug: "x",
          title: HOSTILE,
          markdown: `${HOSTILE} [[not-a-uuid:1-2]] [[00000000-0000-0000-0000-000000000000:1-2]]" onmouseover="x`,
          citations: [],
          claims: [],
        } as never}
        codeBase="/p/p1/builds/b1"
      />
    </MemoryRouter>,
  );
  expect(container.textContent).toContain("<script>window.__pwned = 1</script>");
  assertInert(container);
});

test("hostile source lines, names and cards stay text in the code viewer", async () => {
  const replies: Record<string, unknown> = {
    "/api/v1/builds/b1": {
      id: "b1", project_id: "p1", status: "ready", commit_sha: "c0ffee0000000000", snapshot_status: "scanned",
      rejection_code: null, created_at: "2026-11-18T00:00:00Z", stages: [], counts: { files: 1, symbols: 1 },
    },
    "/api/v1/builds/b1/tree?path=": {
      items: [{ name: HOSTILE, path: "evil.py", type: "file", file_id: "f1", status: "selected", reason: HOSTILE }],
      next_cursor: null,
    },
    "/api/v1/builds/b1/files/f1?start_line=1": {
      id: "f1", path: HOSTILE, start_line: 1, lines: [`def main(): # ${HOSTILE}`, ...PAYLOADS],
      symbols: [{ id: "s1", name: "main", kind: "function", start_line: 1, end_line: 2, summary: HOSTILE }],
      explanation: { status: "generated", summary: HOSTILE },
    },
    "/api/v1/builds/b1/symbols/s1": {
      id: "s1", name: "main", kind: "function", signature: HOSTILE, path: HOSTILE, start_line: 1,
      card: { summary: HOSTILE, details: HOSTILE, notes: HOSTILE, params: [{ name: HOSTILE, description: HOSTILE }] },
      uses: [{ id: "s2", name: HOSTILE, kind: "function", path: HOSTILE, resolution: "name", ambiguous: false }],
      used_by: [],
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const body = replies[typeof input === "string" ? input : input.toString()];
      return new Response(JSON.stringify(body ?? { items: [], next_cursor: null }), { status: body ? 200 : 404 });
    }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/p/p1/builds/b1/code"] })} />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: HOSTILE }));
  fireEvent.click(await screen.findByRole("button", { name: "Explain function main" }));
  await screen.findByRole("dialog", { name: "function main" });
  assertInert(document.body);
  expect(document.body.textContent).toContain('<img src=x onerror="window.__pwned = 1">');
});

test("the offline export viewer writes repository content as text only", () => {
  const html = exportPage;
  // The export's own trusted page skeleton (fixed markup from the repository, no tenant data).
  document.body.innerHTML = html.slice(html.indexOf("<body>") + 6, html.indexOf("<script"));
  const fileId = "00000000-0000-4000-8000-000000000001";
  (window as unknown as { CODEHOVER_DATA: unknown }).CODEHOVER_DATA = {
    project: { name: HOSTILE, repository: HOSTILE },
    build: { commit_sha: "c".repeat(40), created_at: "2026-11-18T00:00:00Z", variant: "en-standard" },
    generator: {},
    overview: null,
    files: [{ id: fileId, path: HOSTILE, language: "python", sha256: "0".repeat(64), lines: [`def main(): ${HOSTILE}`, ...PAYLOADS], explanation: { status: "generated", summary: HOSTILE, chunks: [] } }],
    symbols: [{ id: "s1", file_id: fileId, key: "k", name: "main", kind: "function", start_line: 1, end_line: 2, signature: HOSTILE, card: { summary: HOSTILE, details: HOSTILE }, uses: [] }],
    guide: [{ slug: "start", title: HOSTILE, markdown: `${HOSTILE} [[${fileId}:1-2]] [x](javascript:alert(1))`, check_status: "passed", claims: [{ key: "c", text: HOSTILE, status: "supported", reason: HOSTILE, waived: false }] }],
  };
  Element.prototype.scrollIntoView = () => undefined; // not implemented by jsdom
  // Our own viewer.js from the repository, evaluated as the export's page would (no interpolation).
  new Function(viewerScript)();
  for (const button of Array.from(document.querySelectorAll("nav button"))) {
    fireEvent.click(button);
    for (const inner of Array.from(document.querySelectorAll("main button"))) fireEvent.click(inner);
    assertInert(document.body);
  }
  expect(document.body.textContent).toContain("<script>window.__pwned = 1</script>");
});
