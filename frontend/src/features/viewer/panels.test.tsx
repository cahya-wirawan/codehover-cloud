import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";

import { ReviewPanel } from "./ReviewPanel";
import { SearchPanel } from "./SearchPanel";

function wrap(node: ReactNode, path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function fakeApi(replies: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      const body = replies[url];
      return body === undefined
        ? new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "x", request_id: null, retryable: false, details: {} } }), { status: 404 })
        : new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

test("search results link to the definition, its line and the guide", async () => {
  fakeApi({
    "/api/v1/builds/b1/search?q=help": {
      items: [
        { kind: "symbol", ref_id: "s1", name: "helper", path: "src/util.py", line: 4, symbol_kind: "function", snippet: "Counts.", basis: "name_prefix" },
        { kind: "guide", ref_id: "g1", name: "How it works", path: "how-it-works", line: null, symbol_kind: null, snippet: "", basis: "text" },
      ],
      next_cursor: null,
    },
  });
  wrap(<SearchPanel buildId="b1" base="/p/p1/builds/b1" />, "/?q=help");
  const definition = await screen.findByRole("link", { name: "helper" });
  expect(definition).toHaveAttribute("href", "/p/p1/builds/b1/code?symbol=s1&line=4");
  expect(screen.getByText(/src\/util.py:4 · name prefix/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "How it works" })).toHaveAttribute("href", "/p/p1/builds/b1/guide/how-it-works");
  fireEvent.keyDown(window, { key: "/" });
  expect(screen.getByRole("textbox", { name: /Search definitions/ })).toHaveFocus();
});

const QUALITY = {
  claims: 2,
  counts: { supported: 1, partial: 0, unsupported: 1, unchecked: 0, invalid: 0 },
  waived: { unsupported: 0, partial: 0 },
  supported_fraction: 0.5,
  uncited_sentences: 0,
  pages: { total: 1, failed: 0 },
  publishable: false,
  blockers: ["unsupported_claims"],
  requires_acknowledgment: false,
};
const CHECKS = {
  items: [
    { id: "c1", slug: "flow", claim_key: "k1", claim_text: "It caches results.", citation: "[[x]]", path: "a.py", start_line: 1, end_line: 3, deterministic: "valid", status: "unsupported", reason: "No cache is shown.", waived: false, waived_reason: null },
    { id: "c2", slug: "flow", claim_key: "k2", claim_text: "It returns a sum.", citation: "[[y]]", path: "a.py", start_line: 4, end_line: 5, deterministic: "valid", status: "supported", reason: "", waived: false, waived_reason: null },
  ],
  next_cursor: null,
};

test("review explains blockers; only admins can select claims to waive", async () => {
  fakeApi({ "/api/v1/builds/b1/quality": QUALITY, "/api/v1/builds/b1/quality/checks?limit=100": CHECKS });
  const { unmount } = wrap(<ReviewPanel buildId="b1" canWaive={false} />);
  expect(await screen.findByText(/unsupported and not waived/)).toBeInTheDocument();
  expect(await screen.findByText("It caches results.")).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).toBeNull();
  unmount();
  wrap(<ReviewPanel buildId="b1" canWaive />);
  const box = await screen.findByRole("checkbox", { name: /It caches results/ });
  expect(screen.getAllByRole("checkbox")).toHaveLength(1); // supported claims are not waivable
  fireEvent.click(box);
  expect(screen.getByRole("button", { name: "Waive" })).toBeDisabled(); // a reason is required
  fireEvent.change(screen.getByRole("textbox", { name: /Reason for waiving/ }), { target: { value: "Documented elsewhere in the repo." } });
  expect(screen.getByRole("button", { name: "Waive" })).toBeEnabled();
});
