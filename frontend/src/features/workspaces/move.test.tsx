import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { WorkspacePage } from "./WorkspacePage";

afterEach(() => vi.unstubAllGlobals());

function serve(moveAnswers: Response[], sent: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/v1/auth/csrf") return new Response(JSON.stringify({ csrf_token: "t" }));
      if (init?.method === "POST" && url === "/api/v1/workspaces/w1/move") {
        sent.push(JSON.parse(String(init.body)));
        return moveAnswers.shift() ?? new Response("{}", { status: 500 });
      }
      if (url === "/api/v1/me") return new Response(JSON.stringify({ id: "u1", display_name: "Grace", handle: "grace", handle_confirmed: true }));
      if (url === "/api/v1/organizations")
        return new Response(
          JSON.stringify({
            items: [
              { id: "o1", name: "Acme", handle: "acme", role: "admin" },
              { id: "o2", name: "Globex", handle: "globex", role: "member" },
            ],
            invitations: [],
          }),
        );
      if (url === "/api/v1/workspaces")
        return new Response(
          JSON.stringify({ items: [{ id: "w1", name: "Mine", slug: "mine", role: "owner", owner: { kind: "user", handle: "grace", organization_id: null } }], next_cursor: null }),
        );
      return new Response(JSON.stringify({ items: [], next_cursor: null }));
    }),
  );
}

function page() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/w/w1"]}>
        <Routes>
          <Route path="/w/:wid" element={<WorkspacePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("owners move a workspace to an organization they administer; clashing addresses are listed", async () => {
  const sent: unknown[] = [];
  const clash = {
    error: {
      code: "SLUG_TAKEN",
      message: "acme already has projects at these addresses. Change their addresses in this workspace, then move it.",
      request_id: null,
      retryable: false,
      details: { projects: [{ project_id: "p1", name: "API", slug: "api" }] },
    },
  };
  const moved = { id: "w1", name: "Mine", slug: "mine", role: "owner", owner: { kind: "organization", handle: "acme", organization_id: "o1" } };
  serve([new Response(JSON.stringify(clash), { status: 409 }), new Response(JSON.stringify(moved))], sent);
  page();
  const owner = await screen.findByLabelText("New owner");
  // Only organizations the person owns or administers, never the current owner.
  await waitFor(() => expect(screen.getByRole("option", { name: "Acme (acme)" })).toBeInTheDocument());
  expect(screen.queryByRole("option", { name: "Globex (globex)" })).toBeNull();
  expect(screen.queryByRole("option", { name: /You/ })).toBeNull();
  fireEvent.change(owner, { target: { value: "acme" } });
  expect(screen.getByText(/Its sites move to \/sites\/acme\//)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Move workspace" }));
  expect(await screen.findByRole("link", { name: "API" })).toHaveAttribute("href", "/p/p1");
  fireEvent.click(screen.getByRole("button", { name: "Move workspace" }));
  expect(await screen.findByText("Moved to acme.")).toBeInTheDocument();
  expect(sent).toEqual([{ organization_id: "o1" }, { organization_id: "o1" }]);
});
