import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
      // These pages need a signed-in user (RequireSignIn); tests may override /me.
      const body = url === "/api/v1/me" && !(url in replies) ? { id: "u1", display_name: "Ada" } : replies[url];
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
    "/api/v1/builds/b1/files/f1?start_line=1": {
      id: "f1", path: "app.py", start_line: 1, lines: ["def main():", "    return 1"],
      symbols: [{ id: "s1", name: "main", kind: "function", start_line: 1, end_line: 2, summary: "Entry point." }],
    },
    "/api/v1/builds/b1/symbols/s1": {
      id: "s1", name: "main", kind: "function", signature: "def main()", path: "app.py", start_line: 1,
      card: { summary: "Entry point.", params: [] }, uses: [{ id: "s2", name: "helper", kind: "function", path: "util.py", resolution: "name", ambiguous: false }], used_by: [],
    },
  });
  renderAt("/p/p1/builds/b1");
  expect(await screen.findByRole("list", { name: "Stages" })).toHaveTextContent(/acquire: Done/);
  fireEvent.click(await screen.findByRole("link", { name: "Code" }));
  fireEvent.click(await screen.findByRole("button", { name: "app.py" }));
  const trigger = await screen.findByRole("button", { name: "Explain function main" });
  // WCAG 2.5.3: the control is the identifier, and the source line around it is unchanged.
  expect(trigger).toHaveTextContent(/^main$/);
  expect(document.getElementById("L1")).toHaveTextContent(/^1def main\(\):$/);
  fireEvent.click(trigger);
  const card = await screen.findByRole("dialog", { name: "function main" }); // floats at the name
  await waitFor(() => expect(card).toHaveTextContent("Entry point."));
  expect(within(card).getByRole("button", { name: "helper" })).toBeInTheDocument();
  expect(card).toHaveTextContent("util.py · matched by name");
  await waitFor(() => expect(screen.getByRole("heading", { name: "main" })).toHaveFocus()); // pinning moves focus
  fireEvent.keyDown(card, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByRole("button", { name: "Explain function main" })).toHaveFocus(); // and Esc returns it
});

test("connection errors from the GitHub callback are explained", async () => {
  fakeApi({ "/api/v1/workspaces/w1/connections": { items: [], next_cursor: null } });
  renderAt("/w/w1/connections?error=installation_not_accessible");
  expect(await screen.findByRole("alert")).toHaveTextContent("cannot access that installation");
});
