import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "../../app/routes";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
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
        ? new Response(JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "x", request_id: null, retryable: false, details: {} } }), { status: 401 })
        : new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

const BUILD = {
  id: "b1", project_id: "p1", status: "ready", commit_sha: "c0ffee0000000000", snapshot_status: "scanned",
  rejection_code: null, created_at: "2026-11-18T00:00:00Z",
  stages: [{ stage: "acquire", state: "succeeded", error_code: null }, { stage: "index", state: "succeeded", error_code: null }],
  counts: { files: 2, symbols: 1 },
};

test("ready build shows stages, file tree, source and a definition card", async () => {
  fakeApi({
    "/api/v1/builds/b1": BUILD,
    "/api/v1/builds/b1/tree?path=": { items: [{ name: "app.py", path: "app.py", type: "file", file_id: "f1", status: "selected", reason: null }], next_cursor: null },
    "/api/v1/builds/b1/files/f1": {
      id: "f1", path: "app.py", start_line: 1, lines: ["def main():", "    return 1"],
      symbols: [{ id: "s1", name: "main", kind: "function", start_line: 1, end_line: 2, summary: "Entry point." }],
    },
    "/api/v1/builds/b1/symbols/s1": {
      id: "s1", name: "main", kind: "function", signature: "def main()", path: "app.py", start_line: 1,
      card: { summary: "Entry point.", params: [] }, uses: [{ id: "s2", name: "helper", kind: "function", path: "util.py", resolution: "name", ambiguous: false }], used_by: [],
    },
  });
  renderAt("/p/p1/builds/b1");
  expect(await screen.findByText(/acquire: Done/)).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "app.py" }));
  fireEvent.click(await screen.findByRole("button", { name: "Explain function main" }));
  const card = await screen.findByRole("article", { name: "function main" });
  expect(card).toHaveTextContent("Entry point.");
  expect(card).toHaveTextContent("helper · util.py · matched by name");
});

test("connection errors from the GitHub callback are explained", async () => {
  fakeApi({ "/api/v1/workspaces/w1/connections": { items: [], next_cursor: null } });
  renderAt("/w/w1/connections?error=installation_not_accessible");
  expect(await screen.findByRole("alert")).toHaveTextContent("cannot access that installation");
});
